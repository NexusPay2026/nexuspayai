"""
Auth surface smoke tests — the crown jewels.

Covers: login success/failure, per-account lockout, unverified block, register
happy-path + exclusion enforcement, /api/me identity, admin-only RBAC, and
password-reset enumeration safety + single-use/expiry token rules.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from conftest import make_user, make_exclusion, make_token_row, token_for, IS_SQLITE
from app.models import User, EmailVerificationToken
from sqlalchemy import select

LOGIN = "/api/login"

# The token-expiry comparison in auth.py (row.expires_at < now(tz)) relies on
# tz-aware DateTime storage, which only Postgres provides — sqlite reads back
# naive datetimes and the comparison raises. These tests run against the
# Postgres service container in CI.
requires_pg_datetime = pytest.mark.skipif(
    IS_SQLITE,
    reason="token-expiry comparison needs Postgres tz-aware datetimes (runs in CI)",
)


# ── Login ───────────────────────────────────────────────────────────────────
async def test_login_success_returns_jwt(client, db):
    await make_user(db, email="alice@example.com", password="Secret123!", role="employee")
    r = await client.post(LOGIN, json={"email": "alice@example.com", "password": "Secret123!"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"] and body["token"].count(".") == 2  # header.payload.signature
    assert body["email"] == "alice@example.com"
    assert body["role"] == "employee"


async def test_login_wrong_password_401(client, db):
    await make_user(db, email="bob@example.com", password="RightPass123!")
    r = await client.post(LOGIN, json={"email": "bob@example.com", "password": "WrongPass123!"})
    assert r.status_code == 401
    assert "Invalid" in r.json()["detail"]


async def test_login_unknown_user_401(client, db):
    r = await client.post(LOGIN, json={"email": "nobody@example.com", "password": "whatever123"})
    assert r.status_code == 401


async def test_unverified_account_cannot_login(client, db):
    await make_user(db, email="unverified@example.com", password="Secret123!", verified=False)
    r = await client.post(LOGIN, json={"email": "unverified@example.com", "password": "Secret123!"})
    assert r.status_code == 403
    assert "verif" in r.json()["detail"].lower()


async def test_lockout_triggers_after_configured_failures(client, db):
    """After LOGIN_MAX_FAILS (default 5) wrong-password attempts the account is
    locked and further attempts return 429 — even with the correct password."""
    from app.routers import auth as auth_router

    await make_user(db, email="target@example.com", password="Correct123!")
    for _ in range(auth_router.LOGIN_MAX_FAILS):
        r = await client.post(LOGIN, json={"email": "target@example.com", "password": "bad-guess"})
        assert r.status_code == 401

    # Next attempt is locked out — the correct password does not help.
    r = await client.post(LOGIN, json={"email": "target@example.com", "password": "Correct123!"})
    assert r.status_code == 429
    assert "lock" in r.json()["detail"].lower()


# ── Register ─────────────────────────────────────────────────────────────────
async def test_register_happy_path_creates_unverified_user(client, db):
    r = await client.post(
        "/api/register",
        json={"name": "Carol New", "email": "carol@example.com", "password": "Password123!", "company": "Acme"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["email"] == "carol@example.com"

    row = (await db.execute(select(User).where(User.email == "carol@example.com"))).scalar_one_or_none()
    assert row is not None
    assert row.verified is False  # must verify email before login
    assert row.role == "user"


async def test_register_blocks_excluded_email(client, db):
    await make_exclusion(db, email="banned@example.com")
    r = await client.post(
        "/api/register",
        json={"name": "Banned Person", "email": "banned@example.com", "password": "Password123!"},
    )
    assert r.status_code == 403
    # No account should have been created.
    row = (await db.execute(select(User).where(User.email == "banned@example.com"))).scalar_one_or_none()
    assert row is None


async def test_register_blocks_excluded_name(client, db):
    await make_exclusion(db, name="Hunter Sohl")
    r = await client.post(
        "/api/register",
        json={"name": "hunter sohl", "email": "someone@example.com", "password": "Password123!"},
    )
    assert r.status_code == 403


# ── /api/me ──────────────────────────────────────────────────────────────────
async def test_me_with_valid_token_returns_role(client, db):
    user = await make_user(db, email="me@example.com", role="admin")
    r = await client.get("/api/me", headers={"Authorization": f"Bearer {token_for(user)}"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "admin"
    assert r.json()["email"] == "me@example.com"


async def test_me_with_garbage_token_401(client):
    r = await client.get("/api/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


async def test_me_without_token_401(client):
    r = await client.get("/api/me")
    assert r.status_code == 401


# ── Admin-only RBAC ──────────────────────────────────────────────────────────
async def test_admin_route_rejects_employee_and_client(client, db):
    employee = await make_user(db, email="emp@example.com", role="employee")
    clientu = await make_user(db, email="cli@example.com", role="client")

    for u in (employee, clientu):
        r = await client.get("/api/users", headers={"Authorization": f"Bearer {token_for(u)}"})
        assert r.status_code == 403, f"{u.role} should be forbidden, got {r.status_code}"


async def test_admin_route_allows_admin(client, db):
    admin = await make_user(db, email="admin@example.com", role="admin")
    r = await client.get("/api/users", headers={"Authorization": f"Bearer {token_for(admin)}"})
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)


# ── Password reset ───────────────────────────────────────────────────────────
async def test_forgot_password_is_enumeration_safe(client, db):
    await make_user(db, email="exists@example.com")

    r_exists = await client.post("/api/forgot-password", json={"email": "exists@example.com"})
    r_missing = await client.post("/api/forgot-password", json={"email": "ghost@example.com"})

    assert r_exists.status_code == 200
    assert r_missing.status_code == 200
    # Identical generic response regardless of whether the account exists.
    assert r_exists.json() == r_missing.json()


async def test_reset_with_consumed_token_fails(client, db):
    user = await make_user(db, email="reset1@example.com")
    raw = "consumed-token-raw"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    await make_token_row(
        db,
        user_id=user.id,
        token_hash=token_hash,
        purpose="reset",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        used=True,  # already consumed
    )
    r = await client.post("/api/reset-password", json={"token": raw, "new_password": "BrandNew123!"})
    assert r.status_code == 400
    assert "already-used" in r.json()["detail"] or "Invalid" in r.json()["detail"]


@requires_pg_datetime
async def test_reset_with_expired_token_fails(client, db):
    user = await make_user(db, email="reset2@example.com")
    raw = "expired-token-raw"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    await make_token_row(
        db,
        user_id=user.id,
        token_hash=token_hash,
        purpose="reset",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),  # already expired
        used=False,
    )
    r = await client.post("/api/reset-password", json={"token": raw, "new_password": "BrandNew123!"})
    assert r.status_code == 400
    assert "expired" in r.json()["detail"].lower()


@requires_pg_datetime
async def test_reset_with_valid_token_succeeds_and_is_single_use(client, db):
    user = await make_user(db, email="reset3@example.com", password="OldPass123!")
    raw = "good-token-raw"
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    await make_token_row(
        db,
        user_id=user.id,
        token_hash=token_hash,
        purpose="reset",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    ok = await client.post("/api/reset-password", json={"token": raw, "new_password": "FreshPass123!"})
    assert ok.status_code == 200, ok.text

    # Token is consumed → a second use fails.
    again = await client.post("/api/reset-password", json={"token": raw, "new_password": "Another123!"})
    assert again.status_code == 400

    # New password now works for login.
    login = await client.post(LOGIN, json={"email": "reset3@example.com", "password": "FreshPass123!"})
    assert login.status_code == 200, login.text
