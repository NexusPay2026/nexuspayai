"""
Pricing AI Analysis Service — calls all 4 AI providers for deal intelligence.
Text-only prompts (no file upload needed). Returns consensus recommendation.
"""

import json
import asyncio
import httpx
from typing import Dict, Any, List, Optional

from app.config import settings

BENCH_RATES = {
    "Auto Shop":2.9, "Convenience Store":2.5, "Smoke / CBD / Vape":3.8,
    "Restaurant / QSR":2.6, "Retail":2.4, "E-Commerce":3.1,
    "Professional Services":2.7, "Healthcare / Dental":2.8,
    "Salon / Spa":2.6, "Gas Station":2.2, "Other":2.8,
}

# Employee floors — system blocks, no override
EMP_FLOORS = {
    "bt":  {"markup": 0.50, "auth": 0.10},
    "bf":  {"markup": 0.60, "auth": 0.10, "max_total": 4.00},
    "ml":  {"markup": 0.47, "auth": 0.10},
    "mm":  {"markup": 0.60, "auth": 0.11},
    "mh":  {"markup": 0.72, "auth": 0.13},
}

# Admin floors — breakeven + 0.05% buffer
ADMIN_FLOORS = {
    "bt":  {"markup": 0.25, "auth": 0.06},
    "bf":  {"markup": 0.25, "auth": 0.05, "max_total": 4.00},
    "ml":  {"markup": 0.25, "auth": 0.05},
    "mm":  {"markup": 0.30, "auth": 0.07},
    "mh":  {"markup": 0.40, "auth": 0.09},
}

# Surcharge rules
SURCHARGE_CO_MAX_ABOVE_BUY = 2.00  # Colorado: max 2% above buy cost
SURCHARGE_VISA_MAX = 3.00
SURCHARGE_MC_MAX = 4.00
NEXUSPAY_BUFFER = 0.05  # 0.05% buffer above buy cost for all states


def validate_pricing(role: str, risk: str, markup: float, auth: float,
                     program: str = "all", state: str = "CO",
                     pricing_model: str = "interchange_plus") -> Dict:
    """
    Validate pricing against role-based floors and surcharge caps.
    Returns: {valid: bool, blocked: [], warnings: [], floors: {}}
    """
    blocked = []
    warnings = []
    needs_approval = False

    rk = "bt" if program == "beacon_trad" else \
         "bf" if program == "beacon_flex" else \
         "ml" if risk == "low" else "mm" if risk == "moderate" else "mh"

    floors = ADMIN_FLOORS if role == "admin" else EMP_FLOORS
    floor = floors.get(rk, floors.get("bt"))

    # Markup floor check
    if markup < floor["markup"]:
        if role == "admin":
            # Admin below admin floor = hard block (losing money)
            blocked.append(f"Markup {markup:.2f}% is below breakeven floor of {floor['markup']:.2f}% — NexusPay loses money")
        else:
            blocked.append(f"Markup {markup:.2f}% is below your minimum of {floor['markup']:.2f}% — contact admin for override")

    # Auth floor check
    if auth < floor["auth"]:
        if role == "admin":
            blocked.append(f"Auth fee ${auth:.2f} is below breakeven floor of ${floor['auth']:.2f}")
        else:
            blocked.append(f"Auth fee ${auth:.2f} is below your minimum of ${floor['auth']:.2f} — contact admin for override")

    # Employee pricing between employee floor and target = needs admin review
    if role == "employee":
        emp_f = EMP_FLOORS.get(rk, EMP_FLOORS["bt"])
        if markup <= emp_f["markup"] + 0.10:
            needs_approval = True
            warnings.append(f"Tight margin at {markup:.2f}% — deal flagged for admin review before boarding")

    # Dual pricing / cash discount max cap
    if program in ("beacon_flex", "dual_pricing", "cash_discount"):
        max_total = floor.get("max_total", 4.00)
        if markup > max_total:
            blocked.append(f"Dual pricing / cash discount total {markup:.2f}% exceeds maximum {max_total:.2f}% allowed")

    # Surcharge compliance
    if pricing_model == "surcharge":
        if state == "CO":
            if markup > SURCHARGE_CO_MAX_ABOVE_BUY:
                blocked.append(f"Colorado surcharge {markup:.2f}% exceeds max 2.00% above NexusPay buy cost")
        else:
            # Other states: card brand caps after buy cost + buffer
            if markup > SURCHARGE_VISA_MAX + NEXUSPAY_BUFFER:
                warnings.append(f"Surcharge {markup:.2f}% may exceed Visa max of {SURCHARGE_VISA_MAX}% in some states")
            if markup > SURCHARGE_MC_MAX + NEXUSPAY_BUFFER:
                blocked.append(f"Surcharge {markup:.2f}% exceeds Mastercard max of {SURCHARGE_MC_MAX}%")

    return {
        "valid": len(blocked) == 0,
        "blocked": blocked,
        "warnings": warnings,
        "needs_approval": needs_approval,
        "role": role,
        "floor_markup": floor["markup"],
        "floor_auth": floor["auth"],
    }


PRICING_PROMPT = """You are a Senior Merchant Services Pricing Strategist for NexusPay, a veteran-owned payment processing company operating as a sub-ISO.

Analyze this merchant deal and provide a strategic pricing recommendation.

MERCHANT PROFILE:
- Name: {merchant_name}
- Vertical: {vertical}
- Risk Level: {risk_level}
- Monthly Volume: ${volume:,.2f}
- Monthly Transactions: {transactions:,}
- Average Ticket: ${avg_ticket:,.2f}
- Multi-Location: {multi_loc} ({locations} location(s))

PRICING AS QUOTED:
- Markup above interchange: {markup}%
- Auth/per-item fee: ${auth}
- Monthly fee: ${monthly}

NEXUSPAY RESIDUAL RESULTS:
- Beacon Traditional (75% split): ${bt_residual:.2f}/mo
- Beacon Flex / Dual Pricing (50% split): ${bf_residual:.2f}/mo{nt_line}{kv_line}
- Maverick {risk_label} ({mv_split}% split): ${mv_residual:.2f}/mo

INDUSTRY CONTEXT:
- Average effective rate for {vertical}: approximately {bench_rate}%
- This merchant would pay approximately {eff_rate}% effective (interchange + markup)

Return ONLY a valid JSON object with no markdown fences:
{{
  "recommended_program": "<Beacon Traditional|Beacon Flex|North|Kurv / EMS|Maverick>",
  "reasoning": "<2-3 sentences explaining why this program is best for this specific deal>",
  "deal_strength": "<strong|competitive|marginal|weak>",
  "competitive_position": "<how this pricing compares to what the merchant likely pays now and industry norms>",
  "negotiation_tips": ["<actionable tip 1>", "<actionable tip 2>", "<actionable tip 3>"],
  "risk_factors": ["<risk or concern 1>", "<risk or concern 2>"],
  "merchant_pitch": "<3-4 sentence proposal summary written as if speaking directly to the merchant — focus on savings and value, not internal margins>",
  "internal_notes": "<1-2 sentences of internal-only strategy notes for the NexusPay rep>",
  "optimal_markup_pct": <float suggested markup>,
  "annual_nexuspay_value": <float projected annual residual>,
  "win_probability": <integer 0-100>,
  "multi_location_strategy": "<strategy note if multi-location, otherwise empty string>"
}}"""


async def _ai_anthropic(prompt: str) -> Optional[Dict]:
    if not settings.ANTHROPIC_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key": settings.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-sonnet-4-20250514", "max_tokens": 2048, "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        raise Exception(f"Claude: {r.status_code}")
    return {"provider": "Claude", "raw": r.json()["content"][0]["text"]}


async def _ai_openai(prompt: str) -> Optional[Dict]:
    if not settings.OPENAI_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={"model": "gpt-4o", "max_tokens": 2048, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        raise Exception(f"GPT-4o: {r.status_code}")
    return {"provider": "GPT-4o", "raw": r.json()["choices"][0]["message"]["content"]}


async def _ai_gemini(prompt: str) -> Optional[Dict]:
    if not settings.GOOGLE_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={settings.GOOGLE_API_KEY}",
            headers={"Content-Type": "application/json"},
            json={"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048}})
    if r.status_code != 200:
        raise Exception(f"Gemini: {r.status_code}")
    return {"provider": "Gemini", "raw": r.json()["candidates"][0]["content"]["parts"][0]["text"]}


async def _ai_grok(prompt: str) -> Optional[Dict]:
    if not settings.GROK_API_KEY:
        return None
    async with httpx.AsyncClient(timeout=90) as c:
        r = await c.post("https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {settings.GROK_API_KEY}", "Content-Type": "application/json"},
            json={"model": "grok-3", "max_tokens": 2048, "temperature": 0.2, "messages": [{"role": "user", "content": prompt}]})
    if r.status_code != 200:
        raise Exception(f"Grok: {r.status_code}")
    return {"provider": "Grok", "raw": r.json()["choices"][0]["message"]["content"]}


def _extract_json(raw: str) -> Dict:
    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON found")
    return json.loads(text[start:end + 1])


async def analyze_deal(
    merchant_name: str, vertical: str, risk_level: str,
    volume: float, transactions: int, markup: float, auth: float, monthly: float,
    bt_residual: float, bf_residual: float, mv_residual: float,
    nt_residual: float = 0, kv_residual: float = 0,
    multi_loc: bool = False, locations: int = 1,
) -> Dict[str, Any]:
    """Run all 4 AI providers on a pricing deal and return consensus analysis."""

    avg_ticket = volume / max(transactions, 1)
    bench = BENCH_RATES.get(vertical, 2.8)
    mv_split = "90" if risk_level == "low" else "80" if risk_level == "moderate" else "60"
    risk_label = risk_level.capitalize() + "-Risk"
    eff_rate = 1.8 + markup  # approximate: avg IC ~1.8% + markup

    # Build conditional processor lines — only include if residual is non-zero
    nt_line = ""
    if nt_residual and nt_residual != 0:
        nt_line = f"\n- North (70% split): ${nt_residual:.2f}/mo"

    kv_line = ""
    if kv_residual and kv_residual != 0:
        kv_split_pct = "50" if risk_level == "high" else "80"
        kv_line = f"\n- Kurv / EMS ({kv_split_pct}% split): ${kv_residual:.2f}/mo"

    prompt = PRICING_PROMPT.format(
        merchant_name=merchant_name or "Unnamed",
        vertical=vertical, risk_level=risk_level,
        volume=volume, transactions=transactions, avg_ticket=avg_ticket,
        multi_loc="Yes" if multi_loc else "No", locations=locations,
        markup=markup, auth=auth, monthly=monthly,
        bt_residual=bt_residual, bf_residual=bf_residual, mv_residual=mv_residual,
        nt_line=nt_line, kv_line=kv_line,
        mv_split=mv_split, risk_label=risk_label,
        bench_rate=bench, eff_rate=f"{eff_rate:.2f}",
    )

    providers = []
    if settings.ANTHROPIC_API_KEY:
        providers.append(("Claude", _ai_anthropic))
    if settings.OPENAI_API_KEY:
        providers.append(("GPT-4o", _ai_openai))
    if settings.GOOGLE_API_KEY:
        providers.append(("Gemini", _ai_gemini))
    if settings.GROK_API_KEY:
        providers.append(("Grok", _ai_grok))

    if not providers:
        raise ValueError("No AI provider keys configured")

    results = []
    errors = []

    async def _run(name, func):
        try:
            raw = await func(prompt)
            if raw:
                parsed = _extract_json(raw["raw"])
                parsed["_provider"] = name
                results.append(parsed)
        except Exception as e:
            errors.append({"provider": name, "error": str(e)})

    await asyncio.gather(*[_run(n, f) for n, f in providers])

    if not results:
        err_msg = "; ".join(f"{e['provider']}: {e['error']}" for e in errors)
        raise ValueError(f"All providers failed: {err_msg}")

    # Single provider
    if len(results) == 1:
        r = results[0]
        r["_providerCount"] = 1
        r["_confidence"] = "single"
        r["_errors"] = errors
        return r

    # Consensus from multiple providers
    consensus = _build_pricing_consensus(results)
    consensus["_errors"] = errors
    return consensus


def _build_pricing_consensus(results: List[Dict]) -> Dict:
    """Merge pricing analyses from multiple providers."""
    from collections import Counter

    # Majority vote on recommended program
    programs = [r.get("recommended_program", "") for r in results]
    prog_counts = Counter(p for p in programs if p)
    consensus_program = prog_counts.most_common(1)[0][0] if prog_counts else "Beacon Traditional"

    # Majority vote on deal strength
    strengths = [r.get("deal_strength", "") for r in results]
    str_counts = Counter(s for s in strengths if s)
    consensus_strength = str_counts.most_common(1)[0][0] if str_counts else "competitive"

    # Average numerics
    markups = [r.get("optimal_markup_pct", 0) for r in results if r.get("optimal_markup_pct")]
    annuals = [r.get("annual_nexuspay_value", 0) for r in results if r.get("annual_nexuspay_value")]
    win_probs = [r.get("win_probability", 50) for r in results if r.get("win_probability")]

    # Collect all tips, risks, pick best pitch
    all_tips = []
    all_risks = []
    for r in results:
        all_tips.extend(r.get("negotiation_tips", []))
        all_risks.extend(r.get("risk_factors", []))

    # Deduplicate tips/risks by first 40 chars
    seen_tips = set()
    unique_tips = []
    for t in all_tips:
        k = t[:40].lower()
        if k not in seen_tips:
            seen_tips.add(k)
            unique_tips.append(t)

    seen_risks = set()
    unique_risks = []
    for r in all_risks:
        k = r[:40].lower()
        if k not in seen_risks:
            seen_risks.add(k)
            unique_risks.append(r)

    # Use longest merchant_pitch (usually most detailed)
    pitches = [r.get("merchant_pitch", "") for r in results if r.get("merchant_pitch")]
    best_pitch = max(pitches, key=len) if pitches else ""

    internals = [r.get("internal_notes", "") for r in results if r.get("internal_notes")]
    best_internal = max(internals, key=len) if internals else ""

    multi_strats = [r.get("multi_location_strategy", "") for r in results if r.get("multi_location_strategy")]
    best_multi = max(multi_strats, key=len) if multi_strats else ""

    comp_positions = [r.get("competitive_position", "") for r in results if r.get("competitive_position")]
    best_comp = max(comp_positions, key=len) if comp_positions else ""

    agree_count = sum(1 for p in programs if p == consensus_program)
    agree_pct = round((agree_count / len(results)) * 100)

    return {
        "recommended_program": consensus_program,
        "reasoning": next((r.get("reasoning", "") for r in results if r.get("recommended_program") == consensus_program), ""),
        "deal_strength": consensus_strength,
        "competitive_position": best_comp,
        "negotiation_tips": unique_tips[:5],
        "risk_factors": unique_risks[:4],
        "merchant_pitch": best_pitch,
        "internal_notes": best_internal,
        "optimal_markup_pct": round(sum(markups) / len(markups), 3) if markups else 0,
        "annual_nexuspay_value": round(sum(annuals) / len(annuals), 2) if annuals else 0,
        "win_probability": round(sum(win_probs) / len(win_probs)) if win_probs else 50,
        "multi_location_strategy": best_multi,
        "_providerCount": len(results),
        "_providers": [r["_provider"] for r in results],
        "_confidence": "certified" if agree_pct >= 90 else "high" if agree_pct >= 70 else "moderate" if agree_pct >= 50 else "review",
        "_agreePct": agree_pct,
    }
