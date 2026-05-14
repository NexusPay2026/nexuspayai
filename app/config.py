"""
Environment configuration — all secrets and settings from Render env vars.
"""

import os
from dataclasses import dataclass, field

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

    # ── AI Provider Keys ─────────────────────────────────────
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
    GROK_API_KEY: str = os.getenv("GROK_API_KEY", "")

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


settings = Settings()
