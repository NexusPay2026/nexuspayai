"""
Database layer — async PostgreSQL only. No SQLite fallback in production.
"""

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
import databases

from app.config import settings

# ── Async engine (for ORM / table creation) ──────────────────
engine = create_async_engine(
    settings.async_database_url,
    echo=(settings.APP_ENV == "development"),
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

# ── databases library (for raw queries in visitors/webhook) ──
database = databases.Database(settings.async_database_url)


async def get_db() -> AsyncSession:
    """Dependency: yields an async SQLAlchemy session."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()
