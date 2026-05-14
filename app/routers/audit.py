"""
Audit router — server-side AI extraction orchestration.
Keys live in Render env vars. Frontend sends file, backend calls providers.
"""

import base64
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import AuditJob, Merchant, User
from app.schemas import AuditStatusResponse
from app.services.auth_service import get_current_user
from app.services.ai_providers import run_audit_all_providers
from app.services.r2_storage import (
    r2_available, upload_to_r2, generate_r2_key, download_from_r2,
)

router = APIRouter()

ALLOWED_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/jpg", "image/webp",
    "text/csv", "text/plain",
}


# ── POST /api/audit/run ─────────────────────────────────────
@router.post("/audit/run")
async def run_audit(
    file: UploadFile = File(...),
    merchant_name: str = Form(""),
    processor: str = Form(""),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload a statement file and run multi-provider AI extraction.
    The file is stored in R2 (if configured) and metadata in Postgres.
    """
    content_type = file.content_type or "application/octet-stream"
    if content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {content_type}")

    file_bytes = await file.read()
    if len(file_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 20MB)")

    # Create audit job record
    job = AuditJob(
        user_id=user["sub"],
        status="processing",
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # Store file in R2 if available
    r2_key = None
    if r2_available():
        r2_key = generate_r2_key("statements", file.filename or "statement", user["sub"])
        await upload_to_r2(r2_key, file_bytes, content_type)
        job.statement_r2_key = r2_key

    # Prepare file for AI providers
    if content_type in ("text/csv", "text/plain"):
        file_b64 = file_bytes.decode("utf-8", errors="replace")
        media_type = "text/plain"
    else:
        file_b64 = base64.b64encode(file_bytes).decode("utf-8")
        media_type = content_type

    try:
        result = await run_audit_all_providers(file_b64, media_type)

        # Override name/processor if provided
        if merchant_name:
            result["name"] = merchant_name
        if processor:
            result["processor"] = processor

        # Compute rates
        vol = result.get("monthly_volume", 0)
        fees = result.get("total_fees", 0)
        if vol and fees:
            result["effective_rate"] = round((fees / vol) * 100, 4)
        ic = result.get("interchange_cost", 0)
        if vol and ic:
            result["interchange_rate"] = round((ic / vol) * 100, 4)
        markup = result.get("processor_markup", 0)
        if vol and markup:
            result["markup_rate"] = round((markup / vol) * 100, 4)

        # Save merchant to database
        merchant = Merchant(
            name=result.get("name", merchant_name or "Unknown"),
            processor=result.get("processor", processor or ""),
            statement_month=result.get("statement_month", ""),
            monthly_volume=result.get("monthly_volume", 0),
            total_fees=result.get("total_fees", 0),
            interchange_cost=result.get("interchange_cost", 0),
            processor_markup=result.get("processor_markup", 0),
            monthly_fees=result.get("monthly_fees", 0),
            transaction_count=result.get("transaction_count", 0),
            credit_card_pct=result.get("credit_card_pct", 85),
            avg_ticket=result.get("avg_ticket", 0),
            effective_rate=result.get("effective_rate", 0),
            interchange_rate=result.get("interchange_rate", 0),
            markup_rate=result.get("markup_rate", 0),
            risk_score=result.get("risk_score", 0),
            line_items=result.get("line_items", []),
            findings=result.get("findings", []),
            owner_email=user.get("email", ""),
            added_by=user.get("email", ""),
            is_demo=(user.get("role") == "demo"),
            statement_r2_key=r2_key,
        )

        # Link to user
        db_user = await db.execute(select(User).where(User.email == user.get("email")))
        u = db_user.scalar_one_or_none()
        if u:
            merchant.owner_id = u.id

        db.add(merchant)

        # Update job
        job.merchant_id = merchant.id
        job.status = "complete"
        job.consensus_data = result
        job.confidence = result.get("_confidence", "single")
        job.completed_at = datetime.now(timezone.utc)

        await db.commit()
        await db.refresh(merchant)

        return {
            "audit_id": job.id,
            "merchant_id": merchant.id,
            "status": "complete",
            "confidence": result.get("_confidence"),
            "provider_count": result.get("_providerCount", 1),
            "data": result,
        }

    except Exception as e:
        job.status = "failed"
        job.error_message = str(e)
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Audit failed: {str(e)}")


# ── GET /api/audit/{id} ─────────────────────────────────────
@router.get("/audit/{audit_id}", response_model=AuditStatusResponse)
async def get_audit_status(
    audit_id: str,
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(AuditJob).where(AuditJob.id == audit_id))
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Audit not found")

    return AuditStatusResponse(
        id=job.id,
        status=job.status,
        confidence=job.confidence,
        consensus_data=job.consensus_data,
        error_message=job.error_message,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )
