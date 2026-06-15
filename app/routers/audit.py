"""
Audit router - server-side AI extraction orchestration. TIER 1.
Adds: required statement date, file hashing, dedup/cache (reproducibility),
multi-file grouping + R2 cold storage, revision flagging, append-only audit log.
Keys live in Render env vars. Frontend sends file(s); backend calls providers.
"""

import base64
import hashlib
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db
from app.models import AuditJob, Merchant, User
from app.schemas import AuditStatusResponse
from app.services.auth_service import get_current_user
from app.services.ai_providers import run_audit_all_providers
from app.services.r2_storage import r2_available, upload_to_r2, generate_r2_key
from app.services import audit_log

router = APIRouter()


import re as _re_t1d
def _normalize_month(s):
    """Convert MM/YYYY, 'February 2026', or YYYY-MM string to YYYY-MM. Returns '' if cannot parse."""
    s = (s or "").strip()
    if not s: return ""
    if _re_t1d.match(r"^\d{4}-\d{2}$", s): return s
    parts = s.split("/")
    if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
        mm, yyyy = parts[0], parts[1]
        if len(yyyy) == 4 and 1 <= int(mm) <= 12:
            return f"{yyyy}-{mm.zfill(2)}"
    _months = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
               "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}
    m = _re_t1d.match(r"^([A-Za-z]+)\s+(\d{4})$", s)
    if m:
        mname = m.group(1)[:3].lower()
        if mname in _months:
            return f"{m.group(2)}-{_months[mname]:02d}"
    return ""


ALLOWED_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/jpg", "image/webp",
    "image/tiff", "image/heic", "text/csv", "text/plain",
}
MAX_TOTAL_BYTES = 40 * 1024 * 1024  # 40MB across all files in one statement


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _combined_hash(per_file_hashes: List[str]) -> str:
    """Statement-level hash = sha256 over the ordered per-file digests."""
    joined = "".join(per_file_hashes).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()


ENTITY_MATCH_TOLERANCE = 0.05  # 5% - volume/fees this close to a stored record = same statement


def _within_pct(new_val, stored_val, tolerance: float = ENTITY_MATCH_TOLERANCE) -> bool:
    """True if new_val is within `tolerance` (relative) of stored_val."""
    try:
        new_val = float(new_val or 0)
        stored_val = float(stored_val or 0)
    except (TypeError, ValueError):
        return False
    if stored_val == 0:
        return new_val == 0
    return abs(new_val - stored_val) <= tolerance * abs(stored_val)


def _client_meta(request: Request):
    ip = request.client.host if request and request.client else None
    ua = request.headers.get("user-agent") if request else None
    return ip, ua


_CONSENSUS_CHECK_FIELDS = ("monthly_volume", "total_fees", "effective_rate")


def _build_provider_results(result: dict) -> List[dict]:
    """Per-provider snapshots with a match/outlier flag vs the consensus values."""
    out = []
    for p in result.get("_provider_results", []) or []:
        confidence = "match"
        for field in _CONSENSUS_CHECK_FIELDS:
            cv, pv = result.get(field), p.get(field)
            if cv is None and pv is None:
                continue
            try:
                cv, pv = float(cv), float(pv)
            except (TypeError, ValueError):
                confidence = "outlier"
                break
            if abs(pv - cv) > abs(cv) * 0.05:
                confidence = "outlier"
                break
        out.append({
            "provider": p.get("provider"),
            "name": p.get("name"),
            "processor": p.get("processor"),
            "monthly_volume": p.get("monthly_volume"),
            "total_fees": p.get("total_fees"),
            "effective_rate": p.get("effective_rate"),
            "confidence": confidence,
        })
    return out


# -- POST /api/audit/run -------------------------------------
@router.post("/audit/run")
async def run_audit(
    request: Request,
    files: Optional[List[UploadFile]] = File(None),
    file: Optional[UploadFile] = File(None),   # legacy single-file field (back-compat)
    statement_date: str = Form(""),       # Optional - auto-derived from extracted statement_month if blank
    merchant_name: str = Form(""),
    processor: str = Form(""),
    uploaded_via: str = Form("command_center"),
    consent: bool = Form(False),
    user: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Upload one or more files that together form ONE merchant statement,
    run multi-provider AI extraction, store everything in R2, and persist
    metadata + an append-only audit-log trail in Postgres.
    """
    # Merge legacy single-file + new multi-file inputs
    files = (files or []) + ([file] if file else [])

    # 1) Statement date - manual entry takes precedence; auto-derive below if blank
    statement_date = (statement_date or "").strip()

    # 1b) Consent is MANDATORY (chain-of-custody / subprocessor disclosure)
    if not consent:
        raise HTTPException(
            status_code=400,
            detail="Consent is required: you must check the consent box to authorize storage and processing of this statement."
        )

    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required.")

    # 2) Read + validate + hash each file (preserve upload order = page order)
    file_records = []
    total = 0
    per_file_hashes: List[str] = []
    for idx, f in enumerate(files):
        ctype = f.content_type or "application/octet-stream"
        if ctype not in ALLOWED_TYPES:
            raise HTTPException(status_code=400, detail=f"Unsupported file type: {ctype}")
        data = await f.read()
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise HTTPException(status_code=400, detail="Upload too large (max 40MB total).")
        h = _sha256(data)
        per_file_hashes.append(h)
        file_records.append({
            "page": idx + 1,
            "filename": f.filename or f"page_{idx+1}",
            "content_type": ctype,
            "sha256": h,
            "bytes": data,
        })

    combined = _combined_hash(per_file_hashes)
    page_count = len(file_records)
    ip, ua = _client_meta(request)

    # 3) DEDUP / CACHE - identical bytes => return the prior result (reproducibility)
    prior = await db.execute(
        select(AuditJob)
        .where(AuditJob.file_hash == combined,
               AuditJob.user_id == user["sub"],
               AuditJob.status == "complete")
        .order_by(AuditJob.created_at.desc())
    )
    prior_job = prior.scalars().first()
    if prior_job and prior_job.consensus_data:
        await audit_log.record(
            db, action="dedup_hit", actor=user, entity_type="statement",
            entity_id=prior_job.merchant_id, file_hash=combined,
            ip_address=ip, user_agent=ua,
            detail={"reason": "identical file hash", "page_count": page_count},
            commit=True,
        )
        _cached = prior_job.consensus_data or {}
        _cached_pr = prior_job.provider_results
        if not isinstance(_cached_pr, list):  # legacy rows stored {} via the old column default
            _cached_pr = _build_provider_results(_cached)
        return {
            "audit_id": prior_job.id,
            "merchant_id": prior_job.merchant_id,
            "status": "complete",
            "cache_hit": True,
            "entity_match": False,
            "statement_date": _normalize_month(_cached.get("statement_month", "")),
            "confidence": prior_job.confidence,
            "agree_pct": prior_job.agree_pct,
            "provider_results": _cached_pr,
            "data": prior_job.consensus_data,
        }

    # 4) Open the job
    job = AuditJob(
        user_id=user["sub"], status="processing",
        file_hash=combined, page_count=page_count,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    # 5) Cold-store EVERY file in R2 (compliance / chain of custody)
    files_meta = []
    primary_b64 = None
    primary_media = None
    for rec in file_records:
        r2_key = None
        if r2_available():
            r2_key = generate_r2_key("statements", rec["filename"], user["sub"])
            await upload_to_r2(r2_key, rec["bytes"], rec["content_type"])
        files_meta.append({
            "page": rec["page"], "filename": rec["filename"],
            "content_type": rec["content_type"], "sha256": rec["sha256"],
            "r2_key": r2_key,
        })
        if rec["page"] == 1:
            if rec["content_type"] in ("text/csv", "text/plain"):
                primary_b64 = rec["bytes"].decode("utf-8", errors="replace")
                primary_media = "text/plain"
            else:
                primary_b64 = base64.b64encode(rec["bytes"]).decode("utf-8")
                primary_media = rec["content_type"]

    job.files = files_meta
    job.statement_r2_key = files_meta[0]["r2_key"] if files_meta else None

    await audit_log.record(
        db, action="upload", actor=user, entity_type="statement",
        entity_id=job.id, file_hash=combined, ip_address=ip, user_agent=ua,
        detail={"page_count": page_count, "uploaded_via": uploaded_via,
                "statement_date": statement_date, "consent": bool(consent)},
    )

    try:
        # 6) Analyze. Single file / multi-page PDF = full analysis.
        result = await run_audit_all_providers(primary_b64, primary_media)

        # Tier 1d: auto-derive statement_date from extracted statement_month if blank
        if not statement_date:
            statement_date = _normalize_month(result.get("statement_month", ""))

        # Multi-PHOTO statements: stored+grouped, but flag analysis for review
        # (true multi-image vision consensus is Tier 1b).
        multi_photo = page_count > 1 and primary_media and primary_media.startswith("image/")
        if multi_photo:
            findings = result.get("findings", []) or []
            findings.append(
                f"NEEDS REVIEW - multi-file statement: {page_count} files stored and "
                f"hashed, automated analysis covered file 1 of {page_count}. "
                f"Full multi-page analysis pending (Tier 1b)."
            )
            result["findings"] = findings
            result["_needs_review"] = True

        if merchant_name:
            result["name"] = merchant_name
        if processor:
            result["processor"] = processor

        # Derived rates
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

        mn = str(result.get("merchant_number", "") or "").strip()
        agree_pct = int(result.get("_agreePct", 0) or 0)
        providers = result.get("_providers", []) or []
        # Per-provider snapshots flagged against consensus (after derived rates above)
        provider_results = _build_provider_results(result)

        db_user = await db.execute(select(User).where(User.email == user.get("email")))
        u = db_user.scalar_one_or_none()

        # 6b) ENTITY-LEVEL DEDUP - same MID + statement_date + owner, and the
        # extracted volume/fees within 5% of a stored record => same statement
        # re-uploaded (e.g. a rescan with different bytes). Return the stored
        # result instead of persisting a duplicate merchant.
        # NOTE: runs after extraction because merchant_number / monthly_volume /
        # total_fees only exist once the providers have read the statement.
        if mn and statement_date and u:
            ent_q = await db.execute(
                select(Merchant).where(
                    Merchant.merchant_number == mn,
                    Merchant.statement_date == statement_date,
                    Merchant.owner_id == u.id,
                ).order_by(Merchant.created_at.desc())
            )
            for entity in ent_q.scalars().all():
                if not (_within_pct(result.get("monthly_volume", 0), entity.monthly_volume)
                        and _within_pct(result.get("total_fees", 0), entity.total_fees)):
                    continue

                prior_q = await db.execute(
                    select(AuditJob)
                    .where(AuditJob.merchant_id == entity.id,
                           AuditJob.status == "complete")
                    .order_by(AuditJob.created_at.desc())
                )
                entity_job = prior_q.scalars().first()
                cached = (entity_job.consensus_data if entity_job else None) or {
                    "name": entity.name,
                    "processor": entity.processor,
                    "statement_month": entity.statement_month,
                    "merchant_number": entity.merchant_number,
                    "monthly_volume": entity.monthly_volume,
                    "total_fees": entity.total_fees,
                    "interchange_cost": entity.interchange_cost,
                    "processor_markup": entity.processor_markup,
                    "monthly_fees": entity.monthly_fees,
                    "transaction_count": entity.transaction_count,
                    "credit_card_pct": entity.credit_card_pct,
                    "avg_ticket": entity.avg_ticket,
                    "effective_rate": entity.effective_rate,
                    "interchange_rate": entity.interchange_rate,
                    "markup_rate": entity.markup_rate,
                    "risk_score": entity.risk_score,
                    "line_items": entity.line_items,
                    "findings": entity.findings,
                }

                job.merchant_id = entity.id
                job.status = "complete"
                job.consensus_data = cached
                job.confidence = entity_job.confidence if entity_job else "single"
                job.agree_pct = entity.agree_pct
                job.cache_hit = True
                job.completed_at = datetime.now(timezone.utc)

                await audit_log.record(
                    db, action="dedup_hit", actor=user, entity_type="merchant",
                    entity_id=entity.id, file_hash=combined,
                    ip_address=ip, user_agent=ua,
                    detail={"reason": "entity match - same MID + statement_date + "
                                      "owner, volume/fees within 5%",
                            "merchant_number": mn,
                            "statement_date": statement_date,
                            "page_count": page_count},
                    commit=True,
                )
                return {
                    "audit_id": job.id,
                    "merchant_id": entity.id,
                    "status": "complete",
                    "cache_hit": True,
                    "entity_match": True,
                    "statement_date": statement_date,
                    "confidence": job.confidence,
                    "agree_pct": entity.agree_pct,
                    "data": cached,
                }

        # 7) REVISION CHECK - same MID + statement_date, different bytes => flag + keep
        revision_group = None
        is_revision = False
        if mn:
            rev_q = await db.execute(
                select(Merchant).where(
                    Merchant.owner_email == user.get("email", ""),
                    Merchant.merchant_number == mn,
                    Merchant.statement_date == statement_date,
                ).order_by(Merchant.created_at.asc())
            )
            existing = rev_q.scalars().first()
            if existing and existing.file_hash != combined:
                is_revision = True
                revision_group = existing.revision_group or existing.id
                await audit_log.record(
                    db, action="revision_flagged", actor=user,
                    entity_type="statement", entity_id=job.id, file_hash=combined,
                    ip_address=ip, user_agent=ua,
                    detail={"merchant_number": mn, "statement_date": statement_date,
                            "original_id": existing.id},
                )

        # 8) Persist merchant with all Tier 1 fields
        merchant = Merchant(
            name=result.get("name", merchant_name or "Unknown"),
            processor=result.get("processor", processor or ""),
            statement_month=result.get("statement_month", ""),
            statement_date=statement_date,
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
            statement_r2_key=files_meta[0]["r2_key"] if files_meta else None,
            file_hash=combined,
            page_count=page_count,
            merchant_number=mn or None,
            revision_group=revision_group,
            is_revision=is_revision,
            agree_pct=agree_pct,
            provider_count=int(result.get("_providerCount", len(providers) or 1)),
            providers=providers,
            files=files_meta,
            uploaded_via=uploaded_via,
            consent_at=(datetime.now(timezone.utc) if consent else None),
        )
        if u:
            merchant.owner_id = u.id
        db.add(merchant)

        job.merchant_id = merchant.id
        job.status = "complete"
        job.consensus_data = result
        job.provider_results = provider_results
        job.confidence = result.get("_confidence", "single")
        job.agree_pct = agree_pct
        job.cache_hit = False
        job.completed_at = datetime.now(timezone.utc)

        await audit_log.record(
            db, action="analyze", actor=user, entity_type="merchant",
            entity_id=merchant.id, file_hash=combined, ip_address=ip, user_agent=ua,
            detail={"providers": providers, "agree_pct": agree_pct,
                    "confidence": job.confidence, "is_revision": is_revision},
        )

        await db.commit()
        await db.refresh(merchant)

        return {
            "audit_id": job.id,
            "merchant_id": merchant.id,
            "status": "complete",
            "cache_hit": False,
            "entity_match": False,
            "statement_date": statement_date,
            "confidence": result.get("_confidence"),
            "agree_pct": agree_pct,
            "provider_count": merchant.provider_count,
            "provider_results": provider_results,
            "is_revision": is_revision,
            "needs_review": bool(result.get("_needs_review")),
            "data": result,
        }

    except Exception as e:
        job.status = "failed"
        job.error_message = str(e)
        await db.commit()
        raise HTTPException(status_code=500, detail=f"Audit failed: {str(e)}")


# -- GET /api/audit/{id} -------------------------------------
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

    # Role isolation: only admin/employee may view other users' audit jobs
    if user.get("role") not in ("admin", "employee") and job.user_id != user.get("sub"):
        raise HTTPException(status_code=403, detail="Not authorized to view this audit")

    return AuditStatusResponse(
        id=job.id,
        status=job.status,
        confidence=job.confidence,
        consensus_data=job.consensus_data,
        error_message=job.error_message,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )