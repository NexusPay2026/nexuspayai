"""Health check endpoint."""

from datetime import datetime, timezone
from fastapi import APIRouter
from app.services.r2_storage import r2_available
from app.config import settings

router = APIRouter()


@router.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "nexuspay-unified-api",
        "version": "4.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "r2_configured": r2_available(),
        "ai_providers": {
            "anthropic": bool(settings.ANTHROPIC_API_KEY),
            "openai": bool(settings.OPENAI_API_KEY),
            "google": bool(settings.GOOGLE_API_KEY),
            "grok": bool(settings.GROK_API_KEY),
        },
    }
