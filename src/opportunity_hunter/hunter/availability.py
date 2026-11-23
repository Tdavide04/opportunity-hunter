"""Availability maintenance: Listings not observed for N days become `not_seen` (never deleted).

Absence from search results does not prove removal, so this is an honest state, not a deletion.
A Listing seen again goes back to `active` in the upsert.
"""
import logging
from datetime import datetime, timedelta

from opportunity_hunter.hunter.notifications import read_settings
from opportunity_hunter.ports import UnitOfWorkFactory

logger = logging.getLogger(__name__)

MAINTENANCE_INTERVAL = timedelta(hours=1)
CHECKED_AT = "availability_checked_at"


def maintain(unit_of_work: UnitOfWorkFactory, now: datetime) -> int | None:
    """Mark stale Listings as not seen, at most once per MAINTENANCE_INTERVAL. Returns how many, or None if skipped."""
    with unit_of_work() as uow:
        settings = read_settings(uow.settings)
        checked = uow.settings.get(CHECKED_AT)
        if checked is not None and now - datetime.fromisoformat(checked) < MAINTENANCE_INTERVAL:
            return None
        cutoff = now - timedelta(days=settings["not_seen_after_days"])
        marked = uow.listings.mark_not_seen(cutoff)
        uow.settings.put(CHECKED_AT, now.isoformat())
        uow.commit()
    if marked:
        logger.info("listings marked not seen", extra={"event": "availability_not_seen", "count": marked})
    return marked
