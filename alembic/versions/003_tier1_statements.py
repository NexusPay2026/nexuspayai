"""Tier 1: statement integrity fields, audit-job fields, append-only audit_log

Revision ID: 003_tier1_statements
Revises: 002_add_north_kurv
Create Date: 2026-05-22
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "003_tier1_statements"
down_revision: Union[str, None] = "002_add_north_kurv"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- merchants: integrity / grouping / compliance --
    op.add_column("merchants", sa.Column("statement_date", sa.String(length=20), server_default="", nullable=True))
    op.add_column("merchants", sa.Column("file_hash", sa.String(length=64), nullable=True))
    op.add_column("merchants", sa.Column("page_count", sa.Integer(), server_default="0", nullable=True))
    op.add_column("merchants", sa.Column("merchant_number", sa.String(length=60), nullable=True))
    op.add_column("merchants", sa.Column("revision_group", sa.String(), nullable=True))
    op.add_column("merchants", sa.Column("is_revision", sa.Boolean(), server_default=sa.false(), nullable=True))
    op.add_column("merchants", sa.Column("agree_pct", sa.Integer(), server_default="0", nullable=True))
    op.add_column("merchants", sa.Column("provider_count", sa.Integer(), server_default="0", nullable=True))
    op.add_column("merchants", sa.Column("providers", sa.JSON(), nullable=True))
    op.add_column("merchants", sa.Column("files", sa.JSON(), nullable=True))
    op.add_column("merchants", sa.Column("uploaded_via", sa.String(length=40), server_default="command_center", nullable=True))
    op.add_column("merchants", sa.Column("consent_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_merchants_file_hash", "merchants", ["file_hash"])
    op.create_index("ix_merchants_merchant_number", "merchants", ["merchant_number"])
    op.create_index("ix_merchants_revision_group", "merchants", ["revision_group"])

    # -- audit_jobs: integrity fields --
    op.add_column("audit_jobs", sa.Column("file_hash", sa.String(length=64), nullable=True))
    op.add_column("audit_jobs", sa.Column("page_count", sa.Integer(), server_default="0", nullable=True))
    op.add_column("audit_jobs", sa.Column("agree_pct", sa.Integer(), server_default="0", nullable=True))
    op.add_column("audit_jobs", sa.Column("files", sa.JSON(), nullable=True))
    op.add_column("audit_jobs", sa.Column("cache_hit", sa.Boolean(), server_default=sa.false(), nullable=True))
    op.create_index("ix_audit_jobs_file_hash", "audit_jobs", ["file_hash"])

    # -- audit_log: append-only chain of custody --
    op.create_table(
        "audit_log",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("actor_email", sa.String(length=320), server_default="", nullable=True),
        sa.Column("actor_role", sa.String(length=20), server_default="", nullable=True),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("entity_type", sa.String(length=40), server_default="", nullable=True),
        sa.Column("entity_id", sa.String(), nullable=True),
        sa.Column("file_hash", sa.String(length=64), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    op.create_index("ix_audit_log_ts", "audit_log", ["ts"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])
    op.create_index("ix_audit_log_file_hash", "audit_log", ["file_hash"])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_index("ix_audit_jobs_file_hash", table_name="audit_jobs")
    for c in ("cache_hit", "files", "agree_pct", "page_count", "file_hash"):
        op.drop_column("audit_jobs", c)
    op.drop_index("ix_merchants_revision_group", table_name="merchants")
    op.drop_index("ix_merchants_merchant_number", table_name="merchants")
    op.drop_index("ix_merchants_file_hash", table_name="merchants")
    for c in ("consent_at", "uploaded_via", "files", "providers", "provider_count",
              "agree_pct", "is_revision", "revision_group", "merchant_number",
              "page_count", "file_hash", "statement_date"):
        op.drop_column("merchants", c)