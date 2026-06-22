"""Create email_verification_tokens table

Stores single-use, expiring tokens for email verification (and reusable for
password reset). Only a HASH of the token is stored, never the raw token, so a
database leak does not expose usable links.

Revision ID: 005_email_verification_tokens
Revises: 004_provider_results
Create Date: 2026-06-22
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "005_email_verification_tokens"
down_revision: Union[str, None] = "004_provider_results"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    insp = sa.inspect(op.get_bind())
    return name in insp.get_table_names()


def upgrade() -> None:
    if not _has_table("email_verification_tokens"):
        op.create_table(
            "email_verification_tokens",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("user_id", sa.String(), nullable=False, index=True),
            sa.Column("token_hash", sa.String(), nullable=False, index=True),
            sa.Column("purpose", sa.String(length=20), nullable=False, server_default="verify"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("used", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    if _has_table("email_verification_tokens"):
        op.drop_table("email_verification_tokens")