"""Availability maintenance: Listings not observed for N days become `not_seen` (never deleted).

Absence from search results does not prove removal, so this is an honest state, not a deletion.
A Listing seen again goes back to `active` in the upsert.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from opportunity_hunter.db.models import AppSetting, Listing
from opportunity_hunter.hunter.notifications import read_settings

logger = logging.getLogger(__name__)

MAINTENANCE_INTERVAL = timedelta(hours=1)
CHECKED_AT = "availability_checked_at"


def maintain(session_factory: sessionmaker[Session], now: datetime) -> int | None:
    """Mark stale Listings as not seen, at most once per MAINTENANCE_INTERVAL. Returns how many, or None if skipped."""
    with session_factory() as session, session.begin():
        settings = read_settings(session)
        checked = session.get(AppSetting, CHECKED_AT)
        if checked is not None and now - datetime.fromisoformat(checked.value) < MAINTENANCE_INTERVAL:
            return None
        cutoff = now - timedelta(days=settings["not_seen_after_days"])
        result = session.execute(
            update(Listing)
            .where(Listing.availability == "active", Listing.origin == "acquired", Listing.last_seen_at < cutoff)
            .values(availability="not_seen")
        )
        rowcount = getattr(result, "rowcount", 0)
        marked = rowcount if isinstance(rowcount, int) else 0
        session.merge(AppSetting(key=CHECKED_AT, value=now.isoformat()))
    if marked:
        logger.info("listings marked not seen", extra={"event": "availability_not_seen", "count": marked})
    return marked
