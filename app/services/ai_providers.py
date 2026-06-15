"""
AI Provider Service — server-side orchestration.

SECURITY: API keys are read from Render env vars via app.config.settings.
They are NEVER sent to or read from the frontend. The browser uploads only the
file; this module calls every provider and returns merged results.

RELIABILITY DESIGN
------------------
- All providers run in parallel; whichever have a valid key participate.
- PDFs are read by EVERY provider:
    * Claude + Gemini receive the native PDF (vision) — handles scans too.
    * GPT-4o + Grok receive server-side-extracted text (they cannot decode a
      base64 PDF as prompt text — the previous bug that silently dropped them).
- max_tokens is high enough that multi-page line-item JSON is not truncated.
- temperature is 0 for deterministic arithmetic (accounting/math reliability).
- Per-provider failures are captured and RETURNED (_errors) and logged — never
  swallowed silently. A partial run no longer looks like a clean single result.
- Model names come from env vars (settings.*_MODEL); upgrade without code edits.
"""

import io
import json
import base64
import logging
import asyncio
from collections import Counter
from typing import Optional, List, Dict, Any

import httpx

from app.config import settings

logger = logging.getLogger("nexuspay.ai")

AI_EXTRACTION_PROMPT = """You are a Senior Forensic Payment Processing Analyst with CPA-level numerical discipline.

Read the ENTIRE statement PAGE BY PAGE, LINE BY LINE. Do not skip any page or any line. Many merchant statements run 3-5 pages; fee schedules and totals frequently appear on later pages, so you MUST process every page to the end before answering.

Extraction rules:
- Use the EXACT numbers printed on the document. Do not estimate or round source figures.
- Capture EVERY fee line item, including small ones (PCI, regulatory, statement, batch, network access, dues & assessments, downgrades).
- Re-derive any value you can compute from others, and VERIFY your arithmetic before reporting it (e.g., effective_rate = total_fees / monthly_volume * 100). Internally double-check each calculation; report only verified figures.
- If a value genuinely does not appear and cannot be derived, use null. Do not invent numbers.
- EXCEPTION - "name" and "processor" must NEVER be null or empty. Every statement identifies the merchant and the processor somewhere. If neither is explicitly labeled, use your best evidence candidate: the DBA line, the merchant header, the address block, the remittance/logo block, or the entity the statement is addressed to. Pick the strongest candidate present rather than returning null.

Return ONLY a valid JSON object. No markdown fences, no preamble, no trailing text. Start with { and end with }.

Schema:
{
  "name": "<exact business name - NEVER null; if unlabeled, use best evidence candidate (DBA line, merchant header, address block)>",
  "processor": "<processor/acquirer name - NEVER null; if unlabeled, use best evidence candidate (logo, header, footer, remittance block)>",
  "statement_month": "<MM/YYYY>",
  "contact_email": "<contact email printed on the statement, or null>",
  "contact_phone": "<contact phone printed on the statement, or null>",
  "industry": "<merchant industry / business type, or null>",
  "mcc_code": "<4-digit MCC code if shown, or null>",
  "monthly_volume": <float>,
  "total_fees": <float>,
  "interchange_cost": <float>,
  "processor_markup": <float>,
  "monthly_fees": <total fixed recurring fees as float>,
  "statement_fee": <float>,
  "monthly_service_fee": <float>,
  "pci_fee": <float>,
  "batch_fee": <float>,
  "debit_pct": <float 0-100>,
  "credit_volume": <float>,
  "debit_volume": <float>,
  "visa_volume": <float>,
  "mc_volume": <float>,
  "amex_volume": <float>,
  "disc_volume": <float>,
  "qualified_pct": <float>,
  "mid_qual_pct": <float>,
  "non_qual_pct": <float>,
  "downgrade_amount": <float>,
  "chargeback_count": <integer>,
  "transaction_count": <integer>,
  "credit_card_pct": <float 0-100>,
  "avg_ticket": <float>,
  "effective_rate": <total_fees/monthly_volume*100 as float>,
  "interchange_rate": <interchange_cost/monthly_volume*100>,
  "markup_rate": <processor_markup/monthly_volume*100>,
  "risk_score": <integer 0-100, 100=most overcharged>,
  "pages_detected": <integer total pages you read>,
  "line_items": [{"name":"<fee name>","category":"interchange|processor|monthly|misc","amount":<float>,"benchmark":<float>,"note":"<1-sentence>"}],
  "findings": [{"text":"<specific finding with exact $ amounts>","severity":"high|medium|low","savings":<annual $ float>}]
}

Include EVERY fee line item visible across ALL pages in line_items.
For findings: flag every fee above benchmark, every negotiable charge, every downgrade opportunity, citing exact dollar amounts. Do not stop until every page and every line item has been accounted for."""


# ────────────────────────────────────────────────────────────
#  PDF text extraction (so text-only providers see every page)
# ────────────────────────────────────────────────────────────
def _extract_pdf_text(file_b64: str) -> str:
    """Extract text from a base64 PDF, page by page. Empty string if no text layer."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.error("pypdf not installed — add 'pypdf' to requirements.txt")
        return ""
    try:
        raw = base64.b64decode(file_b64)
        reader = PdfReader(io.BytesIO(raw))
        total = len(reader.pages)
        chunks = []
        for i, page in enumerate(reader.pages, start=1):
            txt = (page.extract_text() or "").strip()
            chunks.append(f"\n===== PAGE {i} OF {total} =====\n{txt}")
        out = "".join(chunks).strip()
        logger.info("PDF text extraction: %d page(s), %d chars", total, len(out))
        return out
    except Exception as e:
        logger.warning("PDF text extraction failed: %s", e)
        return ""


def _http_timeout() -> httpx.Timeout:
    return httpx.Timeout(settings.AI_TIMEOUT)


# ────────────────────────────────────────────────────────────
#  Provider calls
#  Each receives: native base64 (file_b64), media_type, and pdf_text
#  (extracted text for PDFs; empty for images/plain text input).
# ────────────────────────────────────────────────────────────
async def _call_anthropic(file_b64: str, media_type: str, pdf_text: str) -> Optional[Dict]:
    if not settings.ANTHROPIC_API_KEY:
        return None

    is_pdf = media_type == "application/pdf"
    is_image = media_type.startswith("image/")

    content: List[Dict[str, Any]] = []
    if is_pdf:
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64}})
    elif is_image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": file_b64}})
    else:
        content.append({"type": "text", "text": f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}"})
    content.append({"type": "text", "text": AI_EXTRACTION_PROMPT})

    headers = {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if is_pdf:
        headers["anthropic-beta"] = "pdfs-2024-09-25"

    async with httpx.AsyncClient(timeout=_http_timeout()) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": settings.ANTHROPIC_MODEL,
                "max_tokens": settings.AI_MAX_TOKENS,
                "temperature": settings.AI_TEMPERATURE,
                "messages": [{"role": "user", "content": content}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Claude API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"provider": "Claude", "raw": text}


async def _call_openai(file_b64: str, media_type: str, pdf_text: str) -> Optional[Dict]:
    if not settings.OPENAI_API_KEY:
        return None

    is_image = media_type.startswith("image/")
    is_pdf = media_type == "application/pdf"

    msg_content: List[Dict[str, Any]] = []
    if is_image:
        msg_content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{file_b64}"}})
        msg_content.append({"type": "text", "text": AI_EXTRACTION_PROMPT})
    elif is_pdf:
        if not pdf_text:
            raise Exception("PDF has no extractable text layer (likely scanned). "
                            "GPT-4o needs text or an image; Claude/Gemini handled it via vision.")
        msg_content.append({"type": "text", "text": f"MERCHANT PROCESSING STATEMENT (all pages):\n\n{pdf_text}\n\n{AI_EXTRACTION_PROMPT}"})
    else:
        msg_content.append({"type": "text", "text": f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}\n\n{AI_EXTRACTION_PROMPT}"})

    async with httpx.AsyncClient(timeout=_http_timeout()) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": settings.OPENAI_MODEL,
                "max_tokens": settings.AI_MAX_TOKENS,
                "temperature": settings.AI_TEMPERATURE,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": msg_content}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"OpenAI API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    return {"provider": "GPT-4o", "raw": text}


async def _call_gemini(file_b64: str, media_type: str, pdf_text: str) -> Optional[Dict]:
    if not settings.GOOGLE_API_KEY:
        return None

    parts: List[Dict[str, Any]] = []
    if media_type == "application/pdf" or media_type.startswith("image/"):
        parts.append({"inlineData": {"mimeType": media_type, "data": file_b64}})
    else:
        parts.append({"text": f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}"})
    parts.append({"text": AI_EXTRACTION_PROMPT})

    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{settings.GEMINI_MODEL}:generateContent?key={settings.GOOGLE_API_KEY}")

    async with httpx.AsyncClient(timeout=_http_timeout()) as client:
        resp = await client.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": settings.AI_TEMPERATURE, "maxOutputTokens": settings.AI_MAX_TOKENS},
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Gemini API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return {"provider": "Gemini", "raw": text}


async def _call_grok(file_b64: str, media_type: str, pdf_text: str) -> Optional[Dict]:
    if not settings.GROK_API_KEY:
        return None

    is_image = media_type.startswith("image/")
    is_pdf = media_type == "application/pdf"

    if is_image:
        if not settings.GROK_VISION_MODEL:
            raise Exception("Image input: GROK_VISION_MODEL not set, skipping Grok for this image.")
        model = settings.GROK_VISION_MODEL
        msg_content = [
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{file_b64}"}},
            {"type": "text", "text": AI_EXTRACTION_PROMPT},
        ]
    else:
        model = settings.GROK_MODEL
        if is_pdf:
            if not pdf_text:
                raise Exception("PDF has no extractable text layer (likely scanned); Grok needs text input.")
            body_text = f"MERCHANT PROCESSING STATEMENT (all pages):\n\n{pdf_text}\n\n{AI_EXTRACTION_PROMPT}"
        else:
            body_text = f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}\n\n{AI_EXTRACTION_PROMPT}"
        msg_content = [{"type": "text", "text": body_text}]

    async with httpx.AsyncClient(timeout=_http_timeout()) as client:
        resp = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.GROK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": model,
                "max_tokens": settings.AI_MAX_TOKENS,
                "temperature": settings.AI_TEMPERATURE,
                "messages": [{"role": "user", "content": msg_content}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Grok API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    return {"provider": "Grok", "raw": text}


def _parse_ai_json(raw_text: str) -> Dict:
    """Extract JSON from AI response, handling markdown fences and preamble."""
    text = raw_text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON object found in response (possibly truncated).")
    json_str = text[start:end + 1]
    sanitized = []
    in_str = False
    escape = False
    for ch in json_str:
        if escape:
            sanitized.append(ch); escape = False; continue
        if ch == "\\":
            sanitized.append(ch); escape = True; continue
        if ch == '"':
            in_str = not in_str; sanitized.append(ch); continue
        if in_str and ch == "\n":
            sanitized.append("\\n"); continue
        if in_str and ch == "\r":
            sanitized.append("\\r"); continue
        sanitized.append(ch)
    return json.loads("".join(sanitized))


def provider_status() -> Dict[str, bool]:
    """Which providers have a key configured. Wire to GET /api/audit/providers for live diagnostics."""
    return settings.ai_provider_status


def _provider_summary(r: Dict) -> Dict[str, Any]:
    """Per-provider extraction snapshot kept alongside the consensus."""
    vol = r.get("monthly_volume")
    fees = r.get("total_fees")
    eff = r.get("effective_rate")
    if eff is None and vol and fees:
        try:
            eff = round((float(fees) / float(vol)) * 100, 4)
        except (TypeError, ValueError, ZeroDivisionError):
            eff = None
    return {
        "provider": r.get("_provider"),
        "name": r.get("name"),
        "processor": r.get("processor"),
        "monthly_volume": vol,
        "total_fees": fees,
        "effective_rate": eff,
    }


async def run_audit_all_providers(file_b64: str, media_type: str) -> Dict[str, Any]:
    """
    Run extraction across all configured AI providers in parallel and return
    consensus. Per-provider failures are recorded in result["_errors"] and logged.
    """
    # Extract PDF text ONCE so text-only providers (GPT-4o, Grok) see every page.
    pdf_text = _extract_pdf_text(file_b64) if media_type == "application/pdf" else ""

    providers = []
    if settings.ANTHROPIC_API_KEY:
        providers.append(("Claude", _call_anthropic))
    if settings.OPENAI_API_KEY:
        providers.append(("GPT-4o", _call_openai))
    if settings.GOOGLE_API_KEY:
        providers.append(("Gemini", _call_gemini))
    if settings.GROK_API_KEY:
        providers.append(("Grok", _call_grok))

    if not providers:
        raise ValueError("No AI provider keys configured in environment variables.")

    results: List[Dict] = []
    errors: List[Dict] = []

    async def _run(name, func):
        try:
            raw_result = await func(file_b64, media_type, pdf_text)
            if raw_result:
                parsed = _parse_ai_json(raw_result["raw"])
                parsed["_provider"] = name
                results.append(parsed)
                logger.info("Provider OK: %s", name)
        except Exception as e:
            errors.append({"provider": name, "error": str(e)})
            logger.warning("Provider FAILED: %s — %s", name, e)

    await asyncio.gather(*[_run(name, func) for name, func in providers])

    # Always log the full run summary so partial failures are visible in Render logs.
    logger.info("Audit run: attempted=%d succeeded=%d failed=%d errors=%s",
                len(providers), len(results), len(errors), errors)

    if not results:
        error_msgs = "; ".join(f"{e['provider']}: {e['error']}" for e in errors)
        raise ValueError(f"All AI providers failed. {error_msgs}")

    if len(results) == 1:
        r = results[0]
        r["_providerCount"] = 1
        r["_providers"] = [r.get("_provider")]
        r["_confidence"] = "single"
        r["_errors"] = errors           # <-- now surfaced, not swallowed
        r["_provider_results"] = [_provider_summary(r)]
        return r

    consensus = _build_consensus(results)
    consensus["_errors"] = errors       # <-- surfaced even on a successful multi-run
    consensus["_provider_results"] = [_provider_summary(r) for r in results]
    return consensus


def _build_consensus(results: List[Dict]) -> Dict:
    """Merge results from multiple providers using tolerance-band averaging / median."""
    numeric_fields = [
        "monthly_volume", "total_fees", "interchange_cost", "processor_markup",
        "monthly_fees", "transaction_count", "credit_card_pct", "avg_ticket",
        "effective_rate", "interchange_rate", "markup_rate", "risk_score",
        "statement_fee", "monthly_service_fee", "pci_fee", "batch_fee",
        "debit_pct", "credit_volume", "debit_volume", "visa_volume",
        "mc_volume", "amex_volume", "disc_volume", "qualified_pct",
        "mid_qual_pct", "non_qual_pct", "downgrade_amount", "chargeback_count",
    ]
    string_fields = ["name", "processor", "statement_month"]

    consensus: Dict[str, Any] = {}
    agreements = 0
    total_fields = 0

    for field in string_fields:
        values = [str(r.get(field, "")).strip() for r in results if r.get(field)]
        if values:
            most_common = Counter(v.lower() for v in values).most_common(1)[0][0]
            consensus[field] = next(v for v in values if v.lower() == most_common)
        else:
            consensus[field] = ""

    for field in numeric_fields:
        values = [r[field] for r in results if r.get(field) is not None]
        if not values:
            consensus[field] = None
            continue
        if len(values) == 1:
            consensus[field] = values[0]
            continue

        total_fields += 1
        avg = sum(values) / len(values)
        tolerance = max(abs(avg) * 0.05, 5)
        if (max(values) - min(values)) <= tolerance:
            consensus[field] = round(avg, 2)
            agreements += 1
        else:
            sorted_v = sorted(values)
            mid = len(sorted_v) // 2
            median = sorted_v[mid] if len(sorted_v) % 2 else (sorted_v[mid - 1] + sorted_v[mid]) / 2
            consensus[field] = round(median, 2)

    seen_items = set()
    all_items = []
    for r in results:
        for item in r.get("line_items", []):
            key = (item.get("name", "")).lower().strip()
            if key not in seen_items:
                seen_items.add(key)
                all_items.append(item)
    consensus["line_items"] = all_items

    seen_findings = set()
    all_findings = []
    for r in results:
        for f in r.get("findings", []):
            key = (f.get("text", ""))[:50].lower()
            if key not in seen_findings:
                seen_findings.add(key)
                all_findings.append(f)
    all_findings.sort(key=lambda x: x.get("savings", 0), reverse=True)
    consensus["findings"] = all_findings

    agree_pct = round((agreements / total_fields) * 100) if total_fields else 100
    consensus["_providerCount"] = len(results)
    consensus["_providers"] = [r["_provider"] for r in results]
    consensus["_confidence"] = (
        "certified" if agree_pct >= 90 else
        "high" if agree_pct >= 70 else
        "moderate" if agree_pct >= 50 else
        "review"
    )
    consensus["_agreePct"] = agree_pct
    return consensus
