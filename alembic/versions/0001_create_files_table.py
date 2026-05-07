"""create files table

Revision ID: 0001
Revises:
Create Date: 2026-05-07

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "files",
        sa.Column("id", sa.String(length=32), primary_key=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("ip_uploaded_from", sa.String(length=45), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Text(), nullable=False),
        sa.Column("deleted_at", sa.Text(), nullable=True),
        sa.Column("one_shot", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_fetched_at", sa.Text(), nullable=True),
        sa.Column("delete_token_hash", sa.String(length=64), nullable=False),
    )
    op.create_index(
        "ix_files_expires_active",
        "files",
        ["expires_at", "deleted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_files_expires_active", table_name="files")
    op.drop_table("files")
