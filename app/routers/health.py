"""Health check endpoint."""

import os
import subprocess
from datetime import datetime, timezone
from fastapi import APIRouter
from app.services.r2_storage import r2_available
from app.config import settings

router = APIRouter()


def _resolve_git_commit() -> str:
    """Resolve the deployed commit SHA so each environment can be matched to a build.

    Render injects RENDER_GIT_COMMIT automatically; Netlify uses COMMIT_REF.
    Falls back to reading local git (dev), then 'unknown'.
    """
    for var in ("RENDER_GIT_COMMIT", "COMMIT_REF", "GIT_COMMIT", "SOURCE_COMMIT"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
    except Exception:
        return "unknown"


def _resolve_git_branch() -> str:
    for var in ("RENDER_GIT_BRANCH", "BRANCH", "GIT_BRANCH"):
        val = os.environ.get(var)
        if val:
            return val
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"], stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
    except Exception:
        return "unknown"


# Resolved once at startup — the deployed commit does not change while the process runs.
GIT_COMMIT = _resolve_git_commit()
GIT_BRANCH = _resolve_git_branch()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nexuspay-unified-api",
        "version": "4.0.0",
        "git": {
            "commit": GIT_COMMIT,
            "commit_short": GIT_COMMIT[:7] if GIT_COMMIT != "unknown" else "unknown",
            "branch": GIT_BRANCH,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "r2_configured": r2_available(),
        "ai_providers": {
            "anthropic": bool(settings.ANTHROPIC_API_KEY),
            "openai": bool(settings.OPENAI_API_KEY),
            "google": bool(settings.GOOGLE_API_KEY),
            "grok": bool(settings.GROK_API_KEY),
        },
    }
