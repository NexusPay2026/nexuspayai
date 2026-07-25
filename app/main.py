"""
NexusPay Intelligence Platform â€” Unified Backend API
=====================================================
Consolidates: Portal API + Visitor Webhook + AI Orchestration + R2 Storage
Surfaces served:
  1. nexuspayservices.com         â€” Main website
  2. freeanalysis.nexuspayservices.com â€” Landing page (lead capture)
  3. nexuspayai.com               â€” Portal / Bloomberg terminal UI
  4. nexuspaydashboard.netlify.app â€” Visitor tracking dashboard
"""

import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import settings
from app.database import engine, Base, database
from app.routers import auth, merchants, users, visitors, audit, health, storage, quotes, pricing_tool, chatbot, statements, exclusions

logger = logging.getLogger("nexuspay.api")


class CorsSafe500Middleware:
    """Turn an unhandled exception into a JSON 500 that still carries CORS headers.

    Starlette's ServerErrorMiddleware wraps *everything* (it sits OUTSIDE the user
    middleware, including CORSMiddleware) and emits its 500 on the raw ASGI send. So an
    unhandled exception in a route returns a 500 with NO Access-Control-Allow-Origin
    header, and the browser reports a misleading "No 'Access-Control-Allow-Origin'
    header is present" CORS error that masks the real exception and status code.

    This middleware is added BEFORE CORSMiddleware (add_middleware inserts at index 0,
    so the LAST-added middleware is outermost -- see Starlette build_middleware_stack).
    CORSMiddleware therefore WRAPS this one: the JSON 500 returned here flows back OUT
    through CORS and gets the Access-Control-Allow-Origin header. HTTPExceptions are
    handled deeper by ExceptionMiddleware and never reach here. The response_started
    guard mirrors ServerErrorMiddleware so a mid-stream crash bubbles up instead of
    trying to send a second response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = False

        async def _send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, _send)
        except Exception as exc:
            logger.exception(
                "Unhandled error on %s %s", scope.get("method"), scope.get("path")
            )
            if response_started:
                # Response is already on the wire; can't replace it. Let it bubble to
                # ServerErrorMiddleware (which handles the response_started case too).
                raise
            response = JSONResponse(
                status_code=500,
                content={
                    "error": "internal_server_error",
                    "type": type(exc).__name__,
                    "detail": str(exc),
                    "path": scope.get("path"),
                    "method": scope.get("method"),
                },
            )
            # `send` here is CORSMiddleware's wrapped send (CORS is outside us), so this
            # response gets Access-Control-Allow-Origin on its way out.
            await response(scope, receive, send)

# â”€â”€ Lifespan â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create all tables on startup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await database.connect()
    yield
    await database.disconnect()

# â”€â”€ App â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app = FastAPI(
    title="NexusPay Intelligence API",
    version="4.0.0",
    description="Unified backend for website, landing page, portal, and visitor dashboard",
    lifespan=lifespan,
)

# â”€â”€ CORS â€” allow all four frontend surfaces â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
ALLOWED_ORIGINS = [
    # 1. Main website
    "https://nexuspayservices.com",
    "https://www.nexuspayservices.com",
    # 2. Landing page
    "https://freeanalysis.nexuspayservices.com",
    "https://nexuspaylandingpage.netlify.app",
    # 3. Portal / Bloomberg terminal UI
    "https://nexuspayai.com",
    "https://www.nexuspayai.com",
    # 4. Visitor tracking dashboard (explicit Netlify production site)
    "https://nexuspaydashboard.netlify.app",
    # 5. Pricing Tool
    "https://paycalculator.nexuspayai.com",
    "https://matrix.nexuspayai.com",
    "https://dashboard.nexuspayservices.com",
    # 6. Calcerta Group / Interstellar I.S. customer-facing site
    "https://calcerta.com",
    "https://www.calcerta.com",
    "https://isinterstellar.com",
    "https://www.isinterstellar.com",
    # Dev
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:8080",
]
# NOTE: the Netlify production sites are listed explicitly above
# (nexuspaylandingpage.netlify.app, nexuspaydashboard.netlify.app). The previous
# wildcard `allow_origin_regex=r"https://.*\.netlify\.app"` was removed — paired
# with credentials it let ANY *.netlify.app site call the authenticated API. Add
# new production Netlify domains to ALLOWED_ORIGINS explicitly; deploy-preview URLs
# are intentionally not allowed.

# Credentials may only be sent with an explicit origin allowlist. In development
# we allow all origins for convenience, so credentials MUST be disabled there
# (browsers reject "*" + credentials, and it would be unsafe regardless).
ALLOW_CREDENTIALS = True
if settings.APP_ENV == "development":
    ALLOWED_ORIGINS = ["*"]
    ALLOW_CREDENTIALS = False

# Unhandled-error CORS safety net. MUST be added BEFORE CORSMiddleware so CORS wraps it
# (add_middleware makes the LAST-added middleware outermost). This lets 500s carry CORS
# headers, so a failing POST shows the real error/status in the browser instead of a
# masked "No Access-Control-Allow-Origin" CORS error. Verified against Starlette 0.52.1.
# ORDERING IS LOAD-BEARING: keep this the FIRST add_middleware call (innermost user
# middleware, directly inside CORS). Any NEW middleware must be added BEFORE this line so
# it stays inside the catch; a middleware added after it would sit outside and its 500s
# would lose CORS headers again.
app.add_middleware(CorsSafe500Middleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=ALLOW_CREDENTIALS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# â”€â”€ Routers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
app.include_router(health.router,    tags=["Health"])
app.include_router(auth.router,      prefix="/api",  tags=["Auth"])
app.include_router(merchants.router, prefix="/api",  tags=["Merchants"])
app.include_router(users.router,     prefix="/api",  tags=["Users"])
app.include_router(visitors.router,  tags=["Visitors / Leads"])
app.include_router(audit.router,     prefix="/api",  tags=["AI Audit"])
app.include_router(statements.router, prefix="/api",  tags=["Statements"])
app.include_router(exclusions.router, prefix="/api",  tags=["Exclusions"])
app.include_router(storage.router,   prefix="/api",  tags=["Storage / R2"])
app.include_router(quotes.router,    prefix="/api",  tags=["Pricing Quotes"])
app.include_router(pricing_tool.router, tags=["Pricing Tool"])
app.include_router(chatbot.router,   prefix="/api",  tags=["Chatbot"])


