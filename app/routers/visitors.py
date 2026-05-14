"""
Visitors / Leads router — captures data from:
  - Landing page form (freeanalysis.nexuspayservices.com)
  - Website contact form (nexuspayservices.com)
  - Webhook endpoint (backward compatible with nexuspay-webhook-main)
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func

from app.database import get_db
from app.models import Visitor
from app.schemas import VisitorPayload, VisitorResponse, ContactPayload
from app.services.auth_service import require_role
from app.config import settings

router = APIRouter()


def _get_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else ""


# ── POST /webhook/visitor  (backward compatible with old webhook) ─
@router.post("/webhook/visitor", response_model=VisitorResponse, status_code=201)
async def capture_visitor_webhook(
    payload: VisitorPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    # Optional webhook secret check
    if settings.WEBHOOK_SECRET:
        auth = request.headers.get("X-Webhook-Secret", "")
        if auth != settings.WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")

    visitor = Visitor(
        full_name=payload.full_name,
        business_name=payload.business_name,
        email=payload.email,
        phone=payload.phone or None,
        source=payload.source or "landing_page",
        ip_address=_get_ip(request),
        user_agent=request.headers.get("User-Agent"),
        referrer=payload.referrer,
        page_url=payload.page_url,
        utm_source=payload.utm_source,
        utm_medium=payload.utm_medium,
        utm_campaign=payload.utm_campaign,
        utm_term=payload.utm_term,
        utm_content=payload.utm_content,
        session_duration_ms=payload.session_duration_ms,
        ai_business_type=payload.ai_business_type,
        message=payload.message,
    )
    db.add(visitor)
    await db.commit()
    await db.refresh(visitor)

    return VisitorResponse(id=visitor.id)


# ── POST /api/leads/contact  (website contact form) ─────────
@router.post("/api/leads/contact", status_code=201)
async def contact_form(
    payload: ContactPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    visitor = Visitor(
        full_name=payload.full_name,
        business_name=payload.business_name or "Not provided",
        email=payload.email,
        phone=payload.phone or None,
        source="website",
        ip_address=_get_ip(request),
        user_agent=request.headers.get("User-Agent"),
        message=payload.message or None,
    )
    db.add(visitor)
    await db.commit()
    await db.refresh(visitor)

    return {"id": visitor.id, "message": "Contact form submitted"}


# ── POST /api/audit/intake  (landing page CTA) ──────────────
@router.post("/api/audit/intake", status_code=201)
async def audit_intake(
    payload: VisitorPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Landing page 'Get My Free Analysis' — captures lead, redirects to portal signup."""
    visitor = Visitor(
        full_name=payload.full_name,
        business_name=payload.business_name,
        email=payload.email,
        phone=payload.phone or None,
        source="landing_page_cta",
        ip_address=_get_ip(request),
        user_agent=request.headers.get("User-Agent"),
        referrer=payload.referrer,
        page_url=payload.page_url,
        utm_source=payload.utm_source,
        utm_medium=payload.utm_medium,
        utm_campaign=payload.utm_campaign,
        session_duration_ms=payload.session_duration_ms,
        ai_business_type=payload.ai_business_type,
    )
    db.add(visitor)
    await db.commit()
    await db.refresh(visitor)

    return {
        "id": visitor.id,
        "redirect_to": f"{settings.PORTAL_URL}?signup=true&email={payload.email}",
        "message": "Lead captured — redirect to portal signup",
    }


# ── GET /admin/visitors ─────────────────────────────────────
@router.get("/admin/visitors")
async def list_visitors(
    limit: int = 50,
    offset: int = 0,
    user: dict = Depends(require_role("admin", "employee")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Visitor).order_by(Visitor.created_at.desc()).limit(limit).offset(offset)
    )
    visitors = result.scalars().all()
    return [
        {
            "id": v.id, "full_name": v.full_name, "business_name": v.business_name,
            "email": v.email, "phone": v.phone, "source": v.source,
            "ip_address": v.ip_address, "referrer": v.referrer,
            "utm_source": v.utm_source, "utm_medium": v.utm_medium,
            "utm_campaign": v.utm_campaign, "page_url": v.page_url,
            "session_duration_ms": v.session_duration_ms,
            "ai_business_type": v.ai_business_type, "message": v.message,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in visitors
    ]


# ── GET /admin/visitors/count ────────────────────────────────
@router.get("/admin/visitors/count")
async def visitor_count(
    user: dict = Depends(require_role("admin", "employee")),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(func.count(Visitor.id)))
    count = result.scalar()
    return {"total_visitors": count}


# ── GET /admin/dashboard/stats ──────────────────────────────
# Aggregated stats for the visitor tracking dashboard
@router.get("/admin/dashboard/stats")
async def dashboard_stats(
    user: dict = Depends(require_role("admin", "employee")),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import cast, Date

    # Total visitors
    total_result = await db.execute(select(func.count(Visitor.id)))
    total = total_result.scalar() or 0

    # By source
    source_result = await db.execute(
        select(Visitor.source, func.count(Visitor.id))
        .group_by(Visitor.source)
    )
    by_source = {row[0] or "unknown": row[1] for row in source_result.all()}

    # By UTM source (top 10)
    utm_result = await db.execute(
        select(Visitor.utm_source, func.count(Visitor.id))
        .where(Visitor.utm_source.isnot(None))
        .group_by(Visitor.utm_source)
        .order_by(func.count(Visitor.id).desc())
        .limit(10)
    )
    by_utm = {row[0]: row[1] for row in utm_result.all()}

    # Visitors per day (last 30 days)
    daily_result = await db.execute(
        select(
            cast(Visitor.created_at, Date).label("day"),
            func.count(Visitor.id),
        )
        .group_by("day")
        .order_by("day")
        .limit(30)
    )
    daily = [{"date": str(row[0]), "count": row[1]} for row in daily_result.all()]

    # Most recent 10 visitors
    recent_result = await db.execute(
        select(Visitor).order_by(Visitor.created_at.desc()).limit(10)
    )
    recent = [
        {
            "id": v.id, "full_name": v.full_name, "email": v.email,
            "business_name": v.business_name, "source": v.source,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in recent_result.scalars().all()
    ]

    return {
        "total_visitors": total,
        "by_source": by_source,
        "by_utm_source": by_utm,
        "daily_visitors": daily,
        "recent_visitors": recent,
    }

