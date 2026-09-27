"""Add per-user geography, source and search controls.

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_filters", sa.Column(
        "cities_by_region", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")))
    op.add_column("user_filters", sa.Column(
        "selected_sources", sa.JSON(), nullable=False,
        server_default=sa.text("'[\"telegram\",\"avito\",\"autoru\",\"drom\"]'::json")))
    op.add_column("user_filters", sa.Column(
        "search_enabled", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    op.drop_column("user_filters", "search_enabled")
    op.drop_column("user_filters", "selected_sources")
    op.drop_column("user_filters", "cities_by_region")
