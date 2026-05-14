"""Initial schema — users, merchants, visitors, audit_jobs

Revision ID: 001_initial
Revises: None
Create Date: 2026-03-26
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Users ────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("email", sa.String(320), unique=True, nullable=False, index=True),
        sa.Column("password_hash", sa.String(256), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("company", sa.String(300), server_default=""),
        sa.Column("role", sa.String(20), server_default="user"),
        sa.Column("tier", sa.String(20), server_default="free"),
        sa.Column("veteran", sa.Boolean(), server_default="false"),
        sa.Column("active", sa.Boolean(), server_default="true"),
        sa.Column("verified", sa.Boolean(), server_default="false"),
        sa.Column("must_change_password", sa.Boolean(), server_default="false"),
        sa.Column("assigned_merchants", sa.JSON(), server_default="[]"),
        sa.Column("created_by", sa.String(320), server_default="self"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_login", sa.DateTime(timezone=True), nullable=True),
    )

    # ── Merchants ────────────────────────────────────────────
    op.create_table(
        "merchants",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("owner_id", sa.String(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("owner_email", sa.String(320), server_default=""),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("processor", sa.String(200), server_default=""),
        sa.Column("statement_month", sa.String(20), server_default=""),
        sa.Column("monthly_volume", sa.Float(), server_default="0"),
        sa.Column("total_fees", sa.Float(), server_default="0"),
        sa.Column("interchange_cost", sa.Float(), server_default="0"),
        sa.Column("processor_markup", sa.Float(), server_default="0"),
        sa.Column("monthly_fees", sa.Float(), server_default="0"),
        sa.Column("transaction_count", sa.Integer(), server_default="0"),
        sa.Column("credit_card_pct", sa.Float(), server_default="85"),
        sa.Column("avg_ticket", sa.Float(), server_default="0"),
        sa.Column("effective_rate", sa.Float(), server_default="0"),
        sa.Column("interchange_rate", sa.Float(), server_default="0"),
        sa.Column("markup_rate", sa.Float(), server_default="0"),
        sa.Column("risk_score", sa.Integer(), server_default="0"),
        sa.Column("line_items", sa.JSON(), server_default="[]"),
        sa.Column("findings", sa.JSON(), server_default="[]"),
        sa.Column("is_demo", sa.Boolean(), server_default="false"),
        sa.Column("added_by", sa.String(320), server_default=""),
        sa.Column("statement_r2_key", sa.String(500), nullable=True),
        sa.Column("report_r2_key", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_merchants_owner", "merchants", ["owner_email"])

    # ── Visitors / Leads ─────────────────────────────────────
    op.create_table(
        "visitors",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("full_name", sa.String(200), nullable=False),
        sa.Column("business_name", sa.String(300), nullable=False),
        sa.Column("email", sa.String(320), nullable=False, index=True),
        sa.Column("phone", sa.String(30), nullable=True),
        sa.Column("source", sa.String(50), server_default="landing_page"),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("referrer", sa.Text(), nullable=True),
        sa.Column("utm_source", sa.String(200), nullable=True),
        sa.Column("utm_medium", sa.String(200), nullable=True),
        sa.Column("utm_campaign", sa.String(200), nullable=True),
        sa.Column("utm_term", sa.String(200), nullable=True),
        sa.Column("utm_content", sa.String(200), nullable=True),
        sa.Column("page_url", sa.Text(), nullable=True),
        sa.Column("session_duration_ms", sa.Integer(), nullable=True),
        sa.Column("ai_business_type", sa.String(200), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_visitors_created", "visitors", [sa.text("created_at DESC")])

    # ── Audit Jobs ───────────────────────────────────────────
    op.create_table(
        "audit_jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("merchant_id", sa.String(), sa.ForeignKey("merchants.id"), nullable=True),
        sa.Column("user_id", sa.String(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.String(30), server_default="pending"),
        sa.Column("provider_results", sa.JSON(), server_default="{}"),
        sa.Column("consensus_data", sa.JSON(), server_default="{}"),
        sa.Column("confidence", sa.String(20), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("statement_r2_key", sa.String(500), nullable=True),
        sa.Column("report_r2_key", sa.String(500), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_audit_jobs_user", "audit_jobs", ["user_id"])


def downgrade() -> None:
    op.drop_table("audit_jobs")
    op.drop_table("visitors")
    op.drop_table("merchants")
    op.drop_table("users")
