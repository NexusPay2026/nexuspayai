"""
Email service - transactional email via Resend (server-side only).

The RESEND_API_KEY is read from Render env vars via app.config.settings and is
never exposed to the frontend. Sending failures are logged and surfaced to the
caller (return False / raise) rather than swallowed silently, mirroring the
ai_providers error-handling discipline.
"""

import logging
import httpx

from app.config import settings

logger = logging.getLogger("nexuspay.email")

RESEND_ENDPOINT = "https://api.resend.com/emails"


async def send_email(to: str, subject: str, html: str) -> bool:
    """Send one transactional email via Resend.

    Returns True on success, False on failure. Never raises into the request
    path - the caller decides how to handle a delivery failure (e.g. still
    create the account but log that the verification email did not send).
    """
    if not settings.RESEND_API_KEY:
        logger.error("RESEND_API_KEY not set - cannot send email to %s", to)
        return False

    payload = {
        "from": settings.RESEND_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    headers = {
        "Authorization": f"Bearer {settings.RESEND_API_KEY}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
        if resp.status_code in (200, 201):
            logger.info("Email sent to %s (subject=%r)", to, subject)
            return True
        logger.error(
            "Resend send failed (%s) to %s: %s",
            resp.status_code, to, resp.text[:300],
        )
        return False
    except Exception as e:
        logger.error("Resend send exception to %s: %s", to, str(e))
        return False