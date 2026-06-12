"""Ensure audit_jobs.provider_results JSON column exists

001_initial already creates this column on fresh databases; this migration
backfills it on databases whose audit_jobs table predates it (e.g. stamped
or hand-created schemas). The column stores the per-provider extraction
snapshots returned by /api/audit/run.

Revision ID: 004_provider_results
Revises: 003_tier1_statements
Create Date: 2026-06-12
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "004_provider_results"
down_revision: Union[str, None] = "003_tier1_statements"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    insp = sa.inspect(op.get_bind())
    return any(c["name"] == "provider_results" for c in insp.get_columns("audit_jobs"))


def upgrade() -> None:
    if not _has_column():
        op.add_column("audit_jobs", sa.Column("provider_results", sa.JSON(), nullable=True))


def downgrade() -> None:
    # No-op: on databases created via 001_initial the column predates this
    # migration, so dropping it here would destroy pre-existing data.
    pass
