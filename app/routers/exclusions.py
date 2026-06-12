"""
Exclusions router - admin-only management of the onboarding exclusion list.
People on this list cannot be onboarded as users of any role.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Exclusion
from app.services.auth_service import require_role

router = APIRouter()


@router.get("/exclusions")
async def list_exclusions(
    user: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Exclusion).order_by(Exclusion.created_at.desc()))
    rows = result.scalars().all()
    return [{
        "id": r.id, "name": r.name, "email": r.email,
        "reason": r.reason, "added_by": r.added_by,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.post("/exclusions", status_code=201)
async def add_exclusion(
    body: dict,
    user: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    if not name and not email:
        raise HTTPException(status_code=400, detail="Provide a name or an email to exclude.")
    entry = Exclusion(
        name=name, email=email,
        reason=(body.get("reason") or "").strip(),
        added_by=user.get("email", ""),
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return {"id": entry.id, "name": entry.name, "email": entry.email}


@router.delete("/exclusions/{exclusion_id}")
async def remove_exclusion(
    exclusion_id: str,
    user: dict = Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Exclusion).where(Exclusion.id == exclusion_id))
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Exclusion not found")
    await db.delete(entry)
    await db.commit()
    return {"deleted": exclusion_id}