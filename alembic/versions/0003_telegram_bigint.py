"""Use a 64-bit Telegram user ID.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("users", "telegram_id", existing_type=sa.Integer(), type_=sa.BigInteger())


def downgrade():
    op.alter_column("users", "telegram_id", existing_type=sa.BigInteger(), type_=sa.Integer())
