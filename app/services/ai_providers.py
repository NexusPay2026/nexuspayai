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
import os
import re
from collections import Counter
from typing import Optional, List, Dict, Any

import httpx

from app.config import settings

logger = logging.getLogger("nexuspay.ai")

# Engine version — bump on any model/prompt/pipeline change to auto-invalidate
# cached audit results (the portal cache is quality-gated AND version-gated).
ENGINE_VERSION = "2026-06-25"

# Per-provider completion-token ceilings. AI_MAX_TOKENS is the desired budget;
# each provider clamps to its OWN hard limit so one global number can never 400 a
# provider again. This lives in CODE, not env, by design.
_PROVIDER_TOKEN_CAP = {
    "openai": 16000,     # GPT-4o hard limit is 16384
    "anthropic": 64000,  # claude-sonnet-4-6
    "gemini": 64000,     # gemini-2.5-flash
    "grok": 131072,      # grok-4.3: 1M context, no low completion cap
}


def _max_tokens(provider: str) -> int:
    """Clamp the global AI_MAX_TOKENS down to a provider's own ceiling."""
    return min(settings.AI_MAX_TOKENS, _PROVIDER_TOKEN_CAP.get(provider, 16000))


# Per-provider wall-clock cap for the audit gather. The promise is ACCURATE results
# within ~90s, NOT waiting for every provider — a slow/hung provider (e.g. Claude
# ReadTimeout) becomes a named _error and the providers that returned are used.
# 90s leaves ~15-20s headroom under the 120s frontend AbortController (upload +
# rasterize + parse). Env-overridable; keep <= ~100 unless the frontend cap is raised too.
_PROVIDER_TIMEOUT_S = int(os.getenv("AI_PROVIDER_TIMEOUT", "90"))
# ONE retry on a FAST transient failure (busy/blip/truncated-JSON), after this backoff.
# The retry runs INSIDE the per-provider wait_for budget above, so it can never exceed
# the cap or blow the 120s frontend budget: a slow call is cancelled by wait_for ->
# timeout and is NOT retried (a second full attempt would not fit, and a consistently
# slow provider like image-bound Claude won't recover on retry — Tier 2 image
# downscaling is what speeds those up).
_RETRY_BACKOFF_S = float(os.getenv("AI_RETRY_BACKOFF", "1.5"))

# Transient upstream statuses (provider busy / rate-limited / overloaded) worth one retry.
_TRANSIENT_STATUS = ("429", "500", "502", "503", "529")
_TRANSIENT_KEYWORDS = ("unavailable", "overloaded", "high demand", "rate limit", "temporarily")


def _is_retryable(exc: Exception) -> bool:
    """True ONLY for fast transient failures a single retry can plausibly fix: a
    salvage-failed (usually truncated) JSON parse, an httpx connection blip, or a
    transient upstream status (429/5xx/529 'overloaded'). Hard errors (401 auth, 400
    bad request, 404) are NOT retried. A slow call never reaches here — wait_for cancels
    it with a BaseException-level CancelledError, surfaced by _run as a timeout."""
    if isinstance(exc, ValueError):
        return True   # _parse_ai_json raised -> response was malformed/truncated
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError,
                        httpx.WriteError, httpx.RemoteProtocolError, httpx.PoolTimeout)):
        return True
    msg = str(exc).lower()
    if "api error" in msg and any(s in msg for s in _TRANSIENT_STATUS):
        return True
    return any(k in msg for k in _TRANSIENT_KEYWORDS)

AI_EXTRACTION_PROMPT = """You are a forensic payment-processing audit TEAM with CPA-level numerical discipline. Treat this engagement like forensic accounting: every number you report must be traceable to the exact page and the exact printed text it came from.

PROCESS - do this in order, do not skip:
1. Read EVERY page, in order, top to bottom, line by line. Statements often run 3-6 pages and put fee schedules and totals on later pages. Pages are labeled "===== PAGE n OF m =====" (text input) or supplied as page images in order ("Page n of N:"). Record the page number for every value you report.
2. Transcribe EVERY fee, rate, count, and dollar amount exactly as printed - the label and the amount, verbatim. Do NOT paraphrase, round, normalize, or skip "small" items (PCI, regulatory, statement, batch, network access, dues & assessments, downgrades).
3. Classify EVERY value you output as exactly one of:
   - "EXTRACTED" - read directly off the statement. MUST carry its page number and verbatim printed text.
   - "DERIVED"   - computed from two or more EXTRACTED values. MUST carry the formula. Never invent inputs.
   - "ESTIMATED" - not present on the statement and not derivable.
4. NEVER report an ESTIMATED value as a fact or as a finding. If an expected value is not on the statement and cannot be derived, do NOT substitute a benchmark or a guess - list it in "not_found" as {"label":"<name>","status":"NOT_FOUND"}.
5. If the same item appears on two pages with different values, record BOTH in "discrepancies" with page numbers - never silently pick one.
6. Verify your arithmetic before reporting any DERIVED value (e.g., effective_rate = total_fees / monthly_volume * 100). Report only verified figures.
7. "name" and "processor" must NEVER be null or empty. If neither is explicitly labeled, use the strongest evidence candidate: the DBA line, the merchant header, the address block, or the remittance/logo block.

CORE-FIGURE RULE: monthly_volume, total_fees, and transaction_count must be "EXTRACTED" (page + verbatim) or "DERIVED" (formula) - NEVER "ESTIMATED". If one is genuinely absent and cannot be derived, set its value to null and flag it in its provenance entry (class "ESTIMATED", confidence below 0.3) so the audit routes to human review.

Return ONLY one valid JSON object - no markdown fences, no preamble, no trailing text. Start with { and end with }. Inside any "verbatim"/"quote" string: never use the double-quote character (replace it with a single quote), and keep it to AT MOST 10 WORDS - the fee label plus its amount, never the whole line or paragraph - so the JSON stays compact and does not truncate.

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
  "provenance": {
    "monthly_volume":    {"value": <float|null>, "class": "EXTRACTED|DERIVED|ESTIMATED", "page": <int|null>, "quote": "<verbatim text from the statement, or null>", "basis": "<formula if DERIVED, else null>", "confidence": <float 0-1>},
    "total_fees":        {"value": <float|null>, "class": "EXTRACTED|DERIVED|ESTIMATED", "page": <int|null>, "quote": "<verbatim or null>", "basis": "<formula or null>", "confidence": <float 0-1>},
    "transaction_count": {"value": <int|null>,   "class": "EXTRACTED|DERIVED|ESTIMATED", "page": <int|null>, "quote": "<verbatim or null>", "basis": "<formula or null>", "confidence": <float 0-1>},
    "interchange_cost":  {"value": <float|null>, "class": "EXTRACTED|DERIVED|ESTIMATED", "page": <int|null>, "quote": "<verbatim or null>", "basis": "<formula or null>", "confidence": <float 0-1>},
    "processor_markup":  {"value": <float|null>, "class": "EXTRACTED|DERIVED|ESTIMATED", "page": <int|null>, "quote": "<verbatim or null>", "basis": "<formula or null>", "confidence": <float 0-1>},
    "effective_rate":    {"value": <float|null>, "class": "DERIVED|EXTRACTED|ESTIMATED", "page": <int|null>, "quote": "<verbatim or null>", "basis": "total_fees/monthly_volume*100", "confidence": <float 0-1>}
  },
  "line_items": [
    {"name":"<fee/line label exactly as printed>","category":"interchange|processor|monthly|misc","amount":<float>,"page":<int>,"verbatim":"<label + amount as printed, <=10 words>","class":"EXTRACTED|DERIVED","benchmark":<float|null - external market reference ONLY, never a statement value>,"note":"<1 factual sentence; for DERIVED put the formula here>"}
  ],
  "not_found": [
    {"label":"<expected fee or figure that is absent from the statement>","status":"NOT_FOUND"}
  ],
  "discrepancies": [
    {"label":"<item that conflicts across pages>","page_a":<int>,"value_a":<float>,"page_b":<int>,"value_b":<float>}
  ],
  "findings": [
    {"text":"<finding citing the exact EXTRACTED dollar amount>","severity":"high|medium|low","savings":<annual $ float>,"page":<int>,"verbatim":"<label + amount as printed, <=10 words>","class":"EXTRACTED|DERIVED"}
  ]
}

Rules for line_items and findings:
- Every line_item and every finding MUST be "EXTRACTED" (with its page + verbatim text) or "DERIVED" (with the formula in "note"/"text"). If you cannot ground it in the statement, it does NOT belong in line_items or findings - put it in "not_found".
- Include EVERY fee line item visible across ALL pages in line_items. Do not stop until every page and every printed line item is accounted for.
- "benchmark" is an external market reference, not a value from this statement. Set it to null unless you are citing a real, known industry benchmark, and never let a benchmark masquerade as an extracted figure.
- For findings, flag every fee above benchmark, every negotiable charge, and every downgrade opportunity, citing the exact EXTRACTED dollar amount and the page it came from.
- Populate the provenance object for all six listed figures, each with its class, a page + verbatim quote (EXTRACTED) or basis formula (DERIVED), and a confidence."""


QUICK_IDENTITY_PROMPT = """You are extracting the identity/header fields from a merchant processing statement, to auto-fill a form. Read the ENTIRE document, ALL pages, top to bottom - header, footer, address blocks, summary boxes, and fine print. Return ONLY a flat JSON object with these keys. No markdown, no preamble.

SEARCH HARD for these - they are easy to miss:
- statement_date: the statement period / cycle / closing date can appear ANYWHERE - a header, a footer, a 'Statement Period' line, near the account summary, or as a date range. Accept any format ('February 2026', '02/2026', 'Feb 1-28 2026', '2/1/26-2/28/26', a date range) and NORMALIZE to MM/YYYY (use the month the statement covers; for a range use its start month). Only null if there is genuinely no date anywhere.
- processor: the processor / acquirer name may be in a logo area, the page header or footer, a remittance address, or a 'Questions about your statement? Call ...' line. Return the ACTUAL printed name (e.g. 'US EZPAY, INC.', 'Fiserv', 'Worldpay'). NEVER return a generic placeholder like 'Other', 'Unknown', or 'Processor'. Only null if no processor/acquirer identifier appears anywhere.
- zip, email, phone: usually in the merchant address block, which can be on ANY page. Search the whole document.

HARD RULE: use null ONLY when a field is genuinely absent after searching every page. Never guess, never estimate, never substitute a placeholder.

{
  "business_name": "<merchant DBA / business name, or null>",
  "statement_date": "<statement period normalized to MM/YYYY, or null>",
  "zip": "<5-digit business ZIP, or null>",
  "email": "<contact email, or null>",
  "phone": "<contact phone, or null>",
  "monthly_volume": <total monthly processing volume as a number, or null>,
  "total_fees": <total fees charged as a number, or null>,
  "interchange_cost": <interchange cost / interchange & dues+assessments as a number, or null>,
  "processor_markup": <processor markup / profit above interchange as a number, or null>,
  "monthly_fees": <fixed monthly / recurring fees as a number, or null>,
  "transaction_count": <transaction count as an integer, or null>,
  "credit_card_pct": <credit card percent of total volume as a number 0-100, or null>,
  "avg_ticket": <average ticket size as a number, or null>,
  "effective_rate": <total_fees / monthly_volume * 100 as a number if both present, else null>,
  "industry": "<merchant industry / business type, or null>",
  "mcc_code": "<4-digit MCC if shown, or null>",
  "processor": "<actual processor / acquirer name as printed, or null>"
}

Start with { and end with }. Null ONLY when genuinely absent on every page."""


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
#  Multi-page normalization (STEP 2)
#  Turn an uploaded file list into ordered, labeled content units so EVERY page
#  reaches EVERY provider. Photos -> image units; digital PDFs -> per-page text
#  units; scanned PDFs -> rasterized image units; text/csv -> text units. Large
#  images are downscaled to stay under provider per-image limits. The pypdf /
#  PyMuPDF / Pillow imports are lazy and degrade gracefully if a lib is missing.
# ────────────────────────────────────────────────────────────
_PDF_TEXT_MIN_CHARS = 200   # below this total, treat the PDF as scanned -> rasterize
_MAX_IMG_DIM = 1400         # long-edge px cap (was 1568): fewer image tokens to ALL 4
                            # providers + faster vision processing, still legible.
_RASTER_DPI = 120           # scanned-PDF raster resolution (was 150): pixmap memory
                            # scales with DPI^2, so 150->120 cuts it ~36%; text stays readable.
_MAX_RASTER_PAGES = 15      # safety cap on pages rasterized from one PDF


def _downscale_image_bytes(raw: bytes) -> Optional[bytes]:
    """Re-encode to a provider-safe JPEG, downscaling if the long edge exceeds
    _MAX_IMG_DIM. Returns None if Pillow is unavailable (caller keeps original)."""
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed — sending image as-is; add 'Pillow' to requirements.txt")
        return None
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        if max(im.size) > _MAX_IMG_DIM:
            ratio = _MAX_IMG_DIM / float(max(im.size))
            im = im.resize((max(1, int(im.width * ratio)), max(1, int(im.height * ratio))))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except Exception as e:
        logger.warning("image downscale failed: %s", e)
        return None


def _prep_image_unit(b64: str, media_type: str) -> Optional[Dict[str, Any]]:
    """Decode, normalize (JPEG + bounded size), and return an image content unit."""
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return None
    small = _downscale_image_bytes(raw)
    if small is None:
        return {"kind": "image", "b64": b64, "media_type": media_type or "image/jpeg"}
    return {"kind": "image", "b64": base64.b64encode(small).decode("ascii"), "media_type": "image/jpeg"}


def _rasterize_pdf(raw: bytes) -> List[str]:
    """Render each PDF page to a downscaled JPEG (base64). Empty list if PyMuPDF
    is unavailable or rendering fails (caller then falls back gracefully)."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.error("PyMuPDF (fitz) not installed — cannot rasterize scanned PDF; "
                     "add 'PyMuPDF' to requirements.txt")
        return []
    out: List[str] = []
    try:
        doc = fitz.open(stream=raw, filetype="pdf")
        try:
            zoom_dpi = _RASTER_DPI / 72.0
            for i, page in enumerate(doc):
                if i >= _MAX_RASTER_PAGES:
                    break
                # Render at the SMALLER of _RASTER_DPI and the zoom that caps the long
                # edge at _MAX_IMG_DIM, so the pixmap is born small — we never build a
                # full-DPI bitmap or a PNG + Pillow RGB copy (the old path's peak spike).
                long_pts = max(page.rect.width, page.rect.height) or 1.0
                zoom = min(zoom_dpi, _MAX_IMG_DIM / long_pts)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                try:
                    jpg = pix.tobytes(output="jpg", jpg_quality=85)   # native JPEG, no PIL
                except Exception:
                    png = pix.tobytes("png")                          # older-PyMuPDF fallback
                    jpg = _downscale_image_bytes(png) or png
                    del png
                out.append(base64.b64encode(jpg).decode("ascii"))
                pix = None                                            # release native bitmap now
                del jpg
        finally:
            doc.close()
    except Exception as e:
        logger.warning("PDF rasterization failed: %s", e)
        return []
    return out


def _pdf_to_units(b64: str) -> List[Dict[str, Any]]:
    """Per-page text units if the PDF has a real text layer; otherwise rasterize
    each page to an image unit so text-only providers can still read it."""
    try:
        raw = base64.b64decode(b64)
    except Exception:
        return []
    page_texts: List[str] = []
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(raw))
        page_texts = [(p.extract_text() or "").strip() for p in reader.pages]
    except Exception as e:
        logger.warning("pdf text read failed: %s", e)
    if page_texts and sum(len(t) for t in page_texts) >= _PDF_TEXT_MIN_CHARS:
        total = len(page_texts)
        return [{"kind": "text", "text": f"===== PAGE {i} OF {total} =====\n{t}"}
                for i, t in enumerate(page_texts, 1)]
    # scanned / no text layer -> rasterize so ALL providers (incl. GPT-4o, Grok) can read it
    return [{"kind": "image", "b64": b, "media_type": "image/jpeg"} for b in _rasterize_pdf(raw)]


def _b64_to_text(b64: str) -> str:
    try:
        return base64.b64decode(b64).decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def _normalize_pages(pages: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    """Flatten an uploaded file list into ordered, labeled content units."""
    units: List[Dict[str, Any]] = []
    for f in pages or []:
        f = f or {}
        b64 = f.get("base64") or f.get("data") or ""
        mt = (f.get("media_type") or f.get("type") or "").lower()
        if not b64:
            continue
        if mt.startswith("image/"):
            u = _prep_image_unit(b64, mt)
            if u:
                units.append(u)
        elif mt == "application/pdf":
            units.extend(_pdf_to_units(b64))
        else:
            txt = _b64_to_text(b64)
            if txt:
                units.append({"kind": "text", "text": txt})
    total = len(units)
    for i, u in enumerate(units, 1):
        u["label"] = f"Page {i} of {total}"
    return units


def _units_have_images(units: List[Dict[str, Any]]) -> bool:
    return any(u.get("kind") == "image" for u in units)


# Provider-specific builders: label + block per page, then the extraction prompt last.
def _anthropic_content(units: List[Dict[str, Any]], prompt: str = AI_EXTRACTION_PROMPT) -> List[Dict[str, Any]]:
    content: List[Dict[str, Any]] = []
    for u in units:
        content.append({"type": "text", "text": u["label"] + ":"})
        if u["kind"] == "image":
            content.append({"type": "image", "source": {"type": "base64",
                            "media_type": u["media_type"], "data": u["b64"]}})
        else:
            content.append({"type": "text", "text": u["text"]})
    content.append({"type": "text", "text": prompt})
    return content


def _openai_style_content(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """OpenAI- and Grok-compatible chat content (image_url + text blocks)."""
    content: List[Dict[str, Any]] = []
    for u in units:
        content.append({"type": "text", "text": u["label"] + ":"})
        if u["kind"] == "image":
            content.append({"type": "image_url",
                            "image_url": {"url": f"data:{u['media_type']};base64,{u['b64']}"}})
        else:
            content.append({"type": "text", "text": u["text"]})
    content.append({"type": "text", "text": AI_EXTRACTION_PROMPT})
    return content


def _gemini_parts(units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    parts: List[Dict[str, Any]] = []
    for u in units:
        parts.append({"text": u["label"] + ":"})
        if u["kind"] == "image":
            parts.append({"inlineData": {"mimeType": u["media_type"], "data": u["b64"]}})
        else:
            parts.append({"text": u["text"]})
    parts.append({"text": AI_EXTRACTION_PROMPT})
    return parts


# ────────────────────────────────────────────────────────────
#  Provider calls
#  Each receives: native base64 (file_b64), media_type, pdf_text (extracted text
#  for single-file PDFs), and optional `units` (normalized multi-page content).
#  When `units` is provided it takes precedence; otherwise the legacy single-file
#  path runs unchanged.
# ────────────────────────────────────────────────────────────
async def _call_anthropic(file_b64: str, media_type: str, pdf_text: str,
                          units: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict]:
    if not settings.ANTHROPIC_API_KEY:
        return None

    headers = {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    if units is not None:
        # Multi-page: every page as a labeled content block (image or text).
        content = _anthropic_content(units)
    else:
        # Legacy single-file path (unchanged).
        is_pdf = media_type == "application/pdf"
        is_image = media_type.startswith("image/")
        content: List[Dict[str, Any]] = []
        if is_pdf:
            content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64}})
            headers["anthropic-beta"] = "pdfs-2024-09-25"
        elif is_image:
            content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": file_b64}})
        else:
            content.append({"type": "text", "text": f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}"})
        content.append({"type": "text", "text": AI_EXTRACTION_PROMPT})

    async with httpx.AsyncClient(timeout=_http_timeout()) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": settings.ANTHROPIC_MODEL,
                "max_tokens": _max_tokens("anthropic"),
                "temperature": settings.AI_TEMPERATURE,
                "messages": [{"role": "user", "content": content}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Claude API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    return {"provider": "Claude", "raw": text}


async def _call_openai(file_b64: str, media_type: str, pdf_text: str,
                       units: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict]:
    if not settings.OPENAI_API_KEY:
        return None

    if units is not None:
        # Multi-page: labeled image_url/text blocks (scanned pages arrive as images).
        msg_content = _openai_style_content(units)
    else:
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
                "max_tokens": _max_tokens("openai"),   # GPT-4o hard ceiling is 16384
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


async def _call_gemini(file_b64: str, media_type: str, pdf_text: str,
                       units: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict]:
    if not settings.GOOGLE_API_KEY:
        return None

    if units is not None:
        parts = _gemini_parts(units)
    else:
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
                "generationConfig": {
                    "temperature": settings.AI_TEMPERATURE,
                    "maxOutputTokens": _max_tokens("gemini"),
                    "responseMimeType": "application/json",   # force syntactically valid JSON
                },
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Gemini API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    cand = (data.get("candidates") or [{}])[0]
    finish = cand.get("finishReason")
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
    # finishReason lets us tell truncation (MAX_TOKENS) from a safety block from
    # normal completion, instead of surfacing a confusing downstream parse error.
    if finish and finish not in ("STOP", "MAX_TOKENS"):
        raise Exception(f"Gemini returned no usable content (finishReason={finish})")
    if not text.strip():
        block = (data.get("promptFeedback") or {}).get("blockReason")
        raise Exception(f"Gemini empty response (finishReason={finish}, blockReason={block})")
    if finish == "MAX_TOKENS":
        logger.warning("Gemini hit MAX_TOKENS (%d) — output truncated", _max_tokens("gemini"))
    logger.info("Gemini OK: finishReason=%s, %d chars", finish, len(text))
    return {"provider": "Gemini", "raw": text}


async def _call_grok(file_b64: str, media_type: str, pdf_text: str,
                     units: Optional[List[Dict[str, Any]]] = None) -> Optional[Dict]:
    if not settings.GROK_API_KEY:
        return None

    if units is not None:
        # Multi-page: vision model when any page is an image (photos / rasterized
        # scans), text model when every page is text.
        if _units_have_images(units):
            if not settings.GROK_VISION_MODEL:
                raise Exception("Image/scanned pages present but GROK_VISION_MODEL is not set — "
                                "set it in Render (e.g. grok-4.3) so Grok can read images.")
            model = settings.GROK_VISION_MODEL
        else:
            model = settings.GROK_MODEL
        msg_content = _openai_style_content(units)
    else:
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
                "max_tokens": _max_tokens("grok"),
                "temperature": settings.AI_TEMPERATURE,
                "response_format": {"type": "json_object"},   # force syntactically valid JSON (xAI is OpenAI-compatible)
                "messages": [{"role": "user", "content": msg_content}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Grok API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    return {"provider": "Grok", "raw": text}


def _balance_json(s: str) -> str:
    """Best-effort repair of a truncated JSON object: drop a dangling trailing comma
    and append the brackets/braces still open (innermost first)."""
    stack = []
    in_str = False
    escape = False
    for ch in s:
        if escape:
            escape = False; continue
        if ch == "\\":
            escape = True; continue
        if ch == '"':
            in_str = not in_str; continue
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch == "}" and stack and stack[-1] == "{":
            stack.pop()
        elif ch == "]" and stack and stack[-1] == "[":
            stack.pop()
    s2 = s.rstrip()
    if s2.endswith(","):
        s2 = s2[:-1]
    return s2 + "".join("}" if c == "{" else "]" for c in reversed(stack))


def _parse_ai_json(raw_text: str) -> Dict:
    """Extract JSON from an AI response. HARDENED so one malformed field never loses
    the whole extraction (foundational for provenance — verbatim quotes legitimately
    contain quote chars): strips fences/preamble, escapes raw newlines AND stray
    double-quotes inside string values, then falls back through trailing-comma
    removal and brace-balancing for truncated output."""
    text = raw_text.strip()
    text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response (possibly truncated).")
    end = text.rfind("}")
    json_str = text[start:(end + 1) if end != -1 else len(text)]

    out = []
    in_str = False
    escape = False
    n = len(json_str)
    for i, ch in enumerate(json_str):
        if escape:
            out.append(ch); escape = False; continue
        if ch == "\\":
            out.append(ch); escape = True; continue
        if ch == '"':
            if not in_str:
                in_str = True; out.append(ch); continue
            # Inside a string: is this the CLOSING quote, or a stray quote in the value?
            # A real close is followed (after optional whitespace) by , } ] : or EOF.
            j = i + 1
            while j < n and json_str[j] in " \t\r\n":
                j += 1
            if j >= n or json_str[j] in ",}]:":
                in_str = False; out.append(ch)            # genuine close
            else:
                out.append('\\"')                          # stray quote inside value -> escape
            continue
        if in_str and ch == "\n":
            out.append("\\n"); continue
        if in_str and ch == "\r":
            out.append("\\r"); continue
        out.append(ch)
    sanitized = "".join(out)

    for cand in (sanitized,
                 re.sub(r",(\s*[}\]])", r"\1", sanitized),   # drop trailing commas
                 _balance_json(sanitized)):                  # close truncated structures
        try:
            return json.loads(cand)
        except Exception:
            continue
    raise ValueError("Could not parse AI JSON after hardening (malformed or truncated).")


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
        # Retain this provider's raw provenance (page + verbatim quote + class +
        # confidence per figure) so the evidence trail survives to the receipt.
        "provenance": r.get("provenance"),
    }


async def run_audit_all_providers(file_b64: str, media_type: str,
                                  pages: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """
    Run extraction across all configured AI providers in parallel and return
    consensus. Per-provider failures are recorded in result["_errors"] and logged.

    `pages` (optional): an ordered list of uploaded files [{"base64","media_type"}].
    When provided, every page is normalized (photos / digital PDF / scanned PDF /
    text) and handed to EVERY provider as a labeled content block ("Page 1 of N").
    When omitted, the single-file file_b64/media_type path runs unchanged
    (back-compat for /api/audit/run).
    """
    # Multi-page normalization; falls back to the single-file path if nothing usable.
    units = _normalize_pages(pages) if pages else None
    if not units:
        units = None

    # Extract PDF text ONCE so text-only providers see every page (single-file path
    # only; the units path already carries per-page content).
    pdf_text = _extract_pdf_text(file_b64) if (units is None and media_type == "application/pdf") else ""

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

    async def _attempt(name, func):
        """One call + parse, with ONE retry on a fast transient failure. Runs entirely
        inside the wait_for budget in _run, so the retry uses leftover slack and can
        never exceed the per-provider cap (a slow call is cancelled by wait_for and is
        surfaced as a timeout, not retried)."""
        for attempt in (1, 2):
            try:
                raw_result = await func(file_b64, media_type, pdf_text, units)
                if not raw_result:
                    raise ValueError("empty provider response")
                return _parse_ai_json(raw_result["raw"])   # raises ValueError on bad JSON
            except Exception as e:
                if attempt == 1 and _is_retryable(e):
                    logger.info("Provider RETRY: %s after transient failure — %s", name, e)
                    await asyncio.sleep(_RETRY_BACKOFF_S)
                    continue
                raise

    async def _run(name, func):
        try:
            # Cap each provider (INCLUDING its one retry) so a slow/hung one can't blow
            # the budget — it becomes a named timeout error and the rest still count.
            parsed = await asyncio.wait_for(_attempt(name, func), timeout=_PROVIDER_TIMEOUT_S)
            parsed["_provider"] = name
            results.append(parsed)
            logger.info("Provider OK: %s", name)
        except asyncio.TimeoutError:
            errors.append({"provider": name, "error": f"timed out after {_PROVIDER_TIMEOUT_S}s"})
            logger.warning("Provider TIMEOUT: %s (%ds)", name, _PROVIDER_TIMEOUT_S)
        except Exception as e:
            errors.append({"provider": name, "error": str(e) or type(e).__name__})
            logger.warning("Provider FAILED: %s — %s", name, e)

    # return_exceptions=True so a stray failure inside a _run can never abort the whole batch.
    await asyncio.gather(*[_run(name, func) for name, func in providers], return_exceptions=True)

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
        r["_engine_version"] = ENGINE_VERSION
        return r

    consensus = _build_consensus(results)
    consensus["_errors"] = errors       # <-- surfaced even on a successful multi-run
    consensus["_provider_results"] = [_provider_summary(r) for r in results]
    consensus["_engine_version"] = ENGINE_VERSION
    return consensus


async def quick_identity_extract(file_b64: str, media_type: str,
                                 pages: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Fast single-Claude identity extraction for FORM AUTO-FILL (not the forensic
    pass). Reuses _normalize_pages so it works for PDF / scan / photo / screenshot
    without duplicating that layer. Returns flat identity fields (nulls for
    not-found). FAIL-OPEN: returns {} on any error/timeout so an upload is never
    blocked. All model logic stays server-side."""
    if not settings.ANTHROPIC_API_KEY:
        return {}
    try:
        page_list = pages or ([{"base64": file_b64, "media_type": media_type}] if file_b64 else [])
        units = _normalize_pages(page_list)
        if not units:
            return {}
        content = _anthropic_content(units, QUICK_IDENTITY_PROMPT)
        async with httpx.AsyncClient(timeout=_http_timeout()) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": settings.ANTHROPIC_MODEL,
                    "max_tokens": 1024,
                    "temperature": settings.AI_TEMPERATURE,
                    "messages": [{"role": "user", "content": content}],
                },
            )
        if resp.status_code != 200:
            logger.warning("quick_identity_extract: Claude %s: %s", resp.status_code, resp.text[:200])
            return {}
        data = resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        return _parse_ai_json(text)
    except Exception as e:
        logger.warning("quick_identity_extract failed: %s", e)
        return {}


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
