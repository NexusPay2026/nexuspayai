"""
Calcerta chatbot smoke tests — POST /api/chatbot/message.

The endpoint does not touch the database; it calls the Anthropic Messages API
over httpx. Every test mocks that outbound call (no network) and drives the
module-level API key + rate-limit buckets directly, so these run against the
default in-memory sqlite config like the rest of the suite.

Covers: 503-when-unconfigured, request validation (422), happy-path JSON parse,
graceful degrade when the model returns non-JSON, and the per-session rate
limit (429).
"""

import json

import pytest

from app.routers import chatbot as cb

MESSAGE = "/api/chatbot/message"


def _body(**over):
    b = {"message": "What is interchange-plus pricing?",
         "session_id": "sess-default", "property": "interstellar"}
    b.update(over)
    return b


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _fake_anthropic_client(reply_text):
    """Return a drop-in for httpx.AsyncClient whose .post() yields a single
    Anthropic text block containing `reply_text`."""

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _FakeResp({"content": [{"type": "text", "text": reply_text}]})

    return _FakeClient


@pytest.fixture(autouse=True)
def _reset_chatbot_state(monkeypatch):
    """Chatbot state is process-global: reset the rate-limit buckets between
    tests and default to a configured API key. Individual tests override the
    key or the httpx client as needed; monkeypatch reverts after each test."""
    cb._ip_buckets.clear()
    cb._session_buckets.clear()
    monkeypatch.setattr(cb, "ANTHROPIC_API_KEY", "sk-ant-test-fake")
    yield
    cb._ip_buckets.clear()
    cb._session_buckets.clear()


async def test_message_503_when_unconfigured(client, monkeypatch):
    monkeypatch.setattr(cb, "ANTHROPIC_API_KEY", "")
    r = await client.post(MESSAGE, json=_body())
    assert r.status_code == 503
    assert "configured" in r.json()["detail"].lower()


async def test_message_empty_message_422(client):
    r = await client.post(MESSAGE, json=_body(message=""))
    assert r.status_code == 422


async def test_message_missing_session_id_422(client):
    r = await client.post(MESSAGE, json={"message": "hello"})
    assert r.status_code == 422


async def test_message_too_long_422(client):
    r = await client.post(MESSAGE, json=_body(message="x" * 2001))
    assert r.status_code == 422


async def test_message_happy_path_parses_model_json(client, monkeypatch):
    reply = json.dumps({
        "reply": "Interchange-plus passes network interchange straight through plus a fixed markup.",
        "quick_actions": [
            {"label": "Book an audit", "action": "lead", "intent": "pricing_review"}
        ],
        "intent": "general",
    })
    monkeypatch.setattr(cb.httpx, "AsyncClient", _fake_anthropic_client(reply))

    r = await client.post(MESSAGE, json=_body(session_id="sess-happy"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert "interchange" in data["reply"].lower()
    assert data["intent"] == "general"
    assert data["quick_actions"][0]["action"] == "lead"
    assert data["quick_actions"][0]["intent"] == "pricing_review"


async def test_message_degrades_gracefully_on_non_json(client, monkeypatch):
    monkeypatch.setattr(cb.httpx, "AsyncClient", _fake_anthropic_client("plain text, not json"))

    r = await client.post(MESSAGE, json=_body(session_id="sess-degrade"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["reply"] == "plain text, not json"
    assert data["quick_actions"] == []
    assert data["intent"] == "general"


async def test_message_strips_markdown_json_fence(client, monkeypatch):
    fenced = "```json\n" + json.dumps({"reply": "Hi there.", "intent": "general"}) + "\n```"
    monkeypatch.setattr(cb.httpx, "AsyncClient", _fake_anthropic_client(fenced))

    r = await client.post(MESSAGE, json=_body(session_id="sess-fence"))
    assert r.status_code == 200, r.text
    assert r.json()["reply"] == "Hi there."


async def test_message_per_session_rate_limit_429(client, monkeypatch):
    monkeypatch.setattr(cb.httpx, "AsyncClient", _fake_anthropic_client('{"reply":"ok","intent":"general"}'))

    codes = []
    for _ in range(cb.RATE_LIMIT_PER_SESSION_PER_MIN + 2):
        rr = await client.post(MESSAGE, json=_body(session_id="sess-rl"))
        codes.append(rr.status_code)

    assert codes.count(200) == cb.RATE_LIMIT_PER_SESSION_PER_MIN, codes
    assert codes[-1] == 429, codes
