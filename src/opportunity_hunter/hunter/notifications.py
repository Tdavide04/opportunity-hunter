"""Notification outbox (ADR-0003): rows are written with their Opportunity, delivered after commit."""
import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from opportunity_hunter.channels.base import ChannelError, NotificationChannel, OutgoingMessage
from opportunity_hunter.db.models import Listing, Notification, Opportunity
from opportunity_hunter.ports import PriceHistoryRepository, SettingsStore, UnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)

NEW_OPPORTUNITY = "new_opportunity"
PRICE_DROP = "price_drop"
SETTING_DEFAULTS: dict[str, Any] = {
    "notifications_paused": False,
    "dry_run": False,
    "price_drop_min_percent": 10,
    "not_seen_after_days": 7,
    "circuit_failures": 3,
    "circuit_suspend_minutes": 60,
    "shadow_marketplaces": [],
}
PRICE_STATUS_TEXT = {"free": "Gratis", "on_request": "Su richiesta", "missing": "Prezzo mancante"}


def read_settings(settings: SettingsStore) -> dict[str, Any]:
    return {**SETTING_DEFAULTS, **settings.get_many(SETTING_DEFAULTS)}


def enqueue(
    uow: UnitOfWork,
    opportunities: Sequence[Opportunity],
    channels: Sequence[NotificationChannel],
    suppressed_reason: str | None,
    now: datetime,
    price_drops: Sequence[tuple[Opportunity, str]] = (),
) -> None:
    """One Notification per (Opportunity, Channel, reason); suppressed ones are recorded, never sent.

    New Opportunities cannot collide. A Price Drop to an amount already notified (20 -> 15 -> 20 -> 15)
    hits the unique constraint, so it is inserted with ON CONFLICT DO NOTHING instead of failing the page.
    """
    status = "suppressed" if suppressed_reason else "pending"
    for opportunity in opportunities:
        for channel in channels:
            uow.outbox.add(opportunity, channel.name, NEW_OPPORTUNITY, status, suppressed_reason, now)
    rows = [
        {"opportunity_id": opportunity.id, "channel": channel.name, "reason": reason, "status": status,
         "suppressed_reason": suppressed_reason, "created_at": now, "attempts": 0}
        for opportunity, reason in price_drops for channel in channels
    ]
    if rows:
        uow.outbox.add_ignoring_duplicates(rows)


def dispatch(unit_of_work: UnitOfWorkFactory, channels: Sequence[NotificationChannel], max_attempts: int, now: datetime) -> dict[str, int]:
    """Send pending Notifications and retry failed ones below max_attempts. Each in its own short transaction.

    If notifications are paused when this runs, waiting ones become suppressed instead of piling up.
    A crash between a successful send and its commit can repeat that one message: no Channel offers idempotency keys.
    """
    by_name = {c.name: c for c in channels}
    result = {"sent": 0, "failed": 0}
    if not by_name:
        return result
    with unit_of_work() as uow:
        settings = read_settings(uow.settings)
        paused = settings["notifications_paused"]
        shadow = set(settings["shadow_marketplaces"])
        ids = uow.outbox.deliverable_ids(by_name, max_attempts)
    for notification_id in ids:
        with unit_of_work() as uow:
            outcome = _deliver(uow, notification_id, by_name, paused, shadow, now)
            uow.commit()
        if outcome is not None:
            result[outcome] += 1
    return result


def _deliver(
    uow: UnitOfWork, notification_id: int, by_name: dict[str, NotificationChannel], paused: bool, shadow: set[str], now: datetime
) -> str | None:
    """Deliver or suppress one Notification inside the caller's Unit of Work: 'sent', 'failed' or None (nothing sent)."""
    row = uow.outbox.claim(notification_id)
    if row is None or row.status not in ("pending", "failed"):
        return None
    if paused:
        row.status, row.suppressed_reason = "suppressed", "paused"
        return None
    if row.opportunity.match.listing.marketplace in shadow:
        # Shadow Mode turned on after the Notification was written: the Legacy Scanner still notifies
        row.status, row.suppressed_reason = "suppressed", "shadow_mode"
        return None
    row.attempts += 1
    try:
        by_name[row.channel].send(to_message(row.opportunity, price_drop_note(uow.price_history, row)))
    except ChannelError as exc:
        row.status, row.last_error = "failed", str(exc)
        logger.warning(
            "notification failed",
            extra={
                "event": "notification_failed",
                "notification_id": row.id,
                "channel": row.channel,
                "attempt": row.attempts,
                "error": str(exc),
            },
        )
        return "failed"
    row.status, row.last_error, row.sent_at = "sent", None, now
    return "sent"


def price_drop_note(history: PriceHistoryRepository, notification: Notification) -> str | None:
    """'Prezzo sceso da X a Y': Y comes from the reason, X is the observation before Y in the price history."""
    if not notification.reason.startswith(f"{PRICE_DROP}:"):
        return None
    amount = Decimal(notification.reason.split(":", 1)[1])
    listing = notification.opportunity.match.listing
    observations = history.amounts(listing.id)
    at = max((i for i, observed in enumerate(observations) if observed == amount), default=None)
    previous_amount = next((observed for observed in reversed(observations[:at]) if observed is not None), None) if at else None
    new = _money(amount, listing.currency)
    if previous_amount is not None:
        return f"Prezzo sceso da {_money(previous_amount, None)} a {new}"
    return f"Prezzo sceso a {new}"


def _money(amount: Decimal, currency: str | None) -> str:
    text = f"{amount:.2f}".replace(".", ",")
    return f"{text} {currency}" if currency else text


def to_message(opportunity: Opportunity, note: str | None = None) -> OutgoingMessage:
    listing: Listing = opportunity.match.listing
    score = opportunity.match.score
    url = listing.url
    if score is None or url is None:
        raise ValueError("an Opportunity must reference a scored match and a listing URL")
    return OutgoingMessage(
        title=listing.title or listing.external_id,
        price=format_price(listing),
        location=format_location(listing.location),
        score=score,
        url=url,
        image_url=listing.images[0] if listing.images else None,
        search_profile=opportunity.match.search_profile.name,
        note=note,
    )


def format_price(listing: Listing) -> str:
    if listing.price_status == "priced" and listing.price_amount is not None:
        return f"{listing.price_amount:.2f}".replace(".", ",") + f" {listing.currency}"
    return PRICE_STATUS_TEXT.get(listing.price_status or "missing", "Prezzo mancante")


def format_location(location: dict[str, Any] | None) -> str | None:
    if not location:
        return None
    return location.get("city") or location.get("province") or location.get("region") or location.get("raw") or location.get("country")
