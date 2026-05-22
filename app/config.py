"""
Environment configuration — all secrets and settings from Render env vars.

SECURITY MODEL
--------------
Every secret (API keys, DB URL, JWT secret) is read here, server-side, from
Render environment variables. Nothing in this file is ever sent to the browser.
Rotating a key = update the single env var in Render; Render restarts and the
new value is picked up automatically. No code edits, no source redeploys.

Model names are ALSO env-overridable so you can upgrade to a stronger model
later (e.g. a newer reasoning model better at accounting/math) by changing one
env var instead of editing code.
"""

import os
from dataclasses import dataclass


@dataclass
class Settings:
    # ── Environment ──────────────────────────────────────────
    APP_ENV: str = os.getenv("APP_ENV", "production")

    # ── Database ─────────────────────────────────────────────
    DATABASE_URL: str = os.getenv("DATABASE_URL", "")

    # ── JWT / Auth ───────────────────────────────────────────
    JWT_SECRET: str = os.getenv("JWT_SECRET", "CHANGE-ME-IN-PRODUCTION")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_HOURS: int = int(os.getenv("JWT_EXPIRE_HOURS", "72"))

    # ── AI Provider Keys (server-side only) ──────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    GROK_API_KEY: str = os.getenv("GROK_API_KEY", "")

    # ── AI Model Names (env-overridable; defaults are known-good) ──
    # To upgrade a model, set the matching env var in Render — no code change.
    ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
    OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    GROK_MODEL: str = os.getenv("GROK_MODEL", "grok-3")
    # Optional vision-capable Grok model for image statements (leave blank to skip Grok on images)
    GROK_VISION_MODEL: str = os.getenv("GROK_VISION_MODEL", "")

    # ── AI Generation Tuning (accuracy-first) ────────────────
    # max_tokens raised so multi-page line-item JSON is never truncated.
    AI_MAX_TOKENS: int = int(os.getenv("AI_MAX_TOKENS", "8192"))
    # temperature 0 = deterministic arithmetic; best for accounting/math reliability.
    AI_TEMPERATURE: float = float(os.getenv("AI_TEMPERATURE", "0"))
    # per-call HTTP timeout in seconds (Render cold starts + large PDFs)
    AI_TIMEOUT: int = int(os.getenv("AI_TIMEOUT", "150"))

    # ── Cloudflare R2 ────────────────────────────────────────
    R2_ACCOUNT_ID: str = os.getenv("R2_ACCOUNT_ID", "")
    R2_ACCESS_KEY_ID: str = os.getenv("R2_ACCESS_KEY_ID", "")
    R2_SECRET_ACCESS_KEY: str = os.getenv("R2_SECRET_ACCESS_KEY", "")
    R2_BUCKET_NAME: str = os.getenv("R2_BUCKET_NAME", "nexuspay-storage")
    R2_PUBLIC_URL: str = os.getenv("R2_PUBLIC_URL", "")

    # ── Frontend URLs (for CORS, redirects, emails) ──────────
    WEBSITE_URL: str = os.getenv("WEBSITE_URL", "https://nexuspayservices.com")
    LANDING_PAGE_URL: str = os.getenv("LANDING_PAGE_URL", "https://freeanalysis.nexuspayservices.com")
    PORTAL_URL: str = os.getenv("PORTAL_URL", "https://nexuspayai.com")
    DASHBOARD_URL: str = os.getenv("DASHBOARD_URL", "https://nexuspaydashboard.netlify.app")
    API_BASE_URL: str = os.getenv("API_BASE_URL", "https://nexuspay-api-ochi.onrender.com")

    # ── Microsoft Bookings ───────────────────────────────────
    MICROSOFT_BOOKINGS_URL: str = os.getenv("MICROSOFT_BOOKINGS_URL", "")

    # ── Admin ────────────────────────────────────────────────
    ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "admin@nexuspayservices.com")

    # ── Webhook ──────────────────────────────────────────────
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "")

    def __post_init__(self):
        if not self.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. "
                "Set it in Render environment variables pointing to your Postgres instance. "
                "SQLite fallback is disabled in production."
            )
        # Render uses postgres:// but asyncpg needs postgresql://
        if self.DATABASE_URL.startswith("postgres://"):
            self.DATABASE_URL = self.DATABASE_URL.replace("postgres://", "postgresql://", 1)

    @property
    def async_database_url(self) -> str:
        return self.DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def ai_provider_status(self) -> dict:
        """Which providers have a key configured (True/False). For diagnostics."""
        return {
            "Claude": bool(self.ANTHROPIC_API_KEY),
            "GPT-4o": bool(self.OPENAI_API_KEY),
            "Gemini": bool(self.GOOGLE_API_KEY),
            "Grok": bool(self.GROK_API_KEY),
        }


settings = Settings()
