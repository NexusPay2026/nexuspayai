"""
AI Provider Service — server-side orchestration.
Keys are read from Render env vars (never from frontend).
Supports: Anthropic Claude, OpenAI GPT-4o, Google Gemini, xAI Grok.
"""

import json
import httpx
from typing import Optional, List, Dict, Any

from app.config import settings

AI_EXTRACTION_PROMPT = """You are a Senior Forensic Payment Processing Analyst.
Analyze this merchant processing statement and extract ALL financial data precisely.
Use exact numbers from the document. Calculate any value that can be derived from others.

Return ONLY a valid JSON object. No markdown fences, no preamble, no trailing text.
Start your response with { and end with }.

Schema (null only if truly undetectable):
{
  "name": "<exact business name>",
  "processor": "<processor/acquirer name>",
  "statement_month": "<MM/YYYY>",
  "monthly_volume": <float>,
  "total_fees": <float>,
  "interchange_cost": <float>,
  "processor_markup": <float>,
  "monthly_fees": <total fixed recurring fees as float>,
  "transaction_count": <integer>,
  "credit_card_pct": <float 0-100>,
  "avg_ticket": <float>,
  "effective_rate": <total_fees/monthly_volume*100 as float>,
  "interchange_rate": <interchange_cost/monthly_volume*100>,
  "markup_rate": <processor_markup/monthly_volume*100>,
  "risk_score": <integer 0-100, 100=most overcharged>,
  "line_items": [{"name":"<fee name>","category":"interchange|processor|monthly|misc","amount":<float>,"benchmark":<float>,"note":"<1-sentence>"}],
  "findings": [{"text":"<specific finding with exact $ amounts>","severity":"high|medium|low","savings":<annual $ float>}]
}

Include EVERY fee line item visible on the statement in the line_items array.
For findings: flag every fee above benchmark, every negotiable charge, every downgrade opportunity.
Cite exact dollar amounts."""


async def _call_anthropic(file_b64: str, media_type: str) -> Optional[Dict]:
    if not settings.ANTHROPIC_API_KEY:
        return None

    is_pdf = media_type == "application/pdf"
    is_image = media_type.startswith("image/")

    content = []
    if is_pdf:
        content.append({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": file_b64}})
    elif is_image:
        content.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": file_b64}})
    else:
        # text-based
        content.append({"type": "text", "text": f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}"})
    content.append({"type": "text", "text": AI_EXTRACTION_PROMPT})

    headers = {
        "x-api-key": settings.ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    if is_pdf:
        headers["anthropic-beta"] = "pdfs-2024-09-25"

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": content}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Claude API error: {resp.status_code} — {resp.text[:300]}")

    data = resp.json()
    text = data["content"][0]["text"]
    return {"provider": "Claude", "raw": text}


async def _call_openai(file_b64: str, media_type: str) -> Optional[Dict]:
    if not settings.OPENAI_API_KEY:
        return None

    is_image = media_type.startswith("image/")

    msg_content = []
    if is_image:
        msg_content.append({"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{file_b64}"}})
        msg_content.append({"type": "text", "text": AI_EXTRACTION_PROMPT})
    else:
        msg_content.append({"type": "text", "text": f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}\n\n{AI_EXTRACTION_PROMPT}"})

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o",
                "max_tokens": 4096,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": msg_content}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"OpenAI API error: {resp.status_code} — {resp.text[:300]}")

    data = resp.json()
    text = data["choices"][0]["message"]["content"]
    return {"provider": "GPT-4o", "raw": text}


async def _call_gemini(file_b64: str, media_type: str) -> Optional[Dict]:
    if not settings.GOOGLE_API_KEY:
        return None

    parts = []
    if media_type in ("application/pdf",) or media_type.startswith("image/"):
        parts.append({"inlineData": {"mimeType": media_type, "data": file_b64}})
    else:
        parts.append({"text": f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}"})
    parts.append({"text": AI_EXTRACTION_PROMPT})

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={settings.GOOGLE_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Gemini API error: {resp.status_code} — {resp.text[:300]}")

    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return {"provider": "Gemini", "raw": text}


async def _call_grok(file_b64: str, media_type: str) -> Optional[Dict]:
    if not settings.GROK_API_KEY:
        return None

    # xAI Grok uses an OpenAI-compatible API
    msg_content = [{"type": "text", "text": f"MERCHANT PROCESSING STATEMENT:\n\n{file_b64}\n\n{AI_EXTRACTION_PROMPT}"}]

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.GROK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "grok-3",
                "max_tokens": 4096,
                "temperature": 0.1,
                "messages": [{"role": "user", "content": msg_content}],
            },
        )
    if resp.status_code != 200:
        raise Exception(f"Grok API error: {resp.status_code} — {resp.text[:300]}")

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
        raise ValueError("No JSON object found in response")
    json_str = text[start:end + 1]
    # Sanitize literal newlines inside strings
    sanitized = []
    in_str = False
    escape = False
    for ch in json_str:
        if escape:
            sanitized.append(ch)
            escape = False
            continue
        if ch == "\\":
            sanitized.append(ch)
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            sanitized.append(ch)
            continue
        if in_str and ch == "\n":
            sanitized.append("\\n")
            continue
        if in_str and ch == "\r":
            sanitized.append("\\r")
            continue
        sanitized.append(ch)
    return json.loads("".join(sanitized))


async def run_audit_all_providers(file_b64: str, media_type: str) -> Dict[str, Any]:
    """
    Run extraction across all configured AI providers in parallel.
    Returns consensus results.
    """
    import asyncio

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
        raise ValueError("No AI provider keys configured in environment variables")

    results = []
    errors = []

    async def _run(name, func):
        try:
            raw_result = await func(file_b64, media_type)
            if raw_result:
                parsed = _parse_ai_json(raw_result["raw"])
                parsed["_provider"] = name
                results.append(parsed)
        except Exception as e:
            errors.append({"provider": name, "error": str(e)})

    await asyncio.gather(*[_run(name, func) for name, func in providers])

    if not results:
        error_msgs = "; ".join(f"{e['provider']}: {e['error']}" for e in errors)
        raise ValueError(f"All AI providers failed. {error_msgs}")

    if len(results) == 1:
        r = results[0]
        r["_providerCount"] = 1
        r["_confidence"] = "single"
        return r

    # Build consensus from multiple results
    return _build_consensus(results)


def _build_consensus(results: List[Dict]) -> Dict:
    """Merge results from multiple providers using median/majority vote."""
    numeric_fields = [
        "monthly_volume", "total_fees", "interchange_cost", "processor_markup",
        "monthly_fees", "transaction_count", "credit_card_pct", "avg_ticket",
        "effective_rate", "interchange_rate", "markup_rate", "risk_score",
    ]
    string_fields = ["name", "processor", "statement_month"]

    consensus = {}
    agreements = 0
    total_fields = 0

    for field in string_fields:
        values = [str(r.get(field, "")).strip() for r in results if r.get(field)]
        if values:
            from collections import Counter
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

    # Merge line items and findings
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
