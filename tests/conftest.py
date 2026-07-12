"""
Shared pytest fixtures for the NexusPay backend test suite.

Test-database strategy
----------------------
The ORM models are portable (String UUID PKs, JSON, Float/Bool/DateTime — no
Postgres-only types), so by default the suite runs against **aiosqlite
in-memory**: zero infrastructure, fast, runnable anywhere. CI additionally runs
the *same* suite against the Postgres service container by setting
``TEST_DATABASE_URL`` (see .github/workflows/ci.yml), so any Postgres-specific
regression is still caught.

Safety
------
* We NEVER touch the real ``DATABASE_URL``. This module overwrites it (before
  importing the app) with a throwaway sqlite placeholder used only so the
  import-time engine in ``app.database`` constructs. That engine is never
  connected — every request's session is redirected to a per-test engine via a
  ``get_db`` dependency override.
* ``_assert_test_db`` refuses loudly to run against anything that is not an
  obvious test database (non-sqlite URL without "test" in it, or one that looks
  like production).
* All external calls are mocked: email (Resend) is patched to a no-op; the AI
  providers and R2 are never reached by the tested endpoints. No network.
"""

import os
import pathlib
import tempfile

# ── Environment MUST be set before importing anything under app.* ───────────
# app.config reads env at import and app.database builds an engine at import.
os.environ["APP_ENV"] = "test"
os.environ["JWT_SECRET"] = "test-secret-nexuspay-ci-only"
# Deterministic, non-random seed so _seed_defaults() is quiet and predictable.
os.environ.setdefault("ADMIN_SEED_PASSWORD", "seed-admin-pw-test")
# Keep the in-memory login limiter generous so unrelated tests don't trip it;
# the lockout test drives the per-account path explicitly.
os.environ.setdefault("LOGIN_RATE_PER_IP_PER_MIN", "1000")

# The import-time engine in app.database is created with pool_size/max_overflow,
# which the sqlite StaticPool used for ":memory:" rejects — so the *placeholder*
# app-engine URL must be a FILE sqlite URL. It is never connected (get_db is
# overridden, lifespan is not run), so no file is ever created.
_APP_ENGINE_PLACEHOLDER = (
    pathlib.Path(tempfile.gettempdir()) / "nexuspay_test_appengine_placeholder.db"
)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_APP_ENGINE_PLACEHOLDER.as_posix()}"

# The database the tests actually read/write. Default: in-memory sqlite.
# CI sets TEST_DATABASE_URL to the Postgres service container.
RUN_DB_URL = os.environ.get("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")


def _assert_test_db(url: str) -> None:
    """Fail loudly if pointed at anything that is not clearly a test database."""
    low = url.lower()
    if low.startswith("sqlite"):
        return  # in-memory / temp sqlite is inherently non-production
    if "test" not in low:
        raise RuntimeError(
            f"Refusing to run the test suite against a non-test database: {url!r}. "
            "The database name must contain 'test' (or use sqlite)."
        )
    for marker in ("onrender", "amazonaws", "nexuspay-db", "/nexuspay?", "/nexuspay'"):
        if marker in low:
            raise RuntimeError(
                f"Refusing: {url!r} looks like a production database."
            )


_assert_test_db(RUN_DB_URL)

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient, ASGITransport  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool, NullPool  # noqa: E402

# App imports (safe now that env is configured) ─────────────────────────────
from app.database import Base, get_db  # noqa: E402
from app import models  # noqa: E402  (registers all tables on Base.metadata)
from app.main import app  # noqa: E402
from app.services.auth_service import hash_password, create_token  # noqa: E402
from app.routers import auth as auth_router  # noqa: E402

_IS_SQLITE = RUN_DB_URL.startswith("sqlite")
# Public alias for tests that must skip on sqlite. SQLite has no real timezone
# support, so DateTime(timezone=True) columns read back NAIVE; any app code that
# compares them against a tz-aware datetime (e.g. token expiry checks in
# auth.py) only works on Postgres. Those tests are gated with skipif(IS_SQLITE)
# and run against the Postgres service container in CI.
IS_SQLITE = _IS_SQLITE


def _make_engine():
    if _IS_SQLITE:
        # StaticPool keeps a single shared connection so an in-memory DB persists
        # across sessions within one test.
        return create_async_engine(
            RUN_DB_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    # Postgres (CI): a fresh engine per test, NullPool so no connection outlives
    # the test's event loop.
    return create_async_engine(RUN_DB_URL, poolclass=NullPool)


@pytest_asyncio.fixture
async def db_engine():
    """A fresh database + schema for every test (full isolation)."""
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_factory):
    """A session for the test itself to seed rows and make assertions."""
    async with session_factory() as s:
        yield s


@pytest_asyncio.fixture
async def client(session_factory):
    """httpx AsyncClient bound to the real ASGI app, with get_db redirected to
    the per-test database. Lifespan is intentionally not run (no production
    engine connect, no external services)."""

    async def _override_get_db():
        async with session_factory() as s:
            yield s

    app.dependency_overrides[get_db] = _override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_process_state(monkeypatch):
    """Reset the auth module's in-memory limiter/lockout/seed globals between
    tests (they persist per-process) and mock all outbound email."""
    auth_router._login_ip_buckets.clear()
    auth_router._login_fail_counts.clear()
    auth_router._login_lockouts.clear()
    auth_router._seeded = False

    async def _fake_send_email(*_args, **_kwargs):
        return True

    # auth.py did `from app.services.email import send_email`, so patch the name
    # bound in the auth router module.
    monkeypatch.setattr(auth_router, "send_email", _fake_send_email)
    yield


# ── Factory helpers (call with the `db` fixture session) ────────────────────
async def make_user(
    db,
    *,
    email,
    password="Password123!",
    role="user",
    display_name="Test User",
    verified=True,
    active=True,
    company="TestCo",
):
    user = models.User(
        email=email.strip().lower(),
        password_hash=hash_password(password),
        display_name=display_name,
        company=company,
        role=role,
        tier="free",
        active=active,
        verified=verified,
        created_by="test",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def make_exclusion(db, *, email="", name="", reason="test"):
    excl = models.Exclusion(
        email=email.strip().lower(), name=name.strip(), reason=reason, added_by="test"
    )
    db.add(excl)
    await db.commit()
    return excl


async def make_token_row(db, *, user_id, token_hash, purpose, expires_at, used=False):
    row = models.EmailVerificationToken(
        user_id=user_id,
        token_hash=token_hash,
        purpose=purpose,
        expires_at=expires_at,
        used=used,
    )
    db.add(row)
    await db.commit()
    return row


def token_for(user):
    """Mint a valid JWT for a user (or a role-only synthetic identity)."""
    return create_token(user.id, user.email, user.role)


async def count_rows(db, model):
    result = await db.execute(select(model))
    return len(result.scalars().all())
