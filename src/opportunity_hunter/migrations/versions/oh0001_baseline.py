"""Baseline: the nine Hunter tables exactly as Personal Collection Hub left them at its revision 0010.

The DDL in ``oh0001_baseline.sql`` is ``pg_dump --schema-only`` of those tables, frozen: never
edit it, add a new revision instead. An existing database that already has these tables is
adopted (verified, then stamped) with ``opportunity_hunter.migrations.adopt_existing``, never
upgraded through this revision.

Revision ID: oh0001
Revises:
Create Date: 2026-11-23
"""

from collections.abc import Sequence
from pathlib import Path

from alembic import op

revision: str = "oh0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(Path(__file__).with_suffix(".sql").read_text())


def downgrade() -> None:
    raise RuntimeError("the baseline has no downgrade: it would drop every Hunter table and its data")
