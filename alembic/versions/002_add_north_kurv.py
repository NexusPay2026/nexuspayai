"""Add North and Kurv/EMS columns to quotes table

Revision ID: 002_add_north_kurv
Revises: 001_initial
Create Date: 2026-04-09
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "002_add_north_kurv"
down_revision: Union[str, None] = "001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("quotes", sa.Column("north_residual", sa.Float(), server_default="0", nullable=True))
    op.add_column("quotes", sa.Column("north_margin", sa.Float(), server_default="0", nullable=True))
    op.add_column("quotes", sa.Column("kurv_residual", sa.Float(), server_default="0", nullable=True))
    op.add_column("quotes", sa.Column("kurv_margin", sa.Float(), server_default="0", nullable=True))


def downgrade() -> None:
    op.drop_column("quotes", "kurv_margin")
    op.drop_column("quotes", "kurv_residual")
    op.drop_column("quotes", "north_margin")
    op.drop_column("quotes", "north_residual")
