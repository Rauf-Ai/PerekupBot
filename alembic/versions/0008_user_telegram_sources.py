"""Allow users to subscribe to their own Telegram sources.

Revision ID: 0008
Revises: 0007
"""
from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "user_telegram_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_id", sa.Integer(), sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "source_id"),
    )
    op.create_index("ix_user_telegram_sources_user_id", "user_telegram_sources", ["user_id"])
    op.create_index("ix_user_telegram_sources_source_id", "user_telegram_sources", ["source_id"])


def downgrade():
    op.drop_index("ix_user_telegram_sources_source_id", table_name="user_telegram_sources")
    op.drop_index("ix_user_telegram_sources_user_id", table_name="user_telegram_sources")
    op.drop_table("user_telegram_sources")
