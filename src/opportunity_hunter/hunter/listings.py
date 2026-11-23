"""Reconciles Listing Candidates with known Listings: one Listing per (Marketplace, External ID)."""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from opportunity_hunter.db.models import Listing, ListingPriceHistory
from opportunity_hunter.domain import ListingCandidate

# canonical fields copied onto the Listing; raw_payload is stored but kept out of the hash (volatile counters)
CANONICAL_FIELDS = (
    "title", "description", "price_amount", "currency", "price_status", "url", "images", "location",
    "seller_external_id", "seller_name", "condition", "condition_raw", "published_at",
    "shipping_status", "shipping_amount", "fees_amount",
)


@dataclass(frozen=True)
class PageCounts:
    new: int
    updated: int
    unchanged: int
    changed: list[Listing] = field(default_factory=list)  # new or updated: always (re-)evaluated
    seen: list[Listing] = field(default_factory=list)  # every Listing on the page
    price_drops: dict[int, Decimal] = field(default_factory=dict)  # listing id -> previous amount, for prices that went down
    promoted: set[int] = field(default_factory=set)  # Legacy Seen Listings completed by this page: never notified


def content_hash(candidate: ListingCandidate) -> str:
    canonical = {name: getattr(candidate, name) for name in CANONICAL_FIELDS}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, default=str).encode()).hexdigest()


def upsert_page(session: Session, marketplace: str, candidates: list[ListingCandidate], run_id: int, now: datetime) -> PageCounts:
    """Insert new Listings, rewrite changed ones, touch Last Seen on all. Caller owns the transaction.

    shortcut: select-then-write instead of INSERT ... ON CONFLICT, safe while one sequential worker writes
    Listings (ADR-0001); revisit if acquisitions ever run concurrently.
    """
    by_id = {c.external_id: c for c in candidates}  # last occurrence wins within a page
    known = {
        lst.external_id: lst
        for lst in session.scalars(
            select(Listing).where(Listing.marketplace == marketplace, Listing.external_id.in_(by_id)).with_for_update()
        )
    }
    new = updated = unchanged = 0
    changed: list[Listing] = []
    seen: list[Listing] = []
    price_drops: dict[int, Decimal] = {}
    promoted: set[int] = set()
    for external_id, candidate in by_id.items():
        digest = content_hash(candidate)
        listing = known.get(external_id)
        if listing is None:
            listing = Listing(marketplace=marketplace, external_id=external_id, first_seen_at=now)
            session.add(listing)
            new += 1
        elif listing.content_hash == digest and listing.origin == "acquired":
            unchanged += 1
        else:
            updated += 1
        if listing.content_hash != digest or listing.origin != "acquired":
            changed.append(listing)
            if listing.origin == "legacy_seen":
                promoted.add(listing.id)
            previous = (listing.price_status, listing.price_amount, listing.currency)
            current = (candidate.price_status, candidate.price_amount, candidate.currency)
            if previous != current:
                session.add(ListingPriceHistory(listing=listing, price_status=candidate.price_status, price_amount=candidate.price_amount,
                                                currency=candidate.currency, observed_at=now))
                if _is_drop(previous, current):
                    price_drops[listing.id] = listing.price_amount
            for name in CANONICAL_FIELDS:
                setattr(listing, name, getattr(candidate, name))
            listing.raw_payload = candidate.raw_payload
            listing.content_hash = digest
            listing.origin = "acquired"  # completes a Legacy Seen Listing met again
        seen.append(listing)
        listing.last_seen_at = now
        listing.last_seen_run_id = run_id
        listing.availability = "active"
    session.flush()  # assign ids to new Listings before they are evaluated
    return PageCounts(new=new, updated=updated, unchanged=unchanged, changed=changed, seen=seen, price_drops=price_drops, promoted=promoted)


def _is_drop(previous: tuple, current: tuple) -> bool:
    """A Price Drop compares two known amounts in the same currency (status, amount, currency)."""
    return previous[0] == current[0] == "priced" and previous[2] == current[2] and current[1] < previous[1]
