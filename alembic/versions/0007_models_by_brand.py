"""Store selected models under their make.

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_filters", sa.Column(
        "models_by_brand", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))


def downgrade():
    op.drop_column("user_filters", "models_by_brand")
