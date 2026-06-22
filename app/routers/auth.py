"""
Auth router — login, register, password management, /api/me.
Matches the frontend's existing _api() call signatures exactly.
"""

import hashlib
import logging
import os
import secrets
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User, Exclusion, EmailVerificationToken
from app.schemas import (
    LoginRequest, RegisterRequest, ChangePasswordRequest,
    ResetPasswordRequest,
    AuthResponse, MeResponse,
)
from pydantic import BaseModel, EmailStr
from app.services.auth_service import (
    hash_password, verify_password, create_token,
    get_current_user,
)
from app.services.email import send_email
from app.config import settings

router = APIRouter()

logger = logging.getLogger("nexuspay.auth")


def _resolve_seed_password(env_var: str) -> tuple[str, bool]:
    """Return (password, was_generated) for a seed account.

    The password is read from the given env var. If it is unset, a random
    one-time password is generated instead — no password is ever hardcoded in
    source. When generated, the caller logs it once so an operator can retrieve
    it from the boot logs and rotate it.
    """
    pw = (os.getenv(env_var) or "").strip()
    if pw:
        return pw, False
    return secrets.token_urlsafe(18), True


# ── Seed admin + demo on first call ─────────────────────────
_seeded = False

async def _seed_defaults(db: AsyncSession):
    global _seeded
    if _seeded:
        return
    _seeded = True

    # Admin — password from ADMIN_SEED_PASSWORD; random one-time if unset.
    # No default password is hardcoded, and the admin is always forced to rotate
    # it on first login.
    result = await db.execute(select(User).where(User.email == settings.ADMIN_EMAIL))
    if not result.scalar_one_or_none():
        admin_pw, generated = _resolve_seed_password("ADMIN_SEED_PASSWORD")
        if generated:
            logger.warning(
                "Seeded admin %s with a RANDOM one-time password (ADMIN_SEED_PASSWORD "
                "not set): %s — log in and change it immediately.",
                settings.ADMIN_EMAIL, admin_pw,
            )
        admin = User(
            email=settings.ADMIN_EMAIL,
            password_hash=hash_password(admin_pw),
            display_name="Admin",
            company="NexusPay Services",
            role="admin",
            tier="enterprise",
            active=True,
            verified=True,
            must_change_password=True,  # force rotation on first login
            created_by="system",
        )
        db.add(admin)

    # Demo — only seeded when DEMO_SEED_PASSWORD is explicitly set, so we never
    # create a shared account with a guessable/hardcoded password.
    result = await db.execute(select(User).where(User.email == "demo@nexuspayservices.com"))
    if not result.scalar_one_or_none():
        demo_pw = (os.getenv("DEMO_SEED_PASSWORD") or "").strip()
        if not demo_pw:
            logger.warning("DEMO_SEED_PASSWORD not set — skipping demo account seed.")
        else:
            demo = User(
                email="demo@nexuspayservices.com",
                password_hash=hash_password(demo_pw),
                display_name="Demo User",
                company="Demo",
                role="demo",
                tier="free",
                active=True,
                verified=True,
                created_by="system",
            )
            db.add(demo)

    await db.commit()


# ── Login rate limiting / lockout ───────────────────────────
# In-memory, per-process (same pattern/caveats as the chatbot limiter: resets on
# restart, and with >1 worker each worker keeps its own counters). Tunable via env.
LOGIN_RATE_PER_IP_PER_MIN = int(os.getenv("LOGIN_RATE_PER_IP_PER_MIN", "10"))
LOGIN_MAX_FAILS = int(os.getenv("LOGIN_MAX_FAILS", "5"))
LOGIN_LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))

_login_ip_buckets = defaultdict(lambda: deque(maxlen=200))   # ip -> recent attempt timestamps
_login_fail_counts = defaultdict(lambda: deque(maxlen=200))  # email -> recent failure timestamps
_login_lockouts: dict = {}                                   # email -> unlock epoch seconds


def _rate_ok(bucket: deque, limit_per_min: int) -> bool:
    now = time.time()
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= limit_per_min:
        return False
    bucket.append(now)
    return True


def _lock_seconds_remaining(email: str) -> int:
    until = _login_lockouts.get(email, 0)
    if until > time.time():
        return int(until - time.time())
    if until:
        _login_lockouts.pop(email, None)
    return 0


def _record_login_failure(email: str) -> None:
    now = time.time()
    fails = _login_fail_counts[email]
    window = LOGIN_LOCKOUT_MINUTES * 60
    while fails and fails[0] < now - window:
        fails.popleft()
    fails.append(now)
    if len(fails) >= LOGIN_MAX_FAILS:
        _login_lockouts[email] = now + window
        fails.clear()


def _clear_login_failures(email: str) -> None:
    _login_fail_counts.pop(email, None)
    _login_lockouts.pop(email, None)


# ── POST /api/login ─────────────────────────────────────────
@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    await _seed_defaults(db)

    # Per-IP throttle: blunts distributed/rapid guessing before we touch the DB.
    client_ip = request.client.host if request and request.client else "unknown"
    if not _rate_ok(_login_ip_buckets[client_ip], LOGIN_RATE_PER_IP_PER_MIN):
        raise HTTPException(status_code=429, detail="Too many login attempts. Please wait a minute and try again.")

    email = req.email.strip().lower()

    # Per-account lockout after repeated failures.
    locked = _lock_seconds_remaining(email)
    if locked:
        raise HTTPException(
            status_code=429,
            detail=f"Account temporarily locked after too many failed attempts. Try again in {locked // 60 + 1} minute(s).",
        )

    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(req.password, user.password_hash):
        _record_login_failure(email)
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.active:
        raise HTTPException(status_code=403, detail="Account deactivated — contact admin")

    if not user.verified:
        raise HTTPException(status_code=403, detail="Email not verified — contact admin")

    # Successful auth — clear any failure/lockout state for this account.
    _clear_login_failures(email)

    # Update last login
    user.last_login = datetime.now(timezone.utc)
    await db.commit()

    token = create_token(user.id, user.email, user.role)

    return AuthResponse(
        token=token,
        email=user.email,
        role=user.role,
        display_name=user.display_name,
        company=user.company or "",
        assigned_merchants=user.assigned_merchants or [],
        tier=user.tier or "free",
        veteran=user.veteran or False,
        must_change_password=user.must_change_password or False,
    )


# ── POST /api/register ──────────────────────────────────────
VERIFICATION_TOKEN_TTL_HOURS = 24


def _make_verification_token() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    return raw, token_hash


async def _is_excluded(db: AsyncSession, email: str, name: str) -> bool:
    result = await db.execute(select(Exclusion))
    for entry in result.scalars().all():
        if entry.email and entry.email.strip().lower() == email:
            return True
        if entry.name and name and entry.name.strip().lower() == name.strip().lower():
            return True
    return False


async def _send_reset_email(user: User, raw_token: str) -> bool:
    link = f"{settings.PORTAL_URL}/?reset_token={raw_token}"
    html = (
        f"<p>Hello {user.display_name or 'there'},</p>"
        f"<p>We received a request to reset your NexusPay password. "
        f"Click below to choose a new password:</p>"
        f'<p><a href="{link}">Reset my password</a></p>'
        f"<p>This link expires in {VERIFICATION_TOKEN_TTL_HOURS} hours and can be used once. "
        f"If you did not request this, you can ignore this email; your password will not change.</p>"
    )
    return await send_email(user.email, "Reset your NexusPay password", html)


async def _send_verification_email(user: User, raw_token: str) -> bool:
    link = f"{settings.PORTAL_URL}/?token={raw_token}"
    html = (
        f"<p>Welcome to NexusPay, {user.display_name or 'there'}.</p>"
        f"<p>Please confirm your email address to activate your account:</p>"
        f'<p><a href="{link}">Verify my email</a></p>'
        f"<p>This link expires in {VERIFICATION_TOKEN_TTL_HOURS} hours. "
        f"If you did not create a NexusPay account, you can ignore this email.</p>"
    )
    return await send_email(user.email, "Verify your NexusPay account", html)


@router.post("/register", status_code=201)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    await _seed_defaults(db)

    email = req.email.strip().lower()

    # Check duplicate
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    if await _is_excluded(db, email, req.name):
        raise HTTPException(status_code=403, detail="This account cannot be created. Please contact support.")

    user = User(
        email=email,
        password_hash=hash_password(req.password),
        display_name=req.name.strip(),
        company=req.company.strip(),
        role="user",
        tier="free",
        veteran=req.veteran,
        active=True,
        verified=False,  # must verify email before login
        created_by="self",
    )
    db.add(user)
    await db.commit()

    await db.refresh(user)
    raw_token, token_hash = _make_verification_token()
    db.add(EmailVerificationToken(
        user_id=user.id,
        token_hash=token_hash,
        purpose="verify",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_TOKEN_TTL_HOURS),
    ))
    await db.commit()
    sent = await _send_verification_email(user, raw_token)
    if not sent:
        return {"message": "Account created, but the verification email could not be sent. Please contact support.", "email": email, "email_sent": False}
    return {"message": "Account created. Check your email to verify your address before logging in.", "email": email, "email_sent": True}


# ── POST /api/change-password ────────────────────────────────
@router.get("/verify-email")
async def verify_email(token: str, db: AsyncSession = Depends(get_db)):
    """Validate an email-verification token and activate the account.

    The raw token from the emailed link is hashed and matched against an unused,
    unexpired row. On success the user's `verified` flag is set True and the
    token is marked used (single-use)."""
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db.execute(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == token_hash,
            EmailVerificationToken.purpose == "verify",
            EmailVerificationToken.used == False,  # noqa: E712
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid or already-used verification link.")
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="This verification link has expired. Please request a new one.")

    user_result = await db.execute(select(User).where(User.id == row.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")

    user.verified = True
    row.used = True
    await db.commit()
    return {"message": "Email verified. You can now log in.", "email": user.email}

class ResendVerificationRequest(BaseModel):
    email: EmailStr


@router.post("/resend-verification")
async def resend_verification(req: ResendVerificationRequest, db: AsyncSession = Depends(get_db)):
    """Re-send a verification link. Always returns the same generic message so
    the endpoint cannot be used to discover which emails have accounts."""
    generic = {"message": "If that account exists and is unverified, a new verification email has been sent."}
    email = req.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or user.verified:
        return generic  # do not reveal whether the account exists / is already verified

    raw_token, token_hash = _make_verification_token()
    db.add(EmailVerificationToken(
        user_id=user.id,
        token_hash=token_hash,
        purpose="verify",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_TOKEN_TTL_HOURS),
    ))
    await db.commit()
    await _send_verification_email(user, raw_token)
    return generic

@router.post("/reset-password")
async def reset_password(req: ResetPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Complete a password reset using a single-use token from the emailed link.

    The raw token is hashed and matched against an unused, unexpired row with
    purpose='reset'. On success the user's password is updated and the token is
    consumed (single-use)."""
    token_hash = hashlib.sha256(req.token.encode()).hexdigest()
    result = await db.execute(
        select(EmailVerificationToken).where(
            EmailVerificationToken.token_hash == token_hash,
            EmailVerificationToken.purpose == "reset",
            EmailVerificationToken.used == False,  # noqa: E712
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=400, detail="Invalid or already-used reset link.")
    if row.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="This reset link has expired. Please request a new one.")

    user_result = await db.execute(select(User).where(User.id == row.user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="Account not found.")

    user.password_hash = hash_password(req.new_password)
    user.must_change_password = False
    row.used = True
    await db.commit()
    return {"message": "Password reset successfully. You can now sign in with your new password."}

@router.post("/change-password")
async def change_password(
    req: ChangePasswordRequest,
    token_data: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Authenticated self-service password change.

    Identity comes from the JWT (token_data["sub"]), never the request body, so a
    caller can only change their OWN password — and only after proving knowledge
    of the current password. This also covers the forced first-login change, where
    the temporary password issued by the admin is the current password.
    """
    result = await db.execute(select(User).where(User.id == token_data["sub"]))
    user = result.scalar_one_or_none()

    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Account not found or inactive")

    if not verify_password(req.current_password, user.password_hash):
        raise HTTPException(status_code=403, detail="Current password is incorrect")

    user.password_hash = hash_password(req.new_password)
    user.must_change_password = False
    await db.commit()

    return {"message": "Password changed successfully"}


# ── GET /api/me ──────────────────────────────────────────────
@router.get("/me", response_model=MeResponse)
async def me(
    token_data: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == token_data["sub"]))
    user = result.scalar_one_or_none()

    if not user or not user.active:
        raise HTTPException(status_code=401, detail="Account not found or inactive")

    return MeResponse(
        email=user.email,
        role=user.role,
        display_name=user.display_name,
        company=user.company or "",
        assigned_merchants=user.assigned_merchants or [],
        tier=user.tier or "free",
        veteran=user.veteran or False,
        must_change_password=user.must_change_password or False,
    )


# ── POST /api/forgot-password ────────────────────────────────
class ForgotPasswordRequest(BaseModel):
    email: EmailStr

@router.post("/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, db: AsyncSession = Depends(get_db)):
    """Issue a single-use, expiring password-reset token and email a reset link."""
    generic = {"message": "If that email exists, a password reset link has been sent."}
    email = req.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()
    if not user or not user.active:
        return generic
    raw_token, token_hash = _make_verification_token()
    db.add(EmailVerificationToken(
        user_id=user.id,
        token_hash=token_hash,
        purpose="reset",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=VERIFICATION_TOKEN_TTL_HOURS),
    ))
    await db.commit()
    await _send_reset_email(user, raw_token)
    return generic
