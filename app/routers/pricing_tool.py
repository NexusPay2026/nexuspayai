"""
Pricing Tool API â€” Multi-AI Statement Extraction + Proposal Generation
All 4 providers (Claude, GPT-4o, Gemini, Grok) run in PARALLEL.
Results merged via consensus scoring. Files stored to R2, metadata to Postgres.
Employee/Admin only.
"""
import json, os, asyncio, base64
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from collections import Counter

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services.auth_service import get_current_user
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Visitor, Merchant
from app.services.r2_storage import r2_available, generate_r2_key, upload_to_r2

router = APIRouter(prefix="/api/pricing-tool", tags=["pricing-tool"])


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  REQUEST / RESPONSE MODELS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

class ExtractRequest(BaseModel):
    file_base64: str
    media_type: Optional[str] = None
    file_type: Optional[str] = None   # frontend compat alias
    file_name: Optional[str] = "statement"

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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  EXTRACT PROMPT
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

EXTRACT_PROMPT = """You are a merchant processing statement analyst for NexusPay. Extract fields from this statement. Return ONLY valid JSON, no markdown, no backticks:
{"business_name":null,"contact_email":null,"contact_phone":null,"monthly_volume":null,"transaction_count":null,"credit_card_pct":null,"avg_ticket":null,"effective_rate":null,"current_processor":null,"total_fees":null,"interchange_cost":null,"industry":null,"mcc_code":null,"findings":[]}
If a field cannot be determined, use null. For effective_rate, calculate as (total_fees/monthly_volume*100) if both available. Flag hidden fees, overcharges, PCI issues in findings array."""


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  INDIVIDUAL AI PROVIDER CALLS (with vision/document support)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _extract_claude(file_base64: str, media_type: str) -> Dict:
    key = settings.ANTHROPIC_API_KEY
    if not key:
        return None
    content = []
    if media_type == "application/pdf":
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_base64}})
    elif media_type.startswith("image"):
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": file_base64}})
    else:
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": file_base64}})
    content.append({"type": "text", "text": EXTRACT_PROMPT})
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 1500, "messages": [{"role": "user", "content": content}]})
        if r.status_code != 200:
            raise Exception(f"Claude {r.status_code}: {r.text[:200]}")
        d = r.json()
        txt = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    return {"provider": "Claude", "raw": txt}


async def _extract_openai(file_base64: str, media_type: str) -> Dict:
    key = settings.OPENAI_API_KEY
    if not key:
        return None
    content = []
    if media_type.startswith("image") or media_type == "application/pdf":
        content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{file_base64}"}})
    content.append({"type": "text", "text": EXTRACT_PROMPT})
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "max_tokens": 1500, "messages": [{"role": "user", "content": content}]})
        if r.status_code != 200:
            raise Exception(f"GPT-4o {r.status_code}: {r.text[:200]}")
        return {"provider": "GPT-4o", "raw": r.json()["choices"][0]["message"]["content"]}


async def _extract_gemini(file_base64: str, media_type: str) -> Dict:
    key = settings.GOOGLE_API_KEY
    if not key:
        return None
    parts = [{"text": EXTRACT_PROMPT}]
    if media_type.startswith("image") or media_type == "application/pdf":
        parts.append({"inline_data": {"mime_type": media_type, "data": file_base64}})
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={key}",
            json={"contents": [{"parts": parts}], "generationConfig": {"temperature": 0.2, "maxOutputTokens": 1500}})
        if r.status_code != 200:
            raise Exception(f"Gemini {r.status_code}: {r.text[:200]}")
        return {"provider": "Gemini", "raw": r.json()["candidates"][0]["content"]["parts"][0]["text"]}


async def _extract_grok(file_base64: str, media_type: str) -> Dict:
    key = settings.GROK_API_KEY
    if not key:
        return None
    content = [{"type": "text", "text": EXTRACT_PROMPT}]
    if media_type.startswith("image"):
        content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{file_base64}"}})
    async with httpx.AsyncClient(timeout=120.0) as c:
        r = await c.post("https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"model": "grok-2-vision-1212", "max_tokens": 1500, "messages": [{"role": "user", "content": content}]})
        if r.status_code != 200:
            raise Exception(f"Grok {r.status_code}: {r.text[:200]}")
        return {"provider": "Grok", "raw": r.json()["choices"][0]["message"]["content"]}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  JSON PARSER
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

def _parse_json(raw: str) -> Dict:
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON found in response")
    return json.loads(text[start:end + 1])


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#  4-AI PARALLEL EXTRACTION + CONSENSUS
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•

async def _run_all_extractions(file_base64: str, media_type: str) -> Dict[str, Any]:
    """Run all 4 providers in parallel, merge results with consensus scoring."""

    providers = []
    if settings.ANTHROPIC_API_KEY:
        providers.append(("Claude", _extract_claude))
    if settings.OPENAI_API_KEY:
        providers.append(("GPT-4o", _extract_openai))
    if settings.GOOGLE_API_KEY:
        providers.append(("Gemini", _extract_gemini))
    if settings.GROK_API_KEY:
        providers.append(("Grok", _extract_grok))

    if not providers:
        raise HTTPException(500, "No AI API keys configured â€” set ANTHROPIC_API_KEY, OPENAI_API_KEY, GOOGLE_API_KEY, GROK_API_KEY in Render env vars")

    results = []
    errors = []

    async def _run(name, func):
        try:
            raw = await func(file_base64, media_type)
            if raw:
                parsed = _parse_json(raw["raw"])
                parsed["_provider"] = name
                results.append(parsed)
        except Exception as e:
            errors.append({"provider": name, "error": str(e)})

    await asyncio.gather(*[_run(n, f) for n, f in providers])

    if not results:
        err_msg = "; ".join(f"{e['provider']}: {e['error']}" for e in errors)
        raise HTTPException(500, f"All AI providers failed: {err_msg}")

    # Single provider â€” return directly
    if len(results) == 1:
        r = results[0]
        r["_providerCount"] = 1
        r["_confidence"] = "single"
        r["_providers"] = [r["_provider"]]
        r["_errors"] = errors
        return r

    # Multi-provider consensus merge
    consensus = _build_extraction_consensus(results)
    consensus["_errors"] = errors
    return consensus


def _build_extraction_consensus(results: List[Dict]) -> Dict[str, Any]:
    """Merge extraction results from multiple AI providers with confidence scoring."""

    FIELDS = [
        "business_name", "contact_email", "contact_phone", "monthly_volume",
        "transaction_count", "credit_card_pct", "avg_ticket", "effective_rate",
        "current_processor", "total_fees", "interchange_cost", "industry", "mcc_code"
    ]

    merged = {}
    field_sources = {}  # track which providers agreed on each field

    for field in FIELDS:
        values = []
        for r in results:
            v = r.get(field)
            if v is not None and v != "" and v != 0:
                values.append((v, r.get("_provider", "?")))

        if not values:
            merged[field] = None
            field_sources[field] = []
            continue

        # Numeric fields â€” take median of non-null values
        if field in ("monthly_volume", "transaction_count", "credit_card_pct",
                      "avg_ticket", "effective_rate", "total_fees", "interchange_cost"):
            nums = []
            sources = []
            for v, p in values:
                try:
                    n = float(str(v).replace(",", "").replace("$", "").replace("%", ""))
                    nums.append(n)
                    sources.append(p)
                except (ValueError, TypeError):
                    pass
            if nums:
                nums.sort()
                mid = len(nums) // 2
                merged[field] = round(nums[mid], 2) if len(nums) % 2 else round((nums[mid - 1] + nums[mid]) / 2, 2)
                field_sources[field] = sources
            else:
                merged[field] = None
                field_sources[field] = []
        else:
            # String fields â€” majority vote, fallback to longest
            str_vals = [str(v).strip() for v, _ in values if v]
            sources = [p for _, p in values if _]
            if str_vals:
                counts = Counter(s.lower() for s in str_vals)
                winner_lower = counts.most_common(1)[0][0]
                # Find original-case version
                winner = next((s for s in str_vals if s.lower() == winner_lower), str_vals[0])
                merged[field] = winner
                field_sources[field] = [p for v, p in values if str(v).strip().lower() == winner_lower]
            else:
                merged[field] = None
                field_sources[field] = []

    # Merge findings arrays (deduplicated)
    all_findings = []
    seen = set()
    for r in results:
        for f in r.get("findings", []):
            key = f[:50].lower()
            if key not in seen:
                seen.add(key)
                all_findings.append(f)
    merged["findings"] = all_findings[:10]

    # Confidence scoring
    total_fields = len(FIELDS)
    agreed_fields = sum(1 for f in FIELDS if len(field_sources.get(f, [])) >= 2)
    agree_pct = round((agreed_fields / max(total_fields, 1)) * 100)

    merged["_providerCount"] = len(results)
    merged["_providers"] = [r.get("_provider", "?") for r in results]
    merged["_confidence"] = (
        "certified" if agree_pct >= 80 else
        "high" if agree_pct >= 60 else
        "moderate" if agree_pct >= 40 else
        "review"
    )
    merged["_agreePct"] = agree_pct
    merged["_fieldSources"] = {f: field_sources.get(f, []) for f in FIELDS}

    return merged


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
                        json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]})
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
                        json={"model": "grok-3", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]})
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
    if user.get("role") not in ("admin", "employee"):
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
    result = await _run_all_extractions(req.file_base64, media_type)

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
    }


@router.post("/proposal")
async def generate_proposal(req: ProposalRequest, user=Depends(get_current_user)):
    if user.get("role") not in ("admin", "employee"):
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
                        json={"model": "claude-sonnet-4-20250514", "max_tokens": 1000, "messages": [{"role": "user", "content": prompt}]})
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
                        json={"model": "grok-3", "max_tokens": 1000, "temperature": 0.3, "messages": [{"role": "user", "content": prompt}]})
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
    file_base64: str
    media_type: Optional[str] = None
    file_type: Optional[str] = None
    file_name: Optional[str] = "statement"
    business_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None

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
    media_type = req.resolved_media_type()
    result = await _run_all_extractions(req.file_base64, media_type)

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
    }
