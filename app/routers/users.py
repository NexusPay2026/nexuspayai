"""
Users router — admin-only user management (onboard, edit, deactivate, reset password).
Hardened for AsyncSession integrity.
"""

from datetime import datetime, timezone
import secrets
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import User
from app.schemas import UserCreate, UserUpdate, UserResponse, ResetPasswordRequest
from app.services.auth_service import (
    hash_password, get_current_user, require_role,
)

router = APIRouter()

# ── GET /api/users ──────────────────────────────────────────
@router.get("/users", response_model=list[UserResponse])
async def list_users(
    user: dict = Depends(require_role("admin", "employee")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return [UserResponse(
        id=u.id, email=u.email, display_name=u.display_name,
        company=u.company or "", role=u.role, tier=u.tier or "free",
        veteran=u.veteran or False, active=u.active,
        verified=u.verified, must_change_password=u.must_change_password or False,
        assigned_merchants=u.assigned_merchants or [],
        created_by=u.created_by or "", created_at=u.created_at, last_login=u.last_login,
    ) for u in users]

# ── POST /api/users (Admin Onboard) ────────────────────────
@router.post("/users", status_code=201)
@router.post("/users/onboard", status_code=201)
async def create_user(
    req: UserCreate,
    user: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    email = req.email.strip().lower()

    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already registered")

    new_user = User(
        email=email,
        password_hash=hash_password(req.password),
        display_name=req.name.strip(),
        company=req.company.strip(),
        role=req.role,
        tier=req.tier or "free",
        veteran=req.veteran,
        active=True,
        verified=True,
        must_change_password=True,
        assigned_merchants=req.assigned_merchants,
        created_by=user.get("email", "admin"),
    )
    
    try:
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)
        return {
            "id": new_user.id,
            "email": new_user.email,
            "display_name": new_user.display_name,
            "role": new_user.role,
            "message": "User created — must change password on first login",
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create user: {str(e)}")

# ── PUT /api/users/{id} ─────────────────────────────────────
@router.put("/users/{user_id}")
async def update_user(
    user_id: str,
    req: UserUpdate,
    admin: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    try:
        for field, value in req.dict(exclude_unset=True).items():
            setattr(u, field, value)
        u.updated_at = datetime.now(timezone.utc)
        await db.commit()
        return {"id": u.id, "message": "User updated"}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Update failed: {str(e)}")

# ── DELETE /api/users/{id} (Hardened) ───────────────────────
@router.delete("/users/{user_id}")
async def delete_user(
    user_id: str,
    admin: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    u = result.scalar_one_or_none()
    
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    if u.email == "admin@nexuspayservices.com":
        raise HTTPException(status_code=403, detail="Cannot delete the primary admin")

    try:
        # Atomic delete operation
        await db.delete(u)
        await db.flush() # Forces the DB to acknowledge the delete before the commit
        await db.commit()
        return {"message": "User deleted successfully from database"}
    except Exception as e:
        await db.rollback()
        # This will now tell the UI EXACTLY why it failed (e.g. linked records)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail=f"Database Integrity Error: {str(e)}"
        )

# ── POST /api/users/{id}/reset-password ─────────────────────
@router.post("/users/{user_id}/reset-password")
async def reset_user_password(
    user_id: str,
    admin: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    u = result.scalar_one_or_none()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")

    temp_pass = "NexusPay" + secrets.token_urlsafe(6) + "!"
    
    try:
        u.password_hash = hash_password(temp_pass)
        u.must_change_password = True
        await db.commit()
        return {
            "message": "Password reset — user must change on next login",
            "display_name": u.display_name,
            "temp_password": temp_pass,
        }
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Reset failed: {str(e)}")