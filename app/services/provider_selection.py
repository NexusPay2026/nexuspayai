"""
Provider selection — NexusPay sponsor / processor "risk management" engine.

Encodes the contract-driven criteria for choosing which ISO sponsor / merchant
processor program to place a deal on, in priority order:

  1. MERCHANT COST  (DOMINANT — "above all else"): choose the option that costs
     the MERCHANT the least to do business with us.
  2. RISK-BANDED PAYOUT + BRAND RISK: among comparable options, prefer the
     highest payout for the merchant's risk band (low vs moderate/high) while
     mitigating risk to the NexusPay brand / sponsoring ISO.
  3. STRATEGIC / PORTFOLIO VALUE: prefer sponsors that protect the brand and give
     the clearest path to independence — becoming a registered ISO when needed,
     owning our own accounts/clients (not just a monetary residual), and maximum
     flexibility to leave with our portfolio. Also rewards diversification.

ADMIN-ONLY. The ranking and its inputs must never reach staff / ICs / customers.
Staff only ever see ONE number: the rep-split commission from staff_commission().

DATA STATUS
-----------
- COMPUTED per deal: `payout` (NexusPay residual) and `merchant_cost` come from
  the live pricing calc and are passed in as Candidates.
- CONTRACT-SPECIFIC (must come from the signed Schedule A's): each sponsor's
  `brand_risk` and `strategic` scores in SPONSOR_PROFILES. These hold neutral
  PLACEHOLDERS now; `unconfigured_sponsors()` reports any sponsor not yet validated
  so a recommendation is never silently based on guesses. The engine is
  intentionally NOT wired into any endpoint until the real values are in place.
- ROLE_SPLITS are the real rep splits (20/30/10); the 16% reserve, draw, and
  bonuses live in the existing canonical pay-calculator backend (not this repo).

Model note: this is a WEIGHTED blend with merchant cost dominant (not strict
lexicographic) so a trivially-cheaper option can't always override a far better
payout/strategic fit. Switch to strict priority by zeroing the lower weights.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Weights (Tier 1 dominates). Normalized 0..1 sub-scores are blended. ───────
# Tier 1 is a JOINT objective (Marc's revised #1): cheapest to the MERCHANT AND
# highest payout to NexusPay together. The best sponsor is the one whose buy-side
# economics let us undercut the merchant's cost while keeping the largest residual.
WEIGHTS = {
    "merchant_cost": 0.38,   # Tier 1a — cheapest to the merchant
    "payout":        0.37,   # Tier 1b — highest payout to NexusPay (co-dominant)
    "brand_risk":    0.13,   # Tier 2  — lower risk to brand/ISO (amplified on riskier deals)
    "strategic":     0.12,   # Tier 3  — independence / ownership / flexibility / diversity
}

# Staff see only ONE number: their commission = their split of the NexusPay residual.
# Split varies by employment type (Marc, 2026-06-14):
ROLE_SPLITS = {
    "w2_commission": 0.20,   # W2, commission-only with a draw
    "ic":            0.30,   # 1099 independent contractor
    "w2_salary":     0.10,   # W2, salary + commission
}
DEFAULT_REP_SPLIT = 0.20
# NOTE: the AUTHORITATIVE comp calc — 16% reserve/overhead, the draw, and one-time
# payout bonuses — already lives in NexusPay's existing pay-calculator backend, which
# is NOT in this repo. staff_commission() below applies ONLY the role split to the
# residual; it must be reconciled with (or deferred to) that canonical calc before
# it drives real pay.


@dataclass
class SponsorProfile:
    name: str
    brand_risk: int            # 1 (safest for brand/ISO) .. 5 (riskiest)  -> lower better
    strategic: int             # 1 (pure monetary / locked-in) .. 5 (clear path to
                               #    registered ISO, own accounts/clients, max
                               #    flexibility to leave)         -> higher better
    configured: bool = False   # True once an admin sets real contract values


# Neutral placeholders (brand_risk=3, strategic=3, configured=False) until set.
SPONSOR_PROFILES: Dict[str, SponsorProfile] = {
    "Beacon Traditional": SponsorProfile("Beacon Traditional", 3, 3),
    "Beacon Flex":        SponsorProfile("Beacon Flex",        3, 3),
    "North":              SponsorProfile("North",              3, 3),
    "Kurv / EMS":         SponsorProfile("Kurv / EMS",         3, 3),
    "Maverick":           SponsorProfile("Maverick",           3, 3),
    # "CardConnect", "Pineapple Payments" — add when contracts are active
}


@dataclass
class Candidate:
    program: str           # must match a SPONSOR_PROFILES key
    payout: float          # NexusPay residual for THIS deal ($/mo) — higher better
    merchant_cost: float   # what the merchant pays ($/mo) — lower better


@dataclass
class Ranked:
    program: str
    score: float
    payout: float
    merchant_cost: float
    staff_commission: float
    breakdown: Dict[str, float] = field(default_factory=dict)
    configured: bool = False


def staff_commission(nexuspay_residual: float, role: str = "w2_commission") -> float:
    """
    The single number staff see: their split of the NexusPay residual.
      role -> split:  w2_commission 20%, ic 30%, w2_salary 10%.
    Does NOT apply the 16% reserve / draw / one-time bonuses — those belong to the
    canonical pay-calculator backend and must be reconciled before this drives pay.
    """
    split = ROLE_SPLITS.get((role or "").lower(), DEFAULT_REP_SPLIT)
    return round((nexuspay_residual or 0) * split, 2)


def _norm(values: List[float], invert: bool) -> List[float]:
    """Min-max normalize to 0..1. invert=True => lower raw value scores higher."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [1.0 for _ in values]
    scaled = [(v - lo) / (hi - lo) for v in values]
    return [(1.0 - x) for x in scaled] if invert else scaled


def _brand_risk_weight(risk_band: str) -> float:
    """Brand-risk mitigation counts for more on moderate/high-risk merchants."""
    return {"low": 0.5, "moderate": 1.0, "high": 1.5}.get((risk_band or "low").lower(), 1.0)


def rank_providers(candidates: List[Candidate], risk_band: str = "low",
                   role: str = "w2_commission") -> List[Ranked]:
    """
    Rank sponsor programs for a single deal, best first, by NexusPay's criteria.
    Returns Ranked items (ADMIN-ONLY); each carries the rep-split staff number.
    """
    if not candidates:
        return []

    cost_s = _norm([c.merchant_cost for c in candidates], invert=True)   # lower better
    pay_s  = _norm([c.payout for c in candidates],        invert=False)  # higher better

    profiles = [SPONSOR_PROFILES.get(c.program) for c in candidates]
    brisk_s = _norm([(p.brand_risk if p else 3) for p in profiles], invert=True)   # lower risk better
    strat_s = _norm([(p.strategic  if p else 3) for p in profiles], invert=False)  # higher better

    brw = _brand_risk_weight(risk_band)

    ranked: List[Ranked] = []
    for c, cs, ps, brs, sts, prof in zip(candidates, cost_s, pay_s, brisk_s, strat_s, profiles):
        parts = {
            "merchant_cost": round(WEIGHTS["merchant_cost"] * cs, 4),
            "payout":        round(WEIGHTS["payout"] * ps, 4),
            "brand_risk":    round(WEIGHTS["brand_risk"] * brs * brw, 4),
            "strategic":     round(WEIGHTS["strategic"] * sts, 4),
        }
        ranked.append(Ranked(
            program=c.program,
            score=round(sum(parts.values()), 4),
            payout=round(c.payout, 2),
            merchant_cost=round(c.merchant_cost, 2),
            staff_commission=staff_commission(c.payout, role),
            breakdown=parts,
            configured=bool(prof and prof.configured),
        ))

    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def recommend(candidates: List[Candidate], risk_band: str = "low") -> Optional[Ranked]:
    """Top-ranked program, or None. configured=False => based on placeholder data."""
    ranked = rank_providers(candidates, risk_band)
    return ranked[0] if ranked else None


def unconfigured_sponsors() -> List[str]:
    """Sponsors still on placeholder contract values — recommendations are DRAFT until empty."""
    return [name for name, p in SPONSOR_PROFILES.items() if not p.configured]
