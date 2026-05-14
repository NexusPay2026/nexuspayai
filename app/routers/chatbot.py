"""
Calcerta Group chatbot router for NexusPay Intelligence Platform.

Location in repo: nexuspay staging/app/routers/chatbot.py

Wire-up in app/main.py:

  1. Add 'chatbot' to the routers import line:
     from app.routers import auth, merchants, users, visitors, audit, health, storage, quotes, pricing_tool, chatbot

  2. Add the chatbot router include alongside the others:
     app.include_router(chatbot.router, prefix="/api", tags=["Chatbot"])

Required environment variable on Render:
    ANTHROPIC_API_KEY = sk-ant-...

Optional environment variables:
    ANTHROPIC_CHATBOT_MODEL = claude-haiku-4-5  (default; cheapest, fastest)
    CHATBOT_DEBUG = false                       (set true to expose verbose error details)

Endpoints exposed (after prefix="/api" applied at registration):
    GET  /api/chatbot/health   - readiness probe
    POST /api/chatbot/message  - main chat endpoint called by the website widget
"""
import os
import time
import json
import re
import logging
from typing import Optional, List, Literal
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
import httpx

logger = logging.getLogger("calcerta_chatbot")
logger.setLevel(logging.INFO)

# Internal prefix is "/chatbot" - the "/api" prefix is added at registration time
# in main.py (matching the convention used by auth, merchants, users, etc.)
router = APIRouter(prefix="/chatbot", tags=["chatbot"])


# ============================================================
# CONFIG
# ============================================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_CHATBOT_MODEL", "claude-haiku-4-5").strip()
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEBUG_MODE = os.environ.get("CHATBOT_DEBUG", "false").lower() == "true"

# Rate limit: per-IP and per-session (in-memory; resets on Render restart)
RATE_LIMIT_PER_IP_PER_MIN = 30
RATE_LIMIT_PER_SESSION_PER_MIN = 20
_ip_buckets = defaultdict(lambda: deque(maxlen=100))
_session_buckets = defaultdict(lambda: deque(maxlen=100))


# ============================================================
# SYSTEM PROMPT - v2: answer-first, not route-first
# ============================================================
SYSTEM_PROMPT = """You are the Calcerta Group AI assistant - knowledgeable, direct, and genuinely helpful. You are embedded on Calcerta Group's customer-facing website, which represents two operating companies: Interstellar I.S. (telecom, IT, communications) and Nexus Pay (merchant services, payment processing).

YOUR PRIMARY JOB: Answer questions thoroughly and accurately. You have broad knowledge about telecom, payments, merchant services, IT infrastructure, and business technology - use it. Don't deflect to "talk to a human" when you can actually answer.

ABOUT CALCERTA GROUP:
- Customer-facing brand for two separate Colorado LLCs: Interstellar I.S., LLC and Nexus Pay, LLC.
- Founded by Marc Shamp, U.S. Army veteran. Direct line: (720) 735-8800. Email: admin@isinterstellar.com. Address: 11150 E Mississippi Ave, STE 300, Aurora, CO 80012.
- Veteran-owned. Founder-led. No outsourced sales floor.

INTERSTELLAR I.S. - communications & technology infrastructure advisor:
- Master agent / sub-agent across 503+ suppliers in 7 categories: voice & collaboration, contact center (CCaaS), network & connectivity, cybersecurity, cloud & data center, mobility & IoT, AI & customer experience.
- Compensation: paid by suppliers via commission/residual. Client pays $0.
- Process: Discovery > Sourcing > Quoting > Implementation > Lifecycle.
- Typical outcomes: 18-32% annual savings, 72-hour time-to-quote.

NEXUS PAY - merchant services & payment processing sub-ISO:
- Sponsor relationships: Maverick, Beacon, Kurv/EMS, North, CardConnect, Pineapple Payments. 5-8 processing rails.
- Audit-first model: forensic statement audit > benchmark > recommendation. Earns residuals from processors.
- Services: forensic statement audit, card processing setup (interchange-plus, tiered, flat-rate), surcharge/dual pricing programs (C.R.S. 5-2-212 compliant), gateway/terminal hardware, Level II/III optimization, residual transparency.
- Typical recovery: $800-2,400/month in hidden fees identified.

LIFECYCLE COMMITMENTS (both companies):
- 24-hour response on billing escalations
- 90-day notice before any contract auto-renews
- $0 cost to client
- Founder accountability - escalations reach Marc directly

HOW TO HANDLE QUESTIONS:

GENERAL TOPIC QUESTIONS - Answer them. Examples:
- "What's interchange-plus pricing?" -> Explain it clearly.
- "How does SD-WAN compare to MPLS?" -> Compare them.
- "What's Level III data?" -> Explain it.
- "Why do processors hide fees?" -> Discuss the structural reasons (tiered pricing, downgrades, padded interchange).
- "What does a master agent do?" -> Explain the model.
- "Tell me about Calcerta Group" -> Give a real overview.
You can answer these in 4-8 sentences. Be substantive. Show genuine expertise.

QUESTIONS ABOUT OUR SPECIFIC SERVICES - Answer with substance, then naturally offer next steps if appropriate. Examples:
- "Do you do voice services?" -> Yes, here's what we cover. Optional: "Want me to walk through what a discovery call looks like?"
- "Can you help with my processing fees?" -> Yes, here's how the audit works. Optional: "I can pass your details to Marc for a free statement review if you want."

INTENT FLAGS (use sparingly, only when warranted):

intent: "lead_capture" - Set ONLY when the user has clearly expressed buying intent. Examples:
- "I want a quote"
- "Can someone reach out to me"
- "Sign me up"
- "I'd like to schedule a call"
- "Yes, please contact me"

DO NOT trigger lead_capture for:
- General curiosity ("How does this work?")
- Educational questions ("What's interchange?")
- Comparison questions ("Are you better than X?")
- Vague interest ("That sounds interesting")

intent: "human_handoff" - Set ONLY when:
- User explicitly asks: "Can I talk to a person?" "Connect me with the founder" "I want to speak to someone"
- User expresses clear frustration with you specifically
- User asks something genuinely outside your scope (legal advice, specific account lookup, etc.)

intent: "general" - Default for everything else. Just answer the question.

WHAT YOU CAN DISCUSS BUT NOT QUOTE:
- General industry pricing ranges (e.g., "interchange-plus is typically interchange + 0.10-0.50% + 5-15 cents per transaction")
- How pricing models work mechanically
- What factors drive cost up or down

WHAT YOU SHOULDN'T QUOTE:
- A specific dollar figure for THIS merchant or THIS scenario without seeing their statement first
- "You'll save X dollars" type promises

VOICE: Disciplined, founder-led, no fluff. Direct without being curt. Smart without being condescending. You're the AI version of a knowledgeable veteran who actually knows the industry - not a generic customer service bot.

OUTPUT FORMAT - always respond in valid JSON only, no markdown fences:
{
  "reply": "<your message, 2-6 sentences typically. Longer is fine for substantive questions.>",
  "quick_actions": [
    {"label": "<button text 2-4 words>", "action": "send" | "lead" | "human", "value": "<follow-up text if action is send>", "intent": "<context if action is lead>"}
  ],
  "intent": "lead_capture" | "human_handoff" | "general",
  "lead_intent": "<context only if intent is lead_capture>"
}

quick_actions are OPTIONAL. Don't force them on every reply. Use them when:
- The user might naturally want to ask a related follow-up ("Tell me more about Level III")
- The conversation has reached a point where the user might want to take action ("Send my info to Marc")

Default to NO quick_actions for simple educational answers. Let the user drive."""


# ============================================================
# REQUEST / RESPONSE MODELS
# ============================================================
class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=4000)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: List[HistoryMessage] = Field(default_factory=list, max_length=20)
    session_id: str = Field(..., min_length=1, max_length=64)
    property: Literal["interstellar", "nexuspay"] = "interstellar"
    page_url: Optional[str] = Field(None, max_length=500)


class QuickAction(BaseModel):
    label: str = Field(..., max_length=60)
    action: Literal["send", "lead", "human"]
    value: Optional[str] = Field(None, max_length=500)
    intent: Optional[str] = Field(None, max_length=60)


class ChatResponse(BaseModel):
    reply: str
    quick_actions: List[QuickAction] = Field(default_factory=list)
    intent: Optional[Literal["lead_capture", "human_handoff", "general"]] = "general"
    lead_intent: Optional[str] = None


# ============================================================
# RATE LIMITING
# ============================================================
def _check_rate_limit(bucket: deque, limit_per_min: int) -> bool:
    now = time.time()
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= limit_per_min:
        return False
    bucket.append(now)
    return True


# ============================================================
# HEALTH CHECK
# ============================================================
@router.get("/health")
async def chatbot_health():
    """Returns 200 if endpoint is reachable. Reports whether API key is set
    (without exposing the key itself) and which model is configured."""
    return {
        "status": "ok" if ANTHROPIC_API_KEY else "missing_api_key",
        "model": ANTHROPIC_MODEL,
        "configured": bool(ANTHROPIC_API_KEY),
    }


# ============================================================
# MAIN ENDPOINT
# ============================================================
@router.post("/message", response_model=ChatResponse)
async def chatbot_message(req: ChatRequest, request: Request) -> ChatResponse:
    if not ANTHROPIC_API_KEY:
        logger.error("ANTHROPIC_API_KEY environment variable is not set")
        raise HTTPException(status_code=503, detail="Chatbot is not configured")

    # Rate limit
    client_ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(_ip_buckets[client_ip], RATE_LIMIT_PER_IP_PER_MIN):
        raise HTTPException(status_code=429, detail="Too many requests")
    if not _check_rate_limit(_session_buckets[req.session_id], RATE_LIMIT_PER_SESSION_PER_MIN):
        raise HTTPException(status_code=429, detail="Slow down for a moment")

    # Build messages array for Anthropic
    messages = []
    for h in req.history[-10:]:
        messages.append({"role": h.role, "content": h.content})
    messages.append({"role": "user", "content": req.message})

    # Build the system prompt with site context
    system_prompt = SYSTEM_PROMPT + (
        f"\n\nThe user is on the {req.property} site. "
        f"Page URL: {req.page_url or 'unknown'}."
    )

    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 800,
        "system": system_prompt,
        "messages": messages,
    }
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=25.0) as client:
            r = await client.post(ANTHROPIC_API_URL, headers=headers, json=payload)
            r.raise_for_status()
            data = r.json()
    except httpx.HTTPStatusError as e:
        body_excerpt = e.response.text[:500] if e.response is not None else ""
        status = e.response.status_code if e.response else "?"
        logger.error(f"Anthropic API error {status}: {body_excerpt}")
        if DEBUG_MODE:
            raise HTTPException(
                status_code=502,
                detail=f"Anthropic API: {status} {body_excerpt[:200]}",
            )
        raise HTTPException(status_code=502, detail="Upstream AI service error")
    except httpx.RequestError as e:
        logger.error(f"Anthropic request failed: {e}")
        raise HTTPException(status_code=502, detail="Could not reach AI service")
    except Exception:
        logger.exception("Unexpected error calling Anthropic")
        raise HTTPException(status_code=500, detail="Internal error")

    # Extract assistant reply text from Anthropic response
    text_blocks = [
        b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
    ]
    raw_reply = "\n".join(text_blocks).strip()

    # Parse JSON (system prompt instructs JSON output)
    parsed = None
    try:
        cleaned = re.sub(
            r"^```(?:json)?\s*|\s*```\s*$", "", raw_reply, flags=re.MULTILINE
        ).strip()
        parsed = json.loads(cleaned)
    except Exception:
        # Fallback if model didn't return clean JSON for any reason
        logger.warning(f"Model did not return valid JSON. Raw: {raw_reply[:200]}")
        parsed = {
            "reply": raw_reply or "Sorry, I didn't catch that. Could you rephrase?",
            "quick_actions": [],
            "intent": "general",
        }

    # Build safe response object (Pydantic enforces field validation)
    return ChatResponse(
        reply=parsed.get("reply", "Sorry, I didn't catch that."),
        quick_actions=[
            QuickAction(**qa) for qa in (parsed.get("quick_actions") or [])[:4]
        ],
        intent=parsed.get("intent", "general"),
        lead_intent=parsed.get("lead_intent"),
    )