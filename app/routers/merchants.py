"""
Merchants router — CRUD for audited merchant records.
Matches frontend calls: GET/POST /api/merchants, DELETE /api/merchants/{id}
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import Merchant, User
from app.schemas import MerchantCreate, MerchantUpdate, MerchantResponse
from app.services.auth_service import get_current_user

router = APIRouter()


def _compute_rates(m: Merchant):
    """Recalculate derived rate fields."""
    if m.monthly_volume and m.monthly_volume > 0:
        if m.total_fees:
            m.effective_rate = round((m.total_fees / m.monthly_volume) * 100, 4)
        if m.interchange_cost:
            m.interchange_rate = round((m.interchange_cost / m.monthly_volume) * 100, 4)
        if m.processor_markup:
            m.markup_rate = round((m.processor_markup / m.monthly_volume) * 100, 4)


# ── GET /api/merchants ──────────────────────────────────────
@router.get("/merchants", response_model=list[MerchantResponse])
async def list_merchants(
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    role = user.get("role", "user")
    email = user.get("email", "")

    if role in ("admin", "employee"):
        result = await db.execute(select(Merchant).order_by(Merchant.created_at.desc()))
    elif role == "demo":
        result = await db.execute(
            select(Merchant).where(Merchant.is_demo == True).order_by(Merchant.created_at.desc())
        )
    elif role == "client":
        # Client sees only their own merchants + assigned ones
        db_user = await db.execute(select(User).where(User.email == email))
        u = db_user.scalar_one_or_none()
        assigned = u.assigned_merchants if u else []
        result = await db.execute(
            select(Merchant).where(
                (Merchant.owner_email == email) | (Merchant.id.in_(assigned))
            ).order_by(Merchant.created_at.desc())
        )
    else:
        result = await db.execute(
            select(Merchant).where(Merchant.owner_email == email).order_by(Merchant.created_at.desc())
        )

    merchants = result.scalars().all()
    return [MerchantResponse(
        id=m.id, name=m.name, processor=m.processor,
        statement_month=m.statement_month, monthly_volume=m.monthly_volume,
        total_fees=m.total_fees, interchange_cost=m.interchange_cost,
        processor_markup=m.processor_markup, monthly_fees=m.monthly_fees,
        transaction_count=m.transaction_count, credit_card_pct=m.credit_card_pct,
        avg_ticket=m.avg_ticket, effective_rate=m.effective_rate,
        interchange_rate=m.interchange_rate, markup_rate=m.markup_rate,
        risk_score=m.risk_score, line_items=m.line_items or [],
        findings=m.findings or [], owner_email=m.owner_email or "",
        is_demo=m.is_demo or False, added_by=m.added_by or "",
        created_at=m.created_at,
    ) for m in merchants]


# ── POST /api/merchants ─────────────────────────────────────
@router.post("/merchants", status_code=201)
async def create_merchant(
    req: MerchantCreate,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    m = Merchant(
        name=req.name,
        processor=req.processor,
        statement_month=req.statement_month,
        monthly_volume=req.monthly_volume,
        total_fees=req.total_fees,
        interchange_cost=req.interchange_cost,
        processor_markup=req.processor_markup,
        monthly_fees=req.monthly_fees,
        transaction_count=req.transaction_count,
        credit_card_pct=req.credit_card_pct,
        avg_ticket=req.avg_ticket,
        risk_score=req.risk_score,
        line_items=req.line_items,
        findings=req.findings,
        owner_email=user.get("email", ""),
        added_by=user.get("email", ""),
        is_demo=(user.get("role") == "demo"),
    )
    _compute_rates(m)

    # Link to user record if possible
    result = await db.execute(select(User).where(User.email == user.get("email")))
    db_user = result.scalar_one_or_none()
    if db_user:
        m.owner_id = db_user.id

    db.add(m)
    await db.commit()
    await db.refresh(m)

    return {"id": m.id, "message": "Merchant created"}


# ── PUT /api/merchants/{id} ─────────────────────────────────
@router.put("/merchants/{merchant_id}")
async def update_merchant(
    merchant_id: str,
    req: MerchantUpdate,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Merchant).where(Merchant.id == merchant_id))
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found")

    # Only admin/employee or owner can edit
    role = user.get("role", "user")
    if role not in ("admin", "employee") and m.owner_email != user.get("email"):
        raise HTTPException(status_code=403, detail="Not authorized to edit this merchant")

    for field, value in req.dict(exclude_unset=True).items():
        setattr(m, field, value)
    _compute_rates(m)

    await db.commit()
    return {"id": m.id, "message": "Merchant updated"}


# ── DELETE /api/merchants/{id} ──────────────────────────────
@router.delete("/merchants/{merchant_id}")
async def delete_merchant(
    merchant_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.get("role") not in ("admin",):
        raise HTTPException(status_code=403, detail="Only admins can delete merchants")

    result = await db.execute(select(Merchant).where(Merchant.id == merchant_id))
    m = result.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Merchant not found")

    await db.delete(m)
    await db.commit()
    return {"message": "Merchant deleted"}
