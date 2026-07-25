"""
Calcerta CMS Console — backend router (mounted at /api/cms).

WHAT THIS IS
------------
The server side of the custom, branded CMS console that replaces Sveltia CMS.
The console frontend is a separate static Netlify site (repo:
NexusPay2026/calcerta-console). This router is the ONLY thing that holds a
GitHub token: the browser authenticates to us with a username/password, gets a
short-lived JWT, and every read/write of site content is proxied through here.

    browser  --(JWT)-->  /api/cms/*  --(server-side GitHub token)-->  GitHub
                                                                        |
                                          commit to the site repo -> Netlify build

The browser NEVER sees a GitHub token.

Wire-up in app/main.py:
  1. add `cms` to the routers import line
  2. app.include_router(cms.router, prefix="/api", tags=["CMS Console"])
  3. append CMS_CONSOLE_ORIGIN to ALLOWED_ORIGINS (see the CORS note below)

REQUIRED environment variables on Render (all five; missing any -> 503):
    CMS_ADMIN_USERNAME       the console sign-in username
    CMS_ADMIN_PASSWORD_HASH  bcrypt (or argon2) hash — NEVER the plaintext.
                             Generate locally with scripts/make_admin_hash.py
    CMS_JWT_SECRET           strong random string, distinct from JWT_SECRET
    CMS_GITHUB_TOKEN         fine-grained PAT with Contents: Read and write
                             on NexusPay2026/calcerta-site only
    CMS_CONSOLE_ORIGIN       exact origin of the console site, e.g.
                             https://calcerta-console.netlify.app

OPTIONAL:
    CMS_JWT_EXPIRE_HOURS     default 8
    CMS_LOGIN_MAX_FAILS      default 5
    CMS_LOGIN_LOCKOUT_MINUTES default 15
    CMS_LOGIN_RATE_PER_IP_PER_MIN default 20
    CMS_MAX_UPLOAD_MB        default 8

SECURITY POSTURE (this service commits to live production sites)
----------------------------------------------------------------
* No plaintext password exists anywhere in code, config, or logs — only a
  bcrypt/argon2 hash, supplied by env.
* The GitHub token is read from env, used server-side only, never returned to
  a client and never logged (see _redact and _gh_request).
* Every endpoint except /login and /health requires a valid, unexpired JWT.
* Writes are restricted by an allowlist of path prefixes; traversal and any
  build/CI/config file are rejected before GitHub is contacted.
* Login is rate limited per IP and locked out per username after repeated
  failures. Failures are logged WITHOUT the submitted password.
* Requests carrying a browser Origin header must match CMS_CONSOLE_ORIGIN.

CORS note
---------
app/main.py installs ONE global CORSMiddleware, so per-router CORS is not
expressible there. CMS_CONSOLE_ORIGIN is appended to that global allowlist so
the console's preflight succeeds; on top of that, _require_console_origin below
rejects any CMS request whose Origin is not exactly CMS_CONSOLE_ORIGIN. That
means the other allowlisted site origins (calcerta.com, nexuspayai.com, ...)
can NOT call /api/cms/* even though they pass the global CORS layer.
"""

import base64
import binascii
import hmac
import hashlib
import json
import logging
import os
import posixpath
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

logger = logging.getLogger("nexuspay.cms")

# Internal prefix is "/cms"; the "/api" prefix is added at registration time in
# main.py (matching auth, merchants, users, chatbot, ...).
router = APIRouter(prefix="/cms", tags=["cms"])

security = HTTPBearer(auto_error=False)


# ============================================================
# MANAGED PROPERTIES
# ------------------------------------------------------------
# v1 ships calcerta only. Adding another site later is a DATA change (a new
# entry here), not a code change — the endpoints are all property-generic.
# ============================================================
PROPERTIES: dict = {
    "calcerta": {
        "label": "Calcerta Group",
        "repo": "NexusPay2026/calcerta-site",
        "branch": "main",
        "site_url": "https://calcerta.com",
        # Fixed-field page data
        "data_file": "src/_data/home.json",
        # Collections (folder-backed markdown)
        "collections": {
            "faqs": {"label": "FAQs", "folder": "src/faqs", "ext": ".md"},
            "pages": {"label": "Pages", "folder": "src/pages", "ext": ".md"},
        },
        "media_folder": "src/assets",
    },
}

# Writes are permitted ONLY under these prefixes, for any configured property.
# Everything else — .eleventy.js, netlify.toml, package.json, .github/workflows,
# src/index.njk, src/_includes — is rejected before GitHub is contacted.
ALLOWED_PATH_PREFIXES = (
    "src/_data/",
    "src/faqs/",
    "src/pages/",
    "src/assets/",
)

# Belt-and-braces: even if a prefix check were ever loosened, these can never be
# written through the console.
BLOCKED_BASENAMES = frozenset(
    {
        ".eleventy.js",
        "netlify.toml",
        "package.json",
        "package-lock.json",
        ".gitignore",
    }
)

IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".avif"})
IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
        "image/svg+xml",
        "image/avif",
    }
)

GITHUB_API = "https://api.github.com"
GITHUB_TIMEOUT = 30.0


# ============================================================
# CONFIG (read per request so Render env changes and tests both work)
# ============================================================
@dataclass(frozen=True)
class CmsConfig:
    username: str
    password_hash: str
    jwt_secret: str
    github_token: str
    console_origin: str
    jwt_expire_hours: int
    max_upload_mb: int


REQUIRED_ENV = (
    "CMS_ADMIN_USERNAME",
    "CMS_ADMIN_PASSWORD_HASH",
    "CMS_JWT_SECRET",
    "CMS_GITHUB_TOKEN",
    "CMS_CONSOLE_ORIGIN",
)


def _env(name: str, default: str = "") -> str:
    # `.strip() or default` so a var that EXISTS but is BLANK behaves as unset —
    # same hardening as app/config.py and chatbot.py.
    return (os.environ.get(name, "") or "").strip() or default


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def missing_env() -> list:
    """Names of required env vars that are unset or blank."""
    return [name for name in REQUIRED_ENV if not _env(name)]


def get_config() -> CmsConfig:
    """Return the CMS config, or raise 503 if the service is not configured.

    Refusing to serve (rather than falling back to a default) is deliberate: a
    console that commits to production must never run in a half-configured,
    insecure state.
    """
    missing = missing_env()
    if missing:
        logger.error("CMS console is not configured; missing env: %s", ", ".join(missing))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "CMS console is not configured on the server. Missing environment "
                f"variable(s): {', '.join(missing)}. Set them in Render and redeploy."
            ),
        )
    return CmsConfig(
        username=_env("CMS_ADMIN_USERNAME"),
        password_hash=_env("CMS_ADMIN_PASSWORD_HASH"),
        jwt_secret=_env("CMS_JWT_SECRET"),
        github_token=_env("CMS_GITHUB_TOKEN"),
        console_origin=_env("CMS_CONSOLE_ORIGIN").rstrip("/"),
        jwt_expire_hours=_int_env("CMS_JWT_EXPIRE_HOURS", 8),
        max_upload_mb=_int_env("CMS_MAX_UPLOAD_MB", 8),
    )


# ============================================================
# PASSWORD VERIFICATION (bcrypt primary, argon2 accepted)
# ============================================================
def verify_password(password: str, stored_hash: str) -> bool:
    """Constant-time-ish verification against a bcrypt or argon2 hash.

    Never logs, echoes, or stores the supplied password. Returns False on any
    malformed hash rather than raising, so a bad env value is an auth failure
    and not a 500 that leaks shape information.
    """
    if not password or not stored_hash:
        return False

    if stored_hash.startswith("$argon2"):
        try:
            import argon2  # optional; only needed if an argon2 hash is configured

            try:
                argon2.PasswordHasher().verify(stored_hash, password)
                return True
            except Exception:
                return False
        except ImportError:
            logger.error(
                "CMS_ADMIN_PASSWORD_HASH is an argon2 hash but argon2-cffi is not "
                "installed. Use a bcrypt hash (scripts/make_admin_hash.py) or add "
                "argon2-cffi to requirements.txt."
            )
            return False

    # bcrypt: $2a$ / $2b$ / $2y$
    try:
        import bcrypt

        return bcrypt.checkpw(password.encode("utf-8"), stored_hash.encode("utf-8"))
    except (ValueError, TypeError):
        logger.error("CMS_ADMIN_PASSWORD_HASH is not a valid bcrypt hash.")
        return False
    except ImportError:  # pragma: no cover - bcrypt is in requirements.txt
        logger.error("bcrypt is not installed; cannot verify the CMS password.")
        return False


# ============================================================
# JWT (manual HS256 — mirrors app/services/auth_service.py, but signed with
# CMS_JWT_SECRET so a console token is not a portal token and vice versa)
# ============================================================
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    s += "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s)


def create_token(username: str, cfg: CmsConfig) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "sub": username,
                "scope": "cms",
                "iat": now,
                "exp": now + cfg.jwt_expire_hours * 3600,
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    sig = hmac.new(cfg.jwt_secret.encode(), signing_input, hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


def decode_token(token: str, cfg: CmsConfig) -> Optional[dict]:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b, payload_b, sig_b = parts
        signing_input = f"{header_b}.{payload_b}".encode()
        expected = hmac.new(cfg.jwt_secret.encode(), signing_input, hashlib.sha256).digest()
        if not hmac.compare_digest(expected, _b64url_decode(sig_b)):
            return None
        payload = json.loads(_b64url_decode(payload_b))
        if payload.get("scope") != "cms":
            return None
        if payload.get("exp", 0) < int(time.time()):
            return None
        return payload
    except (ValueError, TypeError, binascii.Error, json.JSONDecodeError):
        return None


# ============================================================
# LOGIN RATE LIMITING / LOCKOUT
# In-memory, per-process — same pattern and caveats as the auth and chatbot
# limiters (resets on restart; with --workers 2 each worker counts separately,
# so the effective ceiling is per-worker).
# ============================================================
LOGIN_MAX_FAILS = _int_env("CMS_LOGIN_MAX_FAILS", 5)
LOGIN_LOCKOUT_MINUTES = _int_env("CMS_LOGIN_LOCKOUT_MINUTES", 15)
LOGIN_RATE_PER_IP_PER_MIN = _int_env("CMS_LOGIN_RATE_PER_IP_PER_MIN", 20)

_login_ip_buckets = defaultdict(lambda: deque(maxlen=200))   # ip -> attempt timestamps
_login_fail_counts = defaultdict(lambda: deque(maxlen=200))  # username -> failure timestamps
_login_lockouts: dict = {}                                   # username -> unlock epoch


def _rate_ok(bucket: deque, limit_per_min: int) -> bool:
    now = time.time()
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= limit_per_min:
        return False
    bucket.append(now)
    return True


def _lock_seconds_remaining(username: str) -> int:
    until = _login_lockouts.get(username, 0)
    if until > time.time():
        return int(until - time.time())
    if until:
        _login_lockouts.pop(username, None)
    return 0


def _record_login_failure(username: str) -> None:
    now = time.time()
    fails = _login_fail_counts[username]
    window = LOGIN_LOCKOUT_MINUTES * 60
    while fails and fails[0] < now - window:
        fails.popleft()
    fails.append(now)
    if len(fails) >= LOGIN_MAX_FAILS:
        _login_lockouts[username] = now + window
        fails.clear()


def _clear_login_failures(username: str) -> None:
    _login_fail_counts.pop(username, None)
    _login_lockouts.pop(username, None)


# ============================================================
# DEPENDENCIES
# ============================================================
async def require_console_origin(request: Request) -> None:
    """Reject browser requests coming from anywhere but the console origin.

    Server-to-server callers (curl, tests) send no Origin header and are not
    blocked here — they still have to present a valid JWT.
    """
    origin = request.headers.get("origin")
    if not origin:
        return
    cfg = get_config()
    if origin.rstrip("/") != cfg.console_origin:
        logger.warning("CMS request rejected: disallowed origin %r", origin)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This origin is not allowed to use the CMS API.",
        )


async def require_cms_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> dict:
    """Every endpoint except /login and /health depends on this."""
    cfg = get_config()
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )
    payload = decode_token(credentials.credentials, cfg)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired session"
        )
    return payload


# ============================================================
# PATH VALIDATION
# ============================================================
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def validate_content_path(path: str) -> str:
    """Normalise and authorise a repo-relative content path.

    Rejects: empty, absolute, backslashes, control characters, URL-encoded
    traversal, any '..' segment, anything outside ALLOWED_PATH_PREFIXES, and any
    blocked basename. Returns the normalised path on success.
    """
    if not path or not path.strip():
        raise HTTPException(status_code=400, detail="A file path is required.")

    raw = path.strip()

    if _CONTROL_CHARS.search(raw):
        raise HTTPException(status_code=400, detail="Invalid characters in path.")

    # Reject encoded traversal before normalising (%2e%2e%2f and friends).
    if "%" in raw and re.search(r"%2e|%2f|%5c", raw, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="Invalid path.")

    if "\\" in raw:
        raise HTTPException(status_code=400, detail="Invalid path separator.")

    if raw.startswith("/"):
        raise HTTPException(status_code=400, detail="Path must be repo-relative.")

    # Explicit '..' check: catches traversal even where normpath would collapse
    # it into something that happens to look allowed.
    if any(segment == ".." for segment in raw.split("/")):
        raise HTTPException(status_code=400, detail="Path traversal is not allowed.")

    normalised = posixpath.normpath(raw)
    if normalised.startswith(("/", "../")) or normalised == "..":
        raise HTTPException(status_code=400, detail="Path traversal is not allowed.")

    if posixpath.basename(normalised) in BLOCKED_BASENAMES:
        raise HTTPException(
            status_code=403,
            detail="That file cannot be edited from the console.",
        )

    if not normalised.startswith(ALLOWED_PATH_PREFIXES):
        raise HTTPException(
            status_code=403,
            detail=(
                "That path is outside the editable content folders. Allowed: "
                + ", ".join(ALLOWED_PATH_PREFIXES)
            ),
        )

    return normalised


def get_property(name: str) -> dict:
    prop = PROPERTIES.get(name)
    if not prop:
        raise HTTPException(status_code=404, detail=f"Unknown property '{name}'.")
    return prop


# ============================================================
# GITHUB CLIENT
# ============================================================
def _redact(text: str, cfg: CmsConfig) -> str:
    """Defensive: never let a token reach a client response or a log line."""
    if cfg.github_token and cfg.github_token in text:
        text = text.replace(cfg.github_token, "***REDACTED***")
    return text


async def _gh_request(
    method: str,
    path: str,
    cfg: CmsConfig,
    *,
    params: Optional[dict] = None,
    payload: Optional[dict] = None,
) -> httpx.Response:
    """Call the GitHub contents API. The token is set here and nowhere else."""
    url = f"{GITHUB_API}{path}"
    headers = {
        "Authorization": f"Bearer {cfg.github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nexuspay-cms-console",
    }
    try:
        async with httpx.AsyncClient(timeout=GITHUB_TIMEOUT) as client:
            return await client.request(
                method, url, headers=headers, params=params, json=payload
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="GitHub timed out. Try again.")
    except httpx.HTTPError as exc:
        # str(exc) contains the URL, never the Authorization header — redact anyway.
        logger.error("GitHub request failed: %s", _redact(str(exc), cfg))
        raise HTTPException(status_code=502, detail="Could not reach GitHub.")


def _gh_error(resp: httpx.Response, cfg: CmsConfig) -> HTTPException:
    """Translate a GitHub error into a safe client-facing error."""
    try:
        message = resp.json().get("message", "")
    except (ValueError, AttributeError):
        message = ""
    logger.error("GitHub API %s: %s", resp.status_code, _redact(message, cfg))

    if resp.status_code == 401:
        return HTTPException(
            status_code=502,
            detail="GitHub rejected the server credential. Check CMS_GITHUB_TOKEN.",
        )
    if resp.status_code == 403:
        return HTTPException(
            status_code=502,
            detail="GitHub denied the request. Check the token's repository permissions.",
        )
    if resp.status_code == 404:
        return HTTPException(status_code=404, detail="File not found in the site repo.")
    if resp.status_code == 409:
        return HTTPException(
            status_code=409,
            detail="This file changed since you loaded it. Reload and reapply your edit.",
        )
    if resp.status_code == 422:
        return HTTPException(
            status_code=409,
            detail="GitHub rejected the write (stale or missing file version). Reload and retry.",
        )
    return HTTPException(status_code=502, detail="GitHub request failed.")


def _contents_path(prop: dict, path: str) -> str:
    return f"/repos/{prop['repo']}/contents/{path}"


# ============================================================
# SCHEMAS
# ============================================================
class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=500)


class LoginResponse(BaseModel):
    token: str
    username: str
    expires_in: int


class FileWriteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    content: str
    message: str = Field(default="", max_length=500)
    sha: Optional[str] = Field(default=None, max_length=100)


# ============================================================
# ENDPOINTS
# ============================================================
@router.get("/health")
async def cms_health():
    """Readiness probe. Reports WHETHER each var is set — never its value."""
    missing = missing_env()
    return {
        "service": "cms-console",
        "configured": not missing,
        "missing_env": missing,
        "properties": list(PROPERTIES.keys()),
    }


@router.post("/login", response_model=LoginResponse)
async def login(
    req: LoginRequest,
    request: Request,
    _origin: None = Depends(require_console_origin),
):
    cfg = get_config()

    client_ip = request.client.host if request and request.client else "unknown"
    if not _rate_ok(_login_ip_buckets[client_ip], LOGIN_RATE_PER_IP_PER_MIN):
        logger.warning("CMS login throttled for ip=%s", client_ip)
        raise HTTPException(
            status_code=429, detail="Too many attempts. Wait a minute and try again."
        )

    username = req.username.strip()

    locked = _lock_seconds_remaining(username)
    if locked:
        logger.warning("CMS login blocked (locked out) user=%r ip=%s", username, client_ip)
        raise HTTPException(
            status_code=429,
            detail=(
                "Account temporarily locked after too many failed attempts. "
                f"Try again in {locked // 60 + 1} minute(s)."
            ),
        )

    # Compare the username in constant time too, so timing does not reveal
    # whether the username was the wrong half of the pair.
    username_ok = hmac.compare_digest(username, cfg.username)
    password_ok = verify_password(req.password, cfg.password_hash)

    if not (username_ok and password_ok):
        # Log the attempt WITHOUT the submitted password.
        _record_login_failure(username)
        logger.warning("CMS login failed user=%r ip=%s", username, client_ip)
        raise HTTPException(status_code=401, detail="Invalid username or password")

    _clear_login_failures(username)
    logger.info("CMS login ok user=%r ip=%s", username, client_ip)

    return LoginResponse(
        token=create_token(username, cfg),
        username=username,
        expires_in=cfg.jwt_expire_hours * 3600,
    )


@router.post("/logout")
async def logout(user: dict = Depends(require_cms_user)):
    """Stateless JWT: the client discards the token.

    There is no server-side session to destroy. Kept as an explicit endpoint so
    the console has a single place to call, and so sign-outs are auditable.
    """
    logger.info("CMS logout user=%r", user.get("sub"))
    return {"ok": True}


@router.get("/properties")
async def list_properties(
    user: dict = Depends(require_cms_user),
    _origin: None = Depends(require_console_origin),
):
    """The sites this console manages, and their content structure."""
    return {
        "properties": [
            {
                "id": key,
                "label": prop["label"],
                "repo": prop["repo"],
                "branch": prop["branch"],
                "site_url": prop["site_url"],
                "data_file": prop["data_file"],
                "collections": prop["collections"],
                "media_folder": prop["media_folder"],
            }
            for key, prop in PROPERTIES.items()
        ]
    }


@router.get("/file/{property_id}")
async def get_file(
    property_id: str,
    path: str,
    user: dict = Depends(require_cms_user),
    _origin: None = Depends(require_console_origin),
):
    """Read one file. Returns its text plus the sha needed to write it back."""
    cfg = get_config()
    prop = get_property(property_id)
    safe_path = validate_content_path(path)

    resp = await _gh_request(
        "GET",
        _contents_path(prop, safe_path),
        cfg,
        params={"ref": prop["branch"]},
    )
    if resp.status_code != 200:
        raise _gh_error(resp, cfg)

    body = resp.json()
    if isinstance(body, list):
        raise HTTPException(
            status_code=400, detail="That path is a folder. Use /list instead."
        )

    raw = base64.b64decode(body.get("content", "") or "")
    try:
        return {
            "path": safe_path,
            "sha": body.get("sha"),
            "encoding": "utf-8",
            "content": raw.decode("utf-8"),
            "size": body.get("size"),
        }
    except UnicodeDecodeError:
        # Binary (an image): hand back base64 rather than failing.
        return {
            "path": safe_path,
            "sha": body.get("sha"),
            "encoding": "base64",
            "content": base64.b64encode(raw).decode("ascii"),
            "size": body.get("size"),
        }


@router.get("/list/{property_id}")
async def list_folder(
    property_id: str,
    folder: str,
    user: dict = Depends(require_cms_user),
    _origin: None = Depends(require_console_origin),
):
    """List a collection folder (FAQs, Pages, assets)."""
    cfg = get_config()
    prop = get_property(property_id)
    # Validate as a folder path — same allowlist, trailing slash tolerated.
    safe_folder = validate_content_path(folder.rstrip("/") + "/x")
    safe_folder = posixpath.dirname(safe_folder)

    resp = await _gh_request(
        "GET",
        _contents_path(prop, safe_folder),
        cfg,
        params={"ref": prop["branch"]},
    )
    if resp.status_code != 200:
        raise _gh_error(resp, cfg)

    body = resp.json()
    if not isinstance(body, list):
        raise HTTPException(status_code=400, detail="That path is a file, not a folder.")

    return {
        "folder": safe_folder,
        "entries": [
            {
                "name": item.get("name"),
                "path": item.get("path"),
                "sha": item.get("sha"),
                "size": item.get("size"),
                "type": item.get("type"),
            }
            for item in body
            if item.get("type") == "file"
        ],
    }


@router.put("/file/{property_id}")
async def put_file(
    property_id: str,
    req: FileWriteRequest,
    user: dict = Depends(require_cms_user),
    _origin: None = Depends(require_console_origin),
):
    """Commit a file to the site repo. This triggers that site's Netlify build."""
    cfg = get_config()
    prop = get_property(property_id)
    safe_path = validate_content_path(req.path)

    editor = user.get("sub", "console")
    message = req.message.strip() or f"Content update: {safe_path}"

    payload = {
        "message": f"{message}\n\nEdited via CMS console by {editor}.",
        "content": base64.b64encode(req.content.encode("utf-8")).decode("ascii"),
        "branch": prop["branch"],
    }
    # Omit sha entirely when creating a new file; GitHub 422s on sha=null.
    if req.sha:
        payload["sha"] = req.sha

    resp = await _gh_request("PUT", _contents_path(prop, safe_path), cfg, payload=payload)
    if resp.status_code not in (200, 201):
        raise _gh_error(resp, cfg)

    body = resp.json()
    commit = body.get("commit", {})
    logger.info("CMS write user=%r property=%s path=%s", editor, property_id, safe_path)
    return {
        "ok": True,
        "path": safe_path,
        "sha": body.get("content", {}).get("sha"),
        "commit": commit.get("sha"),
        "site_url": prop["site_url"],
    }


@router.delete("/file/{property_id}")
async def delete_file(
    property_id: str,
    path: str,
    sha: str,
    message: str = "",
    user: dict = Depends(require_cms_user),
    _origin: None = Depends(require_console_origin),
):
    """Delete a file from the site repo."""
    cfg = get_config()
    prop = get_property(property_id)
    safe_path = validate_content_path(path)

    if not sha:
        raise HTTPException(status_code=400, detail="A file sha is required to delete.")

    editor = user.get("sub", "console")
    note = message.strip() or f"Delete {safe_path}"
    payload = {
        "message": f"{note}\n\nDeleted via CMS console by {editor}.",
        "sha": sha,
        "branch": prop["branch"],
    }

    resp = await _gh_request(
        "DELETE", _contents_path(prop, safe_path), cfg, payload=payload
    )
    if resp.status_code != 200:
        raise _gh_error(resp, cfg)

    logger.info("CMS delete user=%r property=%s path=%s", editor, property_id, safe_path)
    return {"ok": True, "path": safe_path, "site_url": prop["site_url"]}


@router.post("/media/{property_id}")
async def upload_media(
    property_id: str,
    file: UploadFile = File(...),
    user: dict = Depends(require_cms_user),
    _origin: None = Depends(require_console_origin),
):
    """Upload an image into the property's media folder (src/assets)."""
    cfg = get_config()
    prop = get_property(property_id)

    original = posixpath.basename((file.filename or "").replace("\\", "/"))
    if not original:
        raise HTTPException(status_code=400, detail="A filename is required.")

    # Slugify: keep it predictable and URL-safe; never trust the client's name.
    stem, dot, ext = original.rpartition(".")
    ext = f".{ext.lower()}" if dot else ""
    if ext not in IMAGE_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Only images are allowed ({', '.join(sorted(IMAGE_EXTENSIONS))}).",
        )
    if file.content_type and file.content_type not in IMAGE_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="That file is not a supported image.")

    safe_stem = re.sub(r"[^a-z0-9]+", "-", (stem or "image").lower()).strip("-") or "image"
    filename = f"{safe_stem}{ext}"

    data = await file.read()
    max_bytes = cfg.max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413, detail=f"Image is larger than {cfg.max_upload_mb} MB."
        )
    if not data:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")

    target = validate_content_path(f"{prop['media_folder'].rstrip('/')}/{filename}")
    editor = user.get("sub", "console")

    # Overwrite requires the existing sha; look it up first.
    existing = await _gh_request(
        "GET", _contents_path(prop, target), cfg, params={"ref": prop["branch"]}
    )
    payload = {
        "message": f"Upload {filename}\n\nUploaded via CMS console by {editor}.",
        "content": base64.b64encode(data).decode("ascii"),
        "branch": prop["branch"],
    }
    if existing.status_code == 200:
        body = existing.json()
        if isinstance(body, dict) and body.get("sha"):
            payload["sha"] = body["sha"]

    resp = await _gh_request("PUT", _contents_path(prop, target), cfg, payload=payload)
    if resp.status_code not in (200, 201):
        raise _gh_error(resp, cfg)

    logger.info("CMS media upload user=%r property=%s path=%s", editor, property_id, target)
    return {
        "ok": True,
        "path": target,
        # Public URL on the built site: src/assets -> /assets
        "url": f"/assets/{filename}",
        "sha": resp.json().get("content", {}).get("sha"),
    }
