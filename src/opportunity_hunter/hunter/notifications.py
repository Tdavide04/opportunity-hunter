"""Notification outbox (ADR-0003): rows are written with their Opportunity, delivered after commit."""
import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, joinedload, sessionmaker

from opportunity_hunter.channels.base import ChannelError, NotificationChannel, OutgoingMessage
from opportunity_hunter.db.models import AppSetting, Listing, ListingPriceHistory, Match, Notification, Opportunity

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


def read_settings(session: Session) -> dict[str, Any]:
    stored = {row.key: row.value for row in session.scalars(select(AppSetting).where(AppSetting.key.in_(SETTING_DEFAULTS)))}
    return {**SETTING_DEFAULTS, **stored}


def enqueue(
    session: Session,
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
            session.add(Notification(
                opportunity=opportunity, channel=channel.name, reason=NEW_OPPORTUNITY,
                status=status, suppressed_reason=suppressed_reason, created_at=now,
            ))
    rows = [
        {"opportunity_id": opportunity.id, "channel": channel.name, "reason": reason, "status": status,
         "suppressed_reason": suppressed_reason, "created_at": now, "attempts": 0}
        for opportunity, reason in price_drops for channel in channels
    ]
    if rows:
        session.execute(insert(Notification).values(rows).on_conflict_do_nothing(constraint="uq_notification_opportunity_channel_reason"))


def dispatch(session_factory: sessionmaker[Session], channels: Sequence[NotificationChannel], max_attempts: int, now: datetime) -> dict[str, int]:
    """Send pending Notifications and retry failed ones below max_attempts. Each in its own short transaction.

    If notifications are paused when this runs, waiting ones become suppressed instead of piling up.
    A crash between a successful send and its commit can repeat that one message: no Channel offers idempotency keys.
    """
    by_name = {c.name: c for c in channels}
    result = {"sent": 0, "failed": 0}
    if not by_name:
        return result
    with session_factory() as session:
        settings = read_settings(session)
        paused = settings["notifications_paused"]
        shadow = set(settings["shadow_marketplaces"])
        ids = list(session.scalars(
            select(Notification.id)
            .where(Notification.channel.in_(by_name))
            .where((Notification.status == "pending") | ((Notification.status == "failed") & (Notification.attempts < max_attempts)))
            .order_by(Notification.id)
        ))
    for notification_id in ids:
        with session_factory() as session, session.begin():
            row = session.scalars(
                select(Notification).where(Notification.id == notification_id).with_for_update(skip_locked=True, of=Notification)
                .options(joinedload(Notification.opportunity).joinedload(Opportunity.match).joinedload(Match.listing),
                         joinedload(Notification.opportunity).joinedload(Opportunity.match).joinedload(Match.search_profile))
            ).first()
            if row is None or row.status not in ("pending", "failed"):
                continue
            if paused:
                row.status, row.suppressed_reason = "suppressed", "paused"
                continue
            if row.opportunity.match.listing.marketplace in shadow:
                # Shadow Mode turned on after the Notification was written: the Legacy Scanner still notifies
                row.status, row.suppressed_reason = "suppressed", "shadow_mode"
                continue
            row.attempts += 1
            try:
                by_name[row.channel].send(to_message(row.opportunity, price_drop_note(session, row)))
            except ChannelError as exc:
                row.status, row.last_error = "failed", str(exc)
                result["failed"] += 1
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
                continue
            row.status, row.last_error, row.sent_at = "sent", None, now
            result["sent"] += 1
    return result


def price_drop_note(session: Session, notification: Notification) -> str | None:
    """'Prezzo sceso da X a Y': Y comes from the reason, X is the observation before Y in the price history."""
    if not notification.reason.startswith(f"{PRICE_DROP}:"):
        return None
    amount = Decimal(notification.reason.split(":", 1)[1])
    listing = notification.opportunity.match.listing
    observations = list(session.scalars(
        select(ListingPriceHistory).where(ListingPriceHistory.listing_id == listing.id).order_by(ListingPriceHistory.observed_at, ListingPriceHistory.id)
    ))
    at = max((i for i, o in enumerate(observations) if o.price_amount == amount), default=None)
    previous = next((o for o in reversed(observations[:at]) if o.price_amount is not None), None) if at else None
    new = _money(amount, listing.currency)
    return f"Prezzo sceso da {_money(previous.price_amount, None)} a {new}" if previous else f"Prezzo sceso a {new}"


def _money(amount: Decimal, currency: str | None) -> str:
    text = f"{amount:.2f}".replace(".", ",")
    return f"{text} {currency}" if currency else text


def to_message(opportunity: Opportunity, note: str | None = None) -> OutgoingMessage:
    listing: Listing = opportunity.match.listing
    return OutgoingMessage(
        title=listing.title or listing.external_id,
        price=format_price(listing),
        location=format_location(listing.location),
        score=opportunity.match.score,
        url=listing.url,
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
