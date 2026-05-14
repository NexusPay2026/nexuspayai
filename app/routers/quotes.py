"""
Pricing Quote endpoints.
Admin and Employee roles only.
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Quote
from app.schemas import QuoteCreate, QuoteResponse, QuoteListResponse
from app.services.auth_service import get_current_user
from app.services.quote_pdf import generate_quote_pdf, upload_quote_pdf

router = APIRouter()

ALLOWED_ROLES = ["admin", "employee"]


def require_internal(user: dict):
    if user.get("role") not in ALLOWED_ROLES:
        raise HTTPException(status_code=403, detail="Pricing quotes are restricted to internal staff")


@router.post("/quotes", response_model=QuoteResponse)
async def create_quote(
    payload: QuoteCreate,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_internal(user)

    quote = Quote(
        created_by=user["sub"],
        created_by_role=user.get("role", "employee"),
        merchant_name=payload.merchant_name or "",
        vertical=payload.vertical,
        risk_level=payload.risk_level,
        volume=payload.volume,
        transactions=payload.transactions,
        markup_pct=payload.markup_pct,
        auth_sell=payload.auth_sell,
        avs_sell=payload.avs_sell,
        batch_sell=payload.batch_sell,
        monthly_sell=payload.monthly_sell,
        transarmor_sell=payload.transarmor_sell,
        pci_sell=payload.pci_sell,
        has_amex=payload.has_amex,
        amex_volume=payload.amex_volume,
        use_gateway=payload.use_gateway,
        # Beacon
        beacon_trad_residual=payload.results.get("beacon_trad_residual", 0),
        beacon_trad_margin=payload.results.get("beacon_trad_margin", 0),
        beacon_flex_residual=payload.results.get("beacon_flex_residual", 0),
        beacon_flex_margin=payload.results.get("beacon_flex_margin", 0),
        # North (optional — 0 if old frontend)
        north_residual=payload.results.get("north_residual", 0),
        north_margin=payload.results.get("north_margin", 0),
        # Kurv / EMS (optional — 0 if old frontend)
        kurv_residual=payload.results.get("kurv_residual", 0),
        kurv_margin=payload.results.get("kurv_margin", 0),
        # Maverick
        maverick_residual=payload.results.get("maverick_residual", 0),
        maverick_tnr=payload.results.get("maverick_tnr", 0),
        maverick_risk=payload.risk_level,
        # Best across all processors
        best_program=payload.results.get("best_program", ""),
        best_residual=payload.results.get("best_residual", 0),
        notes=payload.notes or "",
        status="draft",
    )

    db.add(quote)
    await db.commit()
    await db.refresh(quote)

    pdf_url = None
    try:
        pdf_bytes = generate_quote_pdf(quote)
        pdf_url = await upload_quote_pdf(quote.id, pdf_bytes)
        if pdf_url:
            quote.pdf_url = pdf_url
            await db.commit()
    except Exception:
        pass

    return _to_response(quote)


@router.get("/quotes", response_model=QuoteListResponse)
async def list_quotes(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    vertical: str = Query(None),
    status: str = Query(None),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_internal(user)

    query = select(Quote).order_by(desc(Quote.created_at))

    if vertical:
        query = query.where(Quote.vertical == vertical)
    if status:
        query = query.where(Quote.status == status)

    if user.get("role") == "employee":
        query = query.where(Quote.created_by == user["sub"])

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    quotes = result.scalars().all()

    return QuoteListResponse(
        quotes=[_to_response(q) for q in quotes],
        total=len(quotes),
    )


@router.get("/quotes/{quote_id}", response_model=QuoteResponse)
async def get_quote(
    quote_id: int,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_internal(user)

    result = await db.execute(select(Quote).where(Quote.id == quote_id))
    quote = result.scalar_one_or_none()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    if user.get("role") == "employee" and quote.created_by != user["sub"]:
        raise HTTPException(status_code=403, detail="Access denied")

    return _to_response(quote)


@router.put("/quotes/{quote_id}/status")
async def update_quote_status(
    quote_id: int,
    new_status: str = Query(..., pattern="^(draft|sent|accepted|rejected|expired)$"),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_internal(user)

    result = await db.execute(select(Quote).where(Quote.id == quote_id))
    quote = result.scalar_one_or_none()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    quote.status = new_status
    await db.commit()

    return {"message": f"Quote {quote_id} status updated to {new_status}"}


@router.delete("/quotes/{quote_id}")
async def delete_quote(
    quote_id: int,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    result = await db.execute(select(Quote).where(Quote.id == quote_id))
    quote = result.scalar_one_or_none()
    if not quote:
        raise HTTPException(status_code=404, detail="Quote not found")

    await db.delete(quote)
    await db.commit()

    return {"message": f"Quote {quote_id} deleted"}


def _to_response(q: Quote) -> QuoteResponse:
    return QuoteResponse(
        id=q.id,
        created_by=q.created_by,
        created_at=q.created_at.isoformat() if q.created_at else "",
        merchant_name=q.merchant_name or "",
        vertical=q.vertical,
        risk_level=q.risk_level,
        volume=q.volume,
        transactions=q.transactions,
        markup_pct=q.markup_pct,
        auth_sell=q.auth_sell,
        avs_sell=q.avs_sell,
        batch_sell=q.batch_sell,
        monthly_sell=q.monthly_sell,
        transarmor_sell=q.transarmor_sell,
        pci_sell=q.pci_sell,
        has_amex=q.has_amex,
        amex_volume=q.amex_volume,
        use_gateway=q.use_gateway,
        beacon_trad_residual=q.beacon_trad_residual,
        beacon_trad_margin=q.beacon_trad_margin,
        beacon_flex_residual=q.beacon_flex_residual,
        beacon_flex_margin=q.beacon_flex_margin,
        north_residual=q.north_residual or 0,
        north_margin=q.north_margin or 0,
        kurv_residual=q.kurv_residual or 0,
        kurv_margin=q.kurv_margin or 0,
        maverick_residual=q.maverick_residual,
        maverick_tnr=q.maverick_tnr,
        maverick_risk=q.maverick_risk,
        best_program=q.best_program,
        best_residual=q.best_residual,
        notes=q.notes or "",
        status=q.status,
        pdf_url=q.pdf_url or "",
    )


# ── AI-POWERED DEAL ANALYSIS ─────────────────────────────────
from app.services.pricing_ai import analyze_deal, validate_pricing


@router.post("/quotes/analyze")
async def analyze_quote(
    payload: QuoteCreate,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    require_internal(user)

    role = user.get("role", "employee")
    risk = payload.risk_level or "low"

    # Calculate residuals server-side
    s = payload
    vol = s.volume or 0
    tx = s.transactions or 0
    mu = s.markup_pct or 0
    au = s.auth_sell or 0

    # Beacon Traditional
    bt_rev = vol*(mu/100) + tx*au + tx*s.avs_sell + 30*s.batch_sell + s.monthly_sell + s.transarmor_sell
    bt_cost = vol*0.0002 + tx*0.04 + tx*0.04 + 30*0.04 + 10 + 5
    bt_res = (bt_rev - bt_cost) * 0.75

    # Beacon Flex
    bf_res = (vol*(mu/100)) * 0.50

    # North  (buy: sp=0.0002, au=0.025, ba=0.05, mo=5 | split: 70%)
    nt_rev = vol*(mu/100) + tx*au + tx*s.avs_sell + 30*s.batch_sell + s.monthly_sell
    nt_cost = vol*0.0002 + tx*0.025 + 30*0.05 + 5
    nt_mg = nt_rev - nt_cost
    nt_res = nt_mg * 0.70

    # Kurv / EMS  (buy: sp=0.0002, au=0.04/0.10high, ba=0.05, mo=5 | split: 80% low/mod, 50% high)
    kv_rev = vol*(mu/100) + tx*au + tx*s.avs_sell + 30*s.batch_sell + s.monthly_sell + s.pci_sell
    kv_au = 0.10 if risk == "high" else 0.04
    kv_cost = vol*0.0002 + tx*kv_au + 30*0.05 + 5
    kv_mg = kv_rev - kv_cost
    kv_split = 0.50 if risk == "high" else 0.80
    kv_res = kv_mg * kv_split

    # Maverick
    mv_rates = {"low":(0.0275,0.0002,0.01,0.01,10,5,0.90),
                "moderate":(0.04,0.0002,0.03,0.025,10,5,0.80),
                "high":(0.06,0.0035,0.05,0.04,20,5,0.60)}
    rt = mv_rates.get(risk, mv_rates["low"])
    mv_rev = vol*(mu/100) + tx*au + tx*s.avs_sell + 30*s.batch_sell + s.monthly_sell + s.pci_sell
    mv_cost = vol*rt[1] + tx*rt[0] + tx*rt[2] + 30*rt[3] + rt[4] + rt[5]
    if s.use_gateway:
        mv_cost += tx*0.03 + 5
    mv_res = (mv_rev - mv_cost) * rt[6]

    # Validate pricing against role floors
    validation = validate_pricing(
        role=role, risk=risk, markup=mu, auth=au,
        program="all", state="CO", pricing_model="interchange_plus"
    )

    # Run AI analysis
    try:
        analysis = await analyze_deal(
            merchant_name=s.merchant_name or "",
            vertical=s.vertical or "Other",
            risk_level=risk,
            volume=vol,
            transactions=tx,
            markup=mu,
            auth=au,
            monthly=s.monthly_sell or 0,
            bt_residual=bt_res,
            bf_residual=bf_res,
            nt_residual=nt_res,
            kv_residual=kv_res,
            mv_residual=mv_res,
            multi_loc=False,
            locations=1,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI analysis failed: {str(e)}")

    return {
        "analysis": analysis,
        "validation": validation,
        "residuals": {
            "beacon_trad": round(bt_res, 2),
            "beacon_flex": round(bf_res, 2),
            "north": round(nt_res, 2),
            "kurv": round(kv_res, 2),
            "maverick": round(mv_res, 2),
        },
        "provider_count": analysis.get("_providerCount", 0),
        "confidence": analysis.get("_confidence", "single"),
    }


@router.post("/quotes/validate")
async def validate_quote_pricing(
    markup: float = Query(...),
    auth: float = Query(...),
    risk: str = Query("low"),
    program: str = Query("all"),
    state: str = Query("CO"),
    pricing_model: str = Query("interchange_plus"),
    user: dict = Depends(get_current_user),
):
    require_internal(user)
    role = user.get("role", "employee")
    result = validate_pricing(role, risk, markup, auth, program, state, pricing_model)
    return result
