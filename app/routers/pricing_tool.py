"""
Pricing Tool API â€” Multi-AI Statement Extraction + Proposal Generation
All 4 providers (Claude, GPT-4o, Gemini, Grok) run in PARALLEL.
Results merged via consensus scoring. Files stored to R2, metadata to Postgres.
Employee/Admin only.
"""
import os, asyncio, base64
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services.auth_service import get_current_user
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Visitor, Merchant
from app.services.r2_storage import r2_available, generate_r2_key, upload_to_r2
from app.services.ai_providers import run_audit_all_providers

router = APIRouter(prefix="/api/pricing-tool", tags=["pricing-tool"])


# ── Upload size guard (matches audit.py MAX_TOTAL_BYTES) ──────────
MAX_TOTAL_BYTES = 75 * 1024 * 1024  # 75MB total decoded across all uploaded files


def _b64_decoded_size(b64: str) -> int:
    """Decoded byte size of a base64 string, computed from its length without
    allocating the decoded bytes (cheap pre-decode size guard)."""
    if not b64:
        return 0
    s = "".join(b64.split())  # drop any whitespace/newlines
    if s.startswith("data:") and "," in s:
        s = s.split(",", 1)[1]
    pad = s[-2:].count("=")
    return (len(s) * 3) // 4 - pad


def _enforce_upload_size(files, file_base64) -> None:
    """Reject with 413 if the combined DECODED size of all uploaded files exceeds
    the cap. Checks base64 length (no decode) so oversized payloads are rejected
    before any processing."""
    total = _b64_decoded_size(file_base64 or "")
    for f in files or []:
        f = f or {}
        total += _b64_decoded_size(f.get("base64") or f.get("data") or "")
    if total > MAX_TOTAL_BYTES:
        raise HTTPException(
            413,
            f"Upload too large ({total // (1024 * 1024)}MB); max {MAX_TOTAL_BYTES // (1024 * 1024)}MB total.",
        )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  REQUEST / RESPONSE MODELS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class ExtractRequest(BaseModel):
    file_base64: str
    media_type: Optional[str] = None
    file_type: Optional[str] = None   # frontend compat alias
    file_name: Optional[str] = "statement"
    files: Optional[list] = None      # multi-page: [{"base64":"...","type":"image/jpeg"}, ...]

    def resolved_media_type(self) -> str:
        """Accept either media_type or file_type from frontend."""
        mt = self.media_type or self.file_type or ""
        if not mt:
            name = (self.file_name or "").lower()
            if name.endswith(".pdf"):
                mt = "application/pdf"
            elif name.endswith((".jpg", ".jpeg")):
                mt = "image/jpeg"
            elif name.endswith(".png"):
                mt = "image/png"
            elif name.endswith(".webp"):
                mt = "image/webp"
            elif name.endswith((".csv", ".xlsx", ".xls")):
                mt = "text/csv"
            else:
                mt = "image/jpeg"
        return mt


class ProposalRequest(BaseModel):
    business_name: str = "Prospective Merchant"
    current_processor: str = "Unknown"
    current_rate: Optional[float] = None
    current_fees: Optional[float] = None
    monthly_volume: float = 0
    transactions: int = 0
    credit_card_pct: float = 75
    program_label: str = ""
    program_short: str = ""
    model_label: str = ""
    new_rate: float = 0
    np_residual_mo: float = 0
    market_benchmark: float = 0
    annual_savings: Optional[float] = None
    findings: List[str] = []
    rep_name: str = ""
    model_config = {"protected_namespaces": ()}


# ─────────────────────────────────────────────────────────────────
#  EXTRACTION — delegates to the shared multi-AI engine
#  (app/services/ai_providers.run_audit_all_providers) + adapter
# ─────────────────────────────────────────────────────────────────

async def _run_shared_extraction(file_base64: str, media_type: str, files: list = None) -> Dict[str, Any]:
    """Run the shared 4-AI extraction engine on EVERY uploaded page and adapt its
    result to the field names this router's endpoints and the frontend expect.

    Previously this read only files[0] and silently dropped pages 2..N. Now the
    whole upload is normalized into an ordered `pages` list and handed to the
    engine, which presents every page to every provider as a labeled content
    block ("Page 1 of N", ...). The first page is also passed positionally for
    backward compatibility with the single-file engine signature.
    """
    # Normalize the upload into an ordered page list. Multi-file uploads (photos,
    # multi-page picks) arrive in `files`; a single upload uses file_base64.
    pages: List[Dict[str, str]] = []
    if files:
        for f in files:
            f = f or {}
            b64 = f.get("base64") or f.get("data")
            if not b64:
                continue
            pages.append({
                "base64": b64,
                "media_type": f.get("type") or f.get("media_type") or media_type or "",
            })
    if not pages and file_base64:
        pages.append({"base64": file_base64, "media_type": media_type or ""})
    if not pages:
        raise HTTPException(400, "No file content to analyze.")

    primary_b64 = pages[0]["base64"]
    primary_type = pages[0]["media_type"]
    page_count = len(pages)

    try:
        # `pages` carries ALL pages; the engine (ai_providers.py, STEP 2) renders
        # them as labeled per-provider content blocks. primary_* stays for the
        # single-file fallback path.
        result = await run_audit_all_providers(primary_b64, primary_type, pages=pages)
    except ValueError as e:
        raise HTTPException(500, str(e))

    adapted = dict(result)
    adapted["business_name"] = adapted.pop("name", None) or None
    adapted["current_processor"] = adapted.pop("processor", None) or None
    adapted["findings"] = [
        (f.get("text", "") if isinstance(f, dict) else str(f))
        for f in (result.get("findings") or [])
    ]
    for field in ("contact_email", "contact_phone", "industry", "mcc_code"):
        adapted[field] = adapted.get(field) or None
    adapted.setdefault("_fieldSources", {})
    adapted["_pageCount"] = page_count
    return adapted


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  PROPOSAL GENERATION (text-only, any provider)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _call_proposal(prompt: str) -> str:
    providers = []
    if settings.ANTHROPIC_API_KEY:
        providers.append("claude")
    if settings.OPENAI_API_KEY:
        providers.append("openai")
    if settings.GOOGLE_API_KEY:
        providers.append("gemini")
    if settings.GROK_API_KEY:
        providers.append("grok")
    if not providers:
        raise HTTPException(500, "No AI keys configured")

    last = ""
    async with httpx.AsyncClient(timeout=60.0) as c:
        for name in providers:
            try:
                if name == "claude":
                    r = await c.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": settings.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                        json={"model": "claude-sonnet-4-6", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]})
                    d = r.json()
                    return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
                elif name == "openai":
                    r = await c.post("https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
                        json={"model": "gpt-4o", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]})
                    return r.json()["choices"][0]["message"]["content"]
                elif name == "gemini":
                    r = await c.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={settings.GOOGLE_API_KEY}",
                        json={"contents": [{"parts": [{"text": prompt}]}]})
                    return r.json()["candidates"][0]["content"]["parts"][0]["text"]
                elif name == "grok":
                    r = await c.post("https://api.x.ai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {settings.GROK_API_KEY}", "Content-Type": "application/json"},
                        json={"model": "grok-4.3", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]})
                    return r.json()["choices"][0]["message"]["content"]
            except Exception as ex:
                last = f"{name}: {ex}"
                continue
    raise HTTPException(500, f"All providers failed: {last}")


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  ROUTES
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

@router.post("/extract")
async def extract_statement(req: ExtractRequest, user=Depends(get_current_user)):
    if user.get("role") not in ("admin", "employee", "ic"):
        raise HTTPException(403, "Employee or admin access required")

    media_type = req.resolved_media_type()
    file_name = req.file_name or "statement"

    # â”€â”€ Store raw file to R2 if available â”€â”€
    r2_key = None
    if r2_available():
        try:
            file_bytes = base64.b64decode(req.file_base64)
            r2_key = generate_r2_key("statements", file_name, user.get("sub", ""))
            await upload_to_r2(r2_key, file_bytes, media_type)
        except Exception as e:
            print(f"R2 upload skipped: {e}")
            r2_key = None

    # â”€â”€ Run all 4 AI providers in parallel â”€â”€
    result = await _run_shared_extraction(req.file_base64 or "", media_type, files=getattr(req, "files", None))

    # Attach R2 key and file metadata
    result["_r2_key"] = r2_key
    result["_file_name"] = file_name
    result["_media_type"] = media_type

    # â”€â”€ Map to frontend expected field names â”€â”€
    return {
        "merchant_name": result.get("business_name"),
        "contact_email": result.get("contact_email"),
        "contact_phone": result.get("contact_phone"),
        "monthly_volume": result.get("monthly_volume"),
        "volume": result.get("monthly_volume"),
        "transactions": result.get("transaction_count"),
        "monthly_transactions": result.get("transaction_count"),
        "cc_percent": result.get("credit_card_pct"),
        "cc_pct": result.get("credit_card_pct"),
        "avg_ticket": result.get("avg_ticket"),
        "effective_rate": result.get("effective_rate"),
        "current_rate": result.get("effective_rate"),
        "processor": result.get("current_processor"),
        "total_fees": result.get("total_fees"),
        "interchange_cost": result.get("interchange_cost"),
        "vertical": result.get("industry"),
        "industry": result.get("industry"),
        "mcc_code": result.get("mcc_code"),
        "findings": result.get("findings", []),
        # AI consensus metadata
        "_providerCount": result.get("_providerCount", 0),
        "_providers": result.get("_providers", []),
        "_confidence": result.get("_confidence", "unknown"),
        "_agreePct": result.get("_agreePct", 0),
        "_fieldSources": result.get("_fieldSources", {}),
        "_errors": result.get("_errors", []),
        "_r2_key": r2_key,
        **({"_pageCount": result["_pageCount"]} if "_pageCount" in result else {}),
    }


@router.post("/proposal")
async def generate_proposal(req: ProposalRequest, user=Depends(get_current_user)):
    if user.get("role") not in ("admin", "employee", "ic"):
        raise HTTPException(403, "Employee or admin access required")
    prompt = f"""Write a merchant pricing proposal for NexusPay (veteran-owned). Clean text only, no markdown.
Merchant: {req.business_name}
Current Processor: {req.current_processor}
Current Rate: {str(req.current_rate)+'%' if req.current_rate else 'N/A'}
Current Fees: {'$'+f'{req.current_fees:,.2f}' if req.current_fees else 'N/A'}
Volume: ${req.monthly_volume:,.0f}/mo | {req.transactions:,} txns | {req.credit_card_pct}% CC
Recommended: {req.program_label} ({req.program_short}) x {req.model_label}
New Rate: {req.new_rate:.2f}% | NP Residual: ${req.np_residual_mo:,.2f}/mo | Market: {req.market_benchmark:.2f}%
{'Savings: $'+f'{req.annual_savings:,.2f}/yr' if req.annual_savings else ''}
{'Issues: '+'; '.join(req.findings) if req.findings else ''}
3 paragraphs: (1) current situation, (2) solution and why, (3) savings + next steps.
End with: Ready to get started? Call (720) 689-7272 or visit nexuspayservices.com"""
    txt = await _call_proposal(prompt)
    return {"proposal_text": txt, "generated_at": datetime.now(timezone.utc).isoformat()}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  PUBLIC: 4-AI PARALLEL CONSENSUS PROPOSAL (customer-facing)
#  No authentication required. No internal data exposed.
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class PublicProposalRequest(BaseModel):
    business_name: str = "Business Owner"
    industry: str = "Retail"
    monthly_volume: float = 0
    transactions: int = 0
    credit_card_pct: float = 85
    current_rate: float = 0
    current_monthly_cost: float = 0
    recommended_model: str = "Cash Discount"
    nexuspay_rate: float = 0
    nexuspay_monthly_cost: float = 0
    annual_savings: float = 0
    market_avg_rate: float = 0
    model_config = {"protected_namespaces": ()}


PUBLIC_PROPOSAL_PROMPT = """You are writing a professional merchant services proposal for NexusPay, a veteran-owned payment processing company in Colorado.

Write a clear, warm, professional proposal for this merchant. Use plain language suitable for a business owner. No markdown, no bullet points, no headers â€” just clean paragraphs.

MERCHANT DETAILS:
- Business: {business_name}
- Industry: {industry}
- Monthly Volume: ${volume:,.0f}
- Monthly Transactions: {transactions:,}
- Current Rate: {current_rate:.2f}%
- Current Monthly Cost: ${current_cost:,.2f}

NEXUSPAY RECOMMENDATION:
- Pricing Model: {model}
- NexusPay Rate: {np_rate:.2f}%
- NexusPay Monthly Cost: ${np_cost:,.2f}
- Projected Annual Savings: ${savings:,.0f}
- Industry Average Rate: {market:.2f}%

Write exactly 3 paragraphs:
1. Acknowledge their current situation and what they're paying vs the industry average.
2. Explain the recommended pricing model in simple terms and why it's the best fit.
3. Quantify the savings and provide a clear next step.

End with: Ready to start saving? Call us at (720) 689-7272, visit nexuspayservices.com, or book a free consultation at no obligation.

Keep it under 250 words. Warm, confident, veteran-owned brand voice. No hype, no pressure."""


async def _run_proposal_consensus(prompt: str) -> Dict[str, Any]:
    """Run all 4 AI providers in parallel for proposal text, return best + metadata."""
    providers = []
    if settings.ANTHROPIC_API_KEY:
        providers.append(("Claude", "claude"))
    if settings.OPENAI_API_KEY:
        providers.append(("GPT-4o", "openai"))
    if settings.GOOGLE_API_KEY:
        providers.append(("Gemini", "gemini"))
    if settings.GROK_API_KEY:
        providers.append(("Grok", "grok"))

    if not providers:
        raise HTTPException(500, "No AI keys configured")

    results = []
    errors = []

    async def _run(name, key):
        try:
            async with httpx.AsyncClient(timeout=60.0) as c:
                if key == "claude":
                    r = await c.post("https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": settings.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
                        json={"model": "claude-sonnet-4-6", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]})
                    d = r.json()
                    txt = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
                    results.append({"provider": name, "text": txt})
                elif key == "openai":
                    r = await c.post("https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
                        json={"model": "gpt-4o", "max_tokens": 1000, "temperature": 0.3, "messages": [{"role": "user", "content": prompt}]})
                    results.append({"provider": name, "text": r.json()["choices"][0]["message"]["content"]})
                elif key == "gemini":
                    r = await c.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={settings.GOOGLE_API_KEY}",
                        json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.3, "maxOutputTokens": 1000}})
                    results.append({"provider": name, "text": r.json()["candidates"][0]["content"]["parts"][0]["text"]})
                elif key == "grok":
                    r = await c.post("https://api.x.ai/v1/chat/completions",
                        headers={"Authorization": f"Bearer {settings.GROK_API_KEY}", "Content-Type": "application/json"},
                        json={"model": "grok-4.3", "max_tokens": 1000, "temperature": 0.3, "messages": [{"role": "user", "content": prompt}]})
                    results.append({"provider": name, "text": r.json()["choices"][0]["message"]["content"]})
        except Exception as e:
            errors.append({"provider": name, "error": str(e)})

    await asyncio.gather(*[_run(n, k) for n, k in providers])

    if not results:
        err_msg = "; ".join(f"{e['provider']}: {e['error']}" for e in errors)
        raise HTTPException(500, f"All AI providers failed: {err_msg}")

    # Pick longest proposal (most detailed), report all providers
    best = max(results, key=lambda r: len(r.get("text", "")))

    return {
        "proposal_text": best["text"],
        "selected_provider": best["provider"],
        "_providerCount": len(results),
        "_providers": [r["provider"] for r in results],
        "_errors": errors,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@router.post("/public-proposal")
async def generate_public_proposal(req: PublicProposalRequest):
    """Public endpoint â€” no auth. Runs all 4 AIs in parallel for consensus proposal."""
    if req.monthly_volume <= 0:
        raise HTTPException(400, "Monthly volume is required")

    prompt = PUBLIC_PROPOSAL_PROMPT.format(
        business_name=req.business_name or "Business Owner",
        industry=req.industry or "Retail",
        volume=req.monthly_volume,
        transactions=req.transactions,
        current_rate=req.current_rate,
        current_cost=req.current_monthly_cost,
        model=req.recommended_model,
        np_rate=req.nexuspay_rate,
        np_cost=req.nexuspay_monthly_cost,
        savings=req.annual_savings,
        market=req.market_avg_rate,
    )

    result = await _run_proposal_consensus(prompt)
    return result


class PublicExtractRequest(BaseModel):
    file_base64: Optional[str] = None
    media_type: Optional[str] = None
    file_type: Optional[str] = None
    file_name: Optional[str] = "statement"
    business_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    files: Optional[list] = None  # Multi-page: [{"base64":"...","type":"image/jpeg"}, ...]

    def resolved_media_type(self) -> str:
        mt = self.media_type or self.file_type or ""
        if not mt:
            name = (self.file_name or "").lower()
            if name.endswith(".pdf"): mt = "application/pdf"
            elif name.endswith((".jpg", ".jpeg")): mt = "image/jpeg"
            elif name.endswith(".png"): mt = "image/png"
            else: mt = "image/jpeg"
        return mt


def _compute_forensic_grade(effective_rate: float) -> Dict[str, Any]:
    """
    Forensic Audit v1 Grading.
    Grades the merchant's current processing cost vs US market benchmark.
    Market band: 2.87% - 4.35% (US average per NexusPay paycalculator).

    Returns letter grade, position descriptor, and color tier for UI rendering.
    """
    if not effective_rate or effective_rate <= 0:
        return {
            "grade": None,
            "position": "insufficient_data",
            "label": "Insufficient data \u2014 manual review required",
            "tier": "unknown",
        }

    er = float(effective_rate)
    if er <= 2.00:
        return {"grade": "A+", "position": "well_below_market", "label": "Well below market floor", "tier": "excellent"}
    if er <= 2.50:
        return {"grade": "A",  "position": "below_market",      "label": "Below market floor",      "tier": "excellent"}
    if er <= 2.87:
        return {"grade": "A-", "position": "at_market_floor",   "label": "At market floor",         "tier": "excellent"}
    if er <= 3.10:
        return {"grade": "B+", "position": "low_band",          "label": "Lower end of market",     "tier": "good"}
    if er <= 3.40:
        return {"grade": "B",  "position": "mid_band_lower",    "label": "Lower half of market",    "tier": "good"}
    if er <= 3.61:
        return {"grade": "B-", "position": "median",            "label": "Around market median",    "tier": "fair"}
    if er <= 3.85:
        return {"grade": "C+", "position": "mid_band_upper",    "label": "Upper half of market",    "tier": "fair"}
    if er <= 4.10:
        return {"grade": "C",  "position": "high_band",         "label": "High end of market",      "tier": "poor"}
    if er <= 4.35:
        return {"grade": "C-", "position": "at_market_ceiling", "label": "At market ceiling",       "tier": "poor"}
    if er <= 5.00:
        return {"grade": "D",  "position": "above_market",      "label": "Above market ceiling",    "tier": "critical"}
    return     {"grade": "F",  "position": "severely_above",    "label": "Severely above market",   "tier": "critical"}


@router.post("/public-extract")
async def public_extract_statement(req: PublicExtractRequest, db: AsyncSession = Depends(get_db)):
    """Public: 4-AI extraction + auto-create Visitor lead + Merchant record."""
    _enforce_upload_size(req.files, req.file_base64)   # 413 if total decoded size > 75MB
    media_type = req.resolved_media_type()
    result = await _run_shared_extraction(req.file_base64 or "", media_type, files=req.files)

    biz = result.get("business_name") or req.business_name or "Unknown Business"
    vol = result.get("monthly_volume") or 0
    fees = result.get("total_fees") or 0
    tx = result.get("transaction_count") or 0
    eff = result.get("effective_rate") or 0
    proc = result.get("current_processor") or ""
    industry = result.get("industry") or ""
    email = req.email or ""
    phone = req.phone or ""
    provs = result.get("_providers", [])
    conf = result.get("_confidence", "unknown")
    findings_list = result.get("findings", []) or []

    # Create Visitor (lead) record
    try:
        visitor = Visitor(
            full_name=biz,
            business_name=biz,
            email=email or "noemail@pricetool.nexuspay",
            phone=phone,
            source="pricing_tool_upload",
            ai_business_type=industry,
            message=f"[Statement Upload] {len(provs)} AIs ({conf}) | Processor: {proc} | Vol: ${vol:,.0f} | Fees: ${fees:,.2f} | Rate: {eff:.2f}% | Txns: {tx}",
        )
        db.add(visitor)
        await db.flush()
    except Exception:
        pass

    # Create Merchant prospect record
    merchant_id_for_signup = None
    try:
        merchant = Merchant(
            name=biz,
            processor=proc,
            monthly_volume=float(vol) if vol else 0,
            total_fees=float(fees) if fees else 0,
            transaction_count=int(tx) if tx else 0,
            effective_rate=float(eff) if eff else 0,
            credit_card_pct=float(result.get("credit_card_pct") or 85),
            avg_ticket=float(result.get("avg_ticket") or 0),
            interchange_cost=float(result.get("interchange_cost") or 0),
            is_demo=False,
            added_by="pricing_tool_public",
            owner_email=email,
        )
        db.add(merchant)
        await db.flush()
        merchant_id_for_signup = merchant.id
    except Exception:
        pass

    await db.commit()

    # ── Forensic Audit v1 grading ───────────────────────────────────────
    eff_float = float(eff) if eff else 0.0
    vol_float = float(vol) if vol else 0.0
    forensic = _compute_forensic_grade(eff_float)

    # Estimated annual overcharge vs market floor (2.87%)
    # Only meaningful if effective rate is above market floor and volume is known
    MARKET_FLOOR = 2.87
    annual_overcharge = 0.0
    if eff_float > MARKET_FLOOR and vol_float > 0:
        monthly_overcharge = ((eff_float - MARKET_FLOOR) / 100.0) * vol_float
        annual_overcharge = round(monthly_overcharge * 12, 2)

    return {
        "business_name": result.get("business_name"),
        "monthly_volume": result.get("monthly_volume"),
        "transaction_count": result.get("transaction_count"),
        "credit_card_pct": result.get("credit_card_pct"),
        "avg_ticket": result.get("avg_ticket"),
        "effective_rate": result.get("effective_rate"),
        "current_processor": result.get("current_processor"),
        "total_fees": result.get("total_fees"),
        "industry": result.get("industry"),
        "findings": findings_list,
        "_providerCount": result.get("_providerCount", 0),
        "_providers": result.get("_providers", []),
        "_confidence": result.get("_confidence", "unknown"),
        "_errors": result.get("_errors", []),
        "_saved": True,
        # Forensic Audit v1 fields
        "_grade": forensic["grade"],
        "_grade_position": forensic["position"],
        "_grade_label": forensic["label"],
        "_grade_tier": forensic["tier"],
        "_market_floor": MARKET_FLOOR,
        "_market_ceiling": 4.35,
        "_estimated_annual_overcharge": annual_overcharge,
        "_findings_count": len(findings_list),
        "_merchant_id": merchant_id_for_signup,
        **({"_pageCount": result["_pageCount"]} if "_pageCount" in result else {}),
    }
