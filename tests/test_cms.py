"""
Smoke tests for the CMS console router (app/routers/cms.py).

Coverage required by the build spec:
  * auth required on every endpoint except /login and /health
  * login rate limiting / lockout
  * path-allowlist rejection (traversal, build files, CI files)
  * 503 when the service is not configured

No network: GitHub is never reached. Any test that could plausibly touch it
installs a _gh_request stub that FAILS the test if it is called, which is how we
prove the path allowlist rejects before contacting GitHub.
"""

import time

import bcrypt
import pytest

from app.routers import cms

# Cheap rounds — these hashes exist only for the length of a test run.
TEST_USERNAME = "calcerta-admin"
TEST_PASSWORD = "Correct-Horse-Battery-9!"
TEST_HASH = bcrypt.hashpw(TEST_PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()

CONSOLE_ORIGIN = "https://calcerta-console.netlify.app"

# Every endpoint that must refuse an unauthenticated caller.
PROTECTED_REQUESTS = [
    ("get", "/api/cms/properties", {}),
    ("get", "/api/cms/file/calcerta", {"params": {"path": "src/_data/home.json"}}),
    ("get", "/api/cms/list/calcerta", {"params": {"folder": "src/faqs"}}),
    ("put", "/api/cms/file/calcerta", {"json": {"path": "src/faqs/a.md", "content": "x"}}),
    (
        "delete",
        "/api/cms/file/calcerta",
        {"params": {"path": "src/faqs/a.md", "sha": "abc"}},
    ),
    ("post", "/api/cms/logout", {}),
]

# Paths the console must never be able to write.
FORBIDDEN_PATHS = [
    "../netlify.toml",
    "src/_data/../../netlify.toml",
    "src/faqs/../../../etc/passwd",
    "netlify.toml",
    ".eleventy.js",
    "package.json",
    ".github/workflows/ci.yml",
    "src/index.njk",
    "src/_includes/page.njk",
    "src/404.html",
    "/etc/passwd",
    "src\\_data\\home.json",
    "src/_data/%2e%2e/netlify.toml",
    "",
    "   ",
]

# Paths that are legitimately editable.
ALLOWED_PATHS = [
    "src/_data/home.json",
    "src/faqs/who-is-calcerta-group.md",
    "src/pages/about.md",
    "src/assets/photo.jpg",
]


@pytest.fixture
def configured(monkeypatch):
    """Configure the CMS with test credentials."""
    monkeypatch.setenv("CMS_ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("CMS_ADMIN_PASSWORD_HASH", TEST_HASH)
    monkeypatch.setenv("CMS_JWT_SECRET", "cms-test-secret-not-a-real-one")
    monkeypatch.setenv("CMS_GITHUB_TOKEN", "ghp_fake_token_for_tests_only")
    monkeypatch.setenv("CMS_CONSOLE_ORIGIN", CONSOLE_ORIGIN)
    return True


@pytest.fixture
def unconfigured(monkeypatch):
    """Guarantee every required var is absent."""
    for name in cms.REQUIRED_ENV:
        monkeypatch.delenv(name, raising=False)
    return True


@pytest.fixture
def no_github(monkeypatch):
    """Fail loudly if anything tries to reach GitHub."""

    async def _boom(*args, **kwargs):
        raise AssertionError("GitHub must not be contacted in this test")

    monkeypatch.setattr(cms, "_gh_request", _boom)
    return True


def auth_header(username=TEST_USERNAME, **overrides):
    cfg = cms.get_config()
    if overrides:
        cfg = cms.CmsConfig(**{**cfg.__dict__, **overrides})
    return {"Authorization": f"Bearer {cms.create_token(username, cfg)}"}


# ── 503 when unconfigured ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_health_reports_unconfigured(client, unconfigured):
    """/health stays up even when unconfigured — it is the diagnostic."""
    r = await client.get("/api/cms/health")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert set(body["missing_env"]) == set(cms.REQUIRED_ENV)


@pytest.mark.asyncio
async def test_login_503_when_unconfigured(client, unconfigured):
    r = await client.post(
        "/api/cms/login", json={"username": "x", "password": "y"}
    )
    assert r.status_code == 503
    assert "not configured" in r.json()["detail"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("method,url,kwargs", PROTECTED_REQUESTS)
async def test_endpoints_503_when_unconfigured(client, unconfigured, method, url, kwargs):
    r = await getattr(client, method)(url, **kwargs)
    assert r.status_code == 503


@pytest.mark.asyncio
async def test_503_names_the_missing_variable(client, monkeypatch, unconfigured):
    """A partially-configured service must still refuse, and say what is missing."""
    monkeypatch.setenv("CMS_ADMIN_USERNAME", TEST_USERNAME)
    monkeypatch.setenv("CMS_ADMIN_PASSWORD_HASH", TEST_HASH)
    r = await client.post(
        "/api/cms/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert r.status_code == 503
    detail = r.json()["detail"]
    assert "CMS_JWT_SECRET" in detail
    assert "CMS_GITHUB_TOKEN" in detail


@pytest.mark.asyncio
async def test_blank_env_var_counts_as_unset(client, monkeypatch, configured):
    """A Render var that EXISTS but is blank must not be treated as configured."""
    monkeypatch.setenv("CMS_GITHUB_TOKEN", "   ")
    r = await client.get("/api/cms/properties", headers={"Authorization": "Bearer x"})
    assert r.status_code == 503
    assert "CMS_GITHUB_TOKEN" in r.json()["detail"]


# ── Auth required ───────────────────────────────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("method,url,kwargs", PROTECTED_REQUESTS)
async def test_auth_required(client, configured, no_github, method, url, kwargs):
    r = await getattr(client, method)(url, **kwargs)
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token", ["", "garbage", "a.b.c", "Bearer", "x" * 200]
)
async def test_malformed_token_rejected(client, configured, no_github, token):
    r = await client.get(
        "/api/cms/properties", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_token_signed_with_other_secret_rejected(client, configured, monkeypatch):
    """A token minted with a different secret must not be accepted."""
    cfg = cms.get_config()
    forged = cms.create_token(
        TEST_USERNAME, cms.CmsConfig(**{**cfg.__dict__, "jwt_secret": "attacker-secret"})
    )
    r = await client.get(
        "/api/cms/properties", headers={"Authorization": f"Bearer {forged}"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_portal_token_is_not_a_cms_token(client, configured):
    """A valid *portal* JWT must not unlock the CMS (different secret + scope)."""
    from app.services.auth_service import create_token as portal_token

    r = await client.get(
        "/api/cms/properties",
        headers={"Authorization": f"Bearer {portal_token('u1', 'a@b.c', 'admin')}"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_expired_token_rejected(client, configured, monkeypatch):
    monkeypatch.setenv("CMS_JWT_EXPIRE_HOURS", "-1")
    headers = auth_header()
    r = await client.get("/api/cms/properties", headers=headers)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_valid_token_reaches_properties(client, configured):
    r = await client.get("/api/cms/properties", headers=auth_header())
    assert r.status_code == 200
    props = r.json()["properties"]
    assert [p["id"] for p in props] == ["calcerta"]
    assert props[0]["repo"] == "NexusPay2026/calcerta-site"


@pytest.mark.asyncio
async def test_properties_never_leaks_the_github_token(client, configured):
    r = await client.get("/api/cms/properties", headers=auth_header())
    assert "ghp_fake_token_for_tests_only" not in r.text


# ── Login + rate limiting / lockout ─────────────────────────────────────────
@pytest.mark.asyncio
async def test_login_success(client, configured):
    r = await client.post(
        "/api/cms/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == TEST_USERNAME
    assert body["expires_in"] == 8 * 3600
    assert cms.decode_token(body["token"], cms.get_config()) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "username,password",
    [
        (TEST_USERNAME, "wrong-password"),
        ("wrong-user", TEST_PASSWORD),
        ("wrong-user", "wrong-password"),
    ],
)
async def test_login_failure_401(client, configured, username, password):
    r = await client.post(
        "/api/cms/login", json={"username": username, "password": password}
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid username or password"


@pytest.mark.asyncio
async def test_login_locks_out_after_max_fails(client, configured):
    """5 failures for one username -> 429 lockout."""
    for attempt in range(cms.LOGIN_MAX_FAILS):
        r = await client.post(
            "/api/cms/login",
            json={"username": TEST_USERNAME, "password": f"wrong-{attempt}"},
        )
        assert r.status_code == 401, f"attempt {attempt} should be a plain 401"

    r = await client.post(
        "/api/cms/login", json={"username": TEST_USERNAME, "password": "wrong-again"}
    )
    assert r.status_code == 429
    assert "locked" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_lockout_blocks_even_the_correct_password(client, configured):
    """The lockout is not bypassable by then supplying the right password."""
    for attempt in range(cms.LOGIN_MAX_FAILS):
        await client.post(
            "/api/cms/login",
            json={"username": TEST_USERNAME, "password": f"wrong-{attempt}"},
        )

    r = await client.post(
        "/api/cms/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert r.status_code == 429


@pytest.mark.asyncio
async def test_lockout_is_per_username(client, configured):
    """Locking out one username must not lock a different one."""
    for attempt in range(cms.LOGIN_MAX_FAILS):
        await client.post(
            "/api/cms/login",
            json={"username": "someone-else", "password": f"wrong-{attempt}"},
        )

    r = await client.post(
        "/api/cms/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_lockout_expires(client, configured, monkeypatch):
    for attempt in range(cms.LOGIN_MAX_FAILS):
        await client.post(
            "/api/cms/login",
            json={"username": TEST_USERNAME, "password": f"wrong-{attempt}"},
        )
    assert cms._lock_seconds_remaining(TEST_USERNAME) > 0

    # Wind the stored unlock time into the past rather than sleeping 15 minutes.
    cms._login_lockouts[TEST_USERNAME] = time.time() - 1
    assert cms._lock_seconds_remaining(TEST_USERNAME) == 0

    r = await client.post(
        "/api/cms/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_successful_login_clears_failure_count(client, configured):
    for attempt in range(cms.LOGIN_MAX_FAILS - 1):
        await client.post(
            "/api/cms/login",
            json={"username": TEST_USERNAME, "password": f"wrong-{attempt}"},
        )
    ok = await client.post(
        "/api/cms/login", json={"username": TEST_USERNAME, "password": TEST_PASSWORD}
    )
    assert ok.status_code == 200
    assert TEST_USERNAME not in cms._login_fail_counts

    # A fresh failure run must again take the full MAX_FAILS to lock.
    r = await client.post(
        "/api/cms/login", json={"username": TEST_USERNAME, "password": "wrong"}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_ip_throttle_returns_429(client, configured, monkeypatch):
    monkeypatch.setattr(cms, "LOGIN_RATE_PER_IP_PER_MIN", 3)
    codes = []
    for i in range(5):
        r = await client.post(
            "/api/cms/login", json={"username": f"u{i}", "password": "x"}
        )
        codes.append(r.status_code)
    assert 429 in codes, f"expected a throttled response, got {codes}"


@pytest.mark.asyncio
async def test_login_does_not_log_the_password(client, configured, caplog):
    with caplog.at_level("WARNING", logger="nexuspay.cms"):
        await client.post(
            "/api/cms/login",
            json={"username": TEST_USERNAME, "password": "SuperSecret-Sentinel-42"},
        )
    assert "SuperSecret-Sentinel-42" not in caplog.text
    assert "login failed" in caplog.text.lower()


# ── Path allowlist ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", FORBIDDEN_PATHS)
def test_validate_content_path_rejects(path):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        cms.validate_content_path(path)
    assert exc.value.status_code in (400, 403)


@pytest.mark.parametrize("path", ALLOWED_PATHS)
def test_validate_content_path_accepts(path):
    assert cms.validate_content_path(path) == path


@pytest.mark.asyncio
@pytest.mark.parametrize("path", FORBIDDEN_PATHS)
async def test_put_rejects_forbidden_path(client, configured, no_github, path):
    """Rejected before GitHub is contacted — no_github asserts that."""
    r = await client.put(
        "/api/cms/file/calcerta",
        headers=auth_header(),
        json={"path": path, "content": "malicious", "message": "nope"},
    )
    assert r.status_code in (400, 403, 422)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", FORBIDDEN_PATHS)
async def test_get_rejects_forbidden_path(client, configured, no_github, path):
    r = await client.get(
        "/api/cms/file/calcerta", headers=auth_header(), params={"path": path}
    )
    assert r.status_code in (400, 403, 422)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", FORBIDDEN_PATHS)
async def test_delete_rejects_forbidden_path(client, configured, no_github, path):
    r = await client.delete(
        "/api/cms/file/calcerta",
        headers=auth_header(),
        params={"path": path, "sha": "deadbeef"},
    )
    assert r.status_code in (400, 403, 422)


@pytest.mark.asyncio
async def test_list_rejects_forbidden_folder(client, configured, no_github):
    r = await client.get(
        "/api/cms/list/calcerta",
        headers=auth_header(),
        params={"folder": "../.github/workflows"},
    )
    assert r.status_code in (400, 403)


@pytest.mark.asyncio
async def test_unknown_property_404(client, configured, no_github):
    r = await client.get(
        "/api/cms/file/not-a-site",
        headers=auth_header(),
        params={"path": "src/_data/home.json"},
    )
    assert r.status_code == 404


# ── Origin enforcement ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_disallowed_origin_rejected(client, configured, no_github):
    """calcerta.com is in the global CORS allowlist but must not reach the CMS."""
    r = await client.get(
        "/api/cms/properties",
        headers={**auth_header(), "Origin": "https://calcerta.com"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_console_origin_allowed(client, configured):
    r = await client.get(
        "/api/cms/properties",
        headers={**auth_header(), "Origin": CONSOLE_ORIGIN},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_login_from_disallowed_origin_rejected(client, configured):
    r = await client.post(
        "/api/cms/login",
        headers={"Origin": "https://evil.example"},
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert r.status_code == 403


# ── Media upload guards ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_media_requires_auth(client, configured, no_github):
    r = await client.post(
        "/api/cms/media/calcerta", files={"file": ("a.png", b"x", "image/png")}
    )
    assert r.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "filename,content_type",
    [
        ("evil.js", "text/javascript"),
        ("evil.html", "text/html"),
        ("config.yml", "text/yaml"),
        ("noextension", "image/png"),
    ],
)
async def test_media_rejects_non_image(client, configured, no_github, filename, content_type):
    r = await client.post(
        "/api/cms/media/calcerta",
        headers=auth_header(),
        files={"file": (filename, b"payload", content_type)},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_media_rejects_oversize(client, configured, monkeypatch, no_github):
    monkeypatch.setenv("CMS_MAX_UPLOAD_MB", "1")
    r = await client.post(
        "/api/cms/media/calcerta",
        headers=auth_header(),
        files={"file": ("big.png", b"x" * (2 * 1024 * 1024), "image/png")},
    )
    assert r.status_code == 413


# ── Password verification ───────────────────────────────────────────────────
def test_verify_password_roundtrip():
    assert cms.verify_password(TEST_PASSWORD, TEST_HASH) is True
    assert cms.verify_password("not-it", TEST_HASH) is False


@pytest.mark.parametrize(
    "bad_hash", ["", "plaintext-password", "$2b$broken", "not$a$hash"]
)
def test_verify_password_survives_a_bad_hash(bad_hash):
    """A malformed CMS_ADMIN_PASSWORD_HASH is an auth failure, never a 500."""
    assert cms.verify_password("anything", bad_hash) is False


def test_verify_password_rejects_empty():
    assert cms.verify_password("", TEST_HASH) is False
