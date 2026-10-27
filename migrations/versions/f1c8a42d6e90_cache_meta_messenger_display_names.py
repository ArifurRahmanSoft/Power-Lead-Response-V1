"""cache Meta Messenger Page and contact display names

Revision ID: f1c8a42d6e90
Revises: d4b7e91f2a63
Create Date: 2026-10-26 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "f1c8a42d6e90"
down_revision: str | Sequence[str] | None = "d4b7e91f2a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("t_whatsapp_channel", sa.Column("display_name", sa.String(200), nullable=True))
    op.add_column("t_whatsapp_channel", sa.Column("profile_refreshed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("t_whatsapp_channel", sa.Column("profile_retry_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("t_whatsapp_conversation", sa.Column("provider_display_name", sa.String(200), nullable=True))
    op.add_column("t_whatsapp_conversation", sa.Column("provider_profile_refreshed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("t_whatsapp_conversation", sa.Column("provider_profile_retry_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("t_whatsapp_conversation", sa.Column("provider_profile_error_code", sa.String(50), nullable=True))


def downgrade() -> None:
    op.drop_column("t_whatsapp_conversation", "provider_profile_error_code")
    op.drop_column("t_whatsapp_conversation", "provider_profile_retry_after")
    op.drop_column("t_whatsapp_conversation", "provider_profile_refreshed_at")
    op.drop_column("t_whatsapp_conversation", "provider_display_name")
    op.drop_column("t_whatsapp_channel", "profile_retry_after")
    op.drop_column("t_whatsapp_channel", "profile_refreshed_at")
    op.drop_column("t_whatsapp_channel", "display_name")
