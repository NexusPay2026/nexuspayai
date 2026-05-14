"""
Auth router — login, register, password management, /api/me.
Matches the frontend's existing _api() call signatures exactly.
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User
from app.schemas import (
    LoginRequest, RegisterRequest, ChangePasswordRequest,
    AuthResponse, MeResponse,
)
from pydantic import BaseModel, EmailStr
from app.services.auth_service import (
    hash_password, verify_password, create_token,
    get_current_user,
)
from app.config import settings

router = APIRouter()


# ── Seed admin + demo on first call ─────────────────────────
_seeded = False

async def _seed_defaults(db: AsyncSession):
    global _seeded
    if _seeded:
        return
    _seeded = True

    # Admin
    result = await db.execute(select(User).where(User.email == settings.ADMIN_EMAIL))
    if not result.scalar_one_or_none():
        admin = User(
            email=settings.ADMIN_EMAIL,
            password_hash=hash_password("NexusPay2026!"),
            display_name="Admin",
            company="NexusPay Services",
            role="admin",
            tier="enterprise",
            active=True,
            verified=True,
            created_by="system",
        )
        db.add(admin)

    # Demo
    result = await db.execute(select(User).where(User.email == "demo@nexuspayservices.com"))
    if not result.scalar_one_or_none():
        demo = User(
            email="demo@nexuspayservices.com",
            password_hash=hash_password("Demo2026!"),
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


# ── POST /api/login ─────────────────────────────────────────
@router.post("/login", response_model=AuthResponse)
async def login(req: LoginRequest, db: AsyncSession = Depends(get_db)):
    await _seed_defaults(db)

    email = req.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.active:
        raise HTTPException(status_code=403, detail="Account deactivated — contact admin")

    if not user.verified:
        raise HTTPException(status_code=403, detail="Email not verified — contact admin")

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
@router.post("/register", status_code=201)
async def register(req: RegisterRequest, db: AsyncSession = Depends(get_db)):
    await _seed_defaults(db)

    email = req.email.strip().lower()

    # Check duplicate
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    user = User(
        email=email,
        password_hash=hash_password(req.password),
        display_name=req.name.strip(),
        company=req.company.strip(),
        role="user",
        tier="free",
        veteran=req.veteran,
        active=True,
        verified=True,  # Auto-verify for now; add email verification later
        created_by="self",
    )
    db.add(user)
    await db.commit()

    return {"message": "Account created successfully", "email": email}


# ── POST /api/change-password ────────────────────────────────
@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, db: AsyncSession = Depends(get_db)):
    email = req.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

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
    """Generates a reset — for now returns success regardless (prevents email enumeration)."""
    email = req.email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # Always return success to prevent email enumeration
    if not user:
        return {"message": "If that email exists, a reset code has been sent."}

    # In production, this would send an email with a reset token.
    # For now, the frontend handles reset codes client-side.
    return {"message": "If that email exists, a reset code has been sent."}
