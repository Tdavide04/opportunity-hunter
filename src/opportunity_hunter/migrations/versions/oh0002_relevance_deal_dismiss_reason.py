"""Score v4: Relevance and Deal on Match, the reason of a dismissal on Opportunity.

Only adds nullable columns and their checks: existing rows stay valid (Matches scored before v4 have no
Relevance or Deal, older dismissals have no reason). Re-score them with ``opportunity-hunter rescore``.

Revision ID: oh0002
Revises: oh0001
Create Date: 2026-11-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "oh0002"
down_revision: str | None = "oh0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql(
        """
        ALTER TABLE match
            ADD COLUMN relevance smallint,
            ADD COLUMN deal smallint,
            ADD CONSTRAINT ck_match_relevance_range CHECK (relevance BETWEEN 0 AND 100),
            ADD CONSTRAINT ck_match_deal_range CHECK (deal BETWEEN 0 AND 100);
        ALTER TABLE opportunity
            ADD COLUMN dismiss_reason text,
            ADD CONSTRAINT ck_opportunity_dismiss_reason
                CHECK (dismiss_reason IN ('other_item', 'accessory', 'region', 'too_expensive', 'condition')),
            ADD CONSTRAINT ck_opportunity_dismiss_reason_only_when_dismissed
                CHECK (dismiss_reason IS NULL OR status = 'dismissed');
        """
    )


def downgrade() -> None:
    raise RuntimeError("oh0002 has no downgrade: it would drop the user's dismiss reasons")
