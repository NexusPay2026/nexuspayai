"""
Statements router - TIER 1.
Admin-only read API backing the internal dashboard Statements tab
(dashboard.nexuspayservices.com). Single source of truth: reads the same
Merchant/AuditJob records written by /api/audit/run. Access is restricted
to admin (you only, for now). Every read is recorded in the append-only log.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_

from app.database import get_db
from app.models import Merchant, AuditJob
from app.services.auth_service import get_current_user
from app.services.r2_storage import generate_presigned_download_url
from app.services import audit_log

router = APIRouter()


def _require_admin(user: dict):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Statements are restricted to NexusPay executives.")


def _summary(m: Merchant) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "processor": m.processor,
        "merchant_number": m.merchant_number,
        "statement_date": m.statement_date,
        "statement_month": m.statement_month,
        "monthly_volume": m.monthly_volume,
        "total_fees": m.total_fees,
        "effective_rate": m.effective_rate,
        "transaction_count": m.transaction_count,
        "risk_score": m.risk_score,
        "agree_pct": m.agree_pct,
        "provider_count": m.provider_count,
        "providers": m.providers or [],
        "page_count": m.page_count,
        "file_hash": m.file_hash,
        "is_revision": m.is_revision,
        "revision_group": m.revision_group,
        "uploaded_via": m.uploaded_via,
        "uploaded_by": m.added_by,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


# -- GET /api/statements -------------------------------------
@router.get("/statements")
async def list_statements(
    request: Request,
    q: Optional[str] = Query(None, description="search name / processor / MID"),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)

    stmt = select(Merchant).where(Merchant.file_hash.isnot(None))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(
            Merchant.name.ilike(like),
            Merchant.processor.ilike(like),
            Merchant.merchant_number.ilike(like),
        ))
    stmt = stmt.order_by(Merchant.created_at.desc()).limit(limit).offset(offset)

    rows = (await db.execute(stmt)).scalars().all()

    ip = request.client.host if request and request.client else None
    ua = request.headers.get("user-agent") if request else None
    await audit_log.record(
        db, action="view", actor=user, entity_type="statement_list",
        ip_address=ip, user_agent=ua, detail={"count": len(rows), "q": q or ""},
        commit=True,
    )

    return {"count": len(rows), "statements": [_summary(m) for m in rows]}


# -- GET /api/statements/{merchant_id} -----------------------
@router.get("/statements/{merchant_id}")
async def get_statement(
    merchant_id: str,
    request: Request,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    _require_admin(user)

    m = (await db.execute(select(Merchant).where(Merchant.id == merchant_id))).scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Statement not found")

    # Presigned, time-limited view links for each stored file
    files = []
    for fmeta in (m.files or []):
        url = None
        if fmeta.get("r2_key"):
            url = await generate_presigned_download_url(fmeta["r2_key"], expires=900)
        files.append({**{k: v for k, v in fmeta.items() if k != "r2_key"}, "view_url": url})

    job = (await db.execute(
        select(AuditJob).where(AuditJob.merchant_id == merchant_id, AuditJob.status == "complete")
        .order_by(AuditJob.created_at.desc())
    )).scalars().first()

    ip = request.client.host if request and request.client else None
    ua = request.headers.get("user-agent") if request else None
    await audit_log.record(
        db, action="view", actor=user, entity_type="statement",
        entity_id=merchant_id, file_hash=m.file_hash, ip_address=ip, user_agent=ua,
        commit=True,
    )

    detail = _summary(m)
    detail["files"] = files
    detail["line_items"] = m.line_items or []
    detail["findings"] = m.findings or []
    detail["consensus"] = job.consensus_data if job else None
    detail["confidence"] = job.confidence if job else None
    return detail