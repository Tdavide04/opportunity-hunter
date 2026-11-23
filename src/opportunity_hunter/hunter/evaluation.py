"""Evaluates changed Listings against every active Search Profile of their Marketplace (ADR-0004)."""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from opportunity_hunter.db.models import Listing, ListingPriceHistory, MarketplaceQuery, Match, Opportunity, SearchProfile
from opportunity_hunter.hunter.matching import match
from opportunity_hunter.hunter.scoring import SCORING_VERSION, drop_percent, score


@dataclass
class Evaluation:
    created: list[Opportunity] = field(default_factory=list)  # new Opportunities: notify as new
    price_drops: list[tuple[Opportunity, str]] = field(default_factory=list)  # (existing Opportunity, price_drop:<amount>)


def profiles_for(session: Session, marketplace: str) -> list[SearchProfile]:
    """Active Search Profiles with at least one Marketplace Query on this Marketplace."""
    on_marketplace = select(MarketplaceQuery.search_profile_id).where(MarketplaceQuery.marketplace == marketplace)
    return list(session.scalars(select(SearchProfile).where(SearchProfile.status == "active", SearchProfile.id.in_(on_marketplace))))


def previous_prices(session: Session, listing_ids: list[int]) -> dict[int, Decimal]:
    """Listings whose latest observed price is lower than the one before (same currency): listing id -> previous amount.

    Read from the price history so the Price Drop factor is the same whenever the pair is re-evaluated.
    """
    ranked = (
        select(
            ListingPriceHistory.listing_id, ListingPriceHistory.price_amount, ListingPriceHistory.currency,
            func.row_number().over(
                partition_by=ListingPriceHistory.listing_id, order_by=(ListingPriceHistory.observed_at.desc(), ListingPriceHistory.id.desc())
            ).label("rank"),
        )
        .where(ListingPriceHistory.listing_id.in_(listing_ids))
        .subquery()
    )
    latest: dict[int, list] = {}
    for listing_id, amount, currency, _ in session.execute(select(ranked).where(ranked.c.rank <= 2).order_by(ranked.c.listing_id, ranked.c.rank)):
        latest.setdefault(listing_id, []).append((amount, currency))
    return {
        listing_id: rows[1][0]
        for listing_id, rows in latest.items()
        if len(rows) == 2 and None not in (rows[0][0], rows[1][0]) and rows[0][1] == rows[1][1] and rows[0][0] < rows[1][0]
    }


def evaluate(
    session: Session,
    listings: list[Listing],
    changed: list[Listing],
    profiles: list[SearchProfile],
    now: datetime,
    price_drops: dict[int, Decimal] | None = None,
    drop_min_percent: Decimal = Decimal(10),
) -> Evaluation:
    """Upsert one Match per (profile, listing); create the Opportunity when the Score reaches the threshold.

    A pair is (re-)evaluated when the Listing changed, when it has no Match yet (profile created later),
    or when the profile was edited after the last evaluation, so new criteria apply at the next run.
    An existing Opportunity keeps its status whatever the new Score: it records the user's decision.
    A significant Price Drop (>= drop_min_percent) on an existing Opportunity is reported for a dedicated
    Notification, also when dismissed (spec, story 51), but not once acquired. Caller owns the transaction.
    """
    price_drops = price_drops or {}  # drops observed in this run: they trigger price_drop Notifications
    outcome = Evaluation()
    if not listings or not profiles:
        return outcome
    known = {
        (m.search_profile_id, m.listing_id): m
        for m in session.scalars(
            select(Match).where(Match.listing_id.in_([lst.id for lst in listings]), Match.search_profile_id.in_([p.id for p in profiles]))
        )
    }
    changed_ids = {lst.id for lst in changed}
    session.flush()  # this page's price history rows must be visible to previous_prices
    dropped_from = previous_prices(session, [lst.id for lst in listings])
    for listing in listings:
        for profile in profiles:
            row = known.get((profile.id, listing.id))
            if row is not None and listing.id not in changed_ids and profile.updated_at <= row.evaluated_at:
                continue
            result = match(listing, profile)
            value, breakdown = score(listing, profile, result, now, dropped_from.get(listing.id)) if result.matched else (None, None)
            previous_price = price_drops.get(listing.id)
            if row is None:
                row = Match(search_profile_id=profile.id, listing_id=listing.id)
                session.add(row)
            row.matched = result.matched
            row.reasons = result.reasons
            row.score = value
            row.score_breakdown = breakdown
            row.scoring_version = SCORING_VERSION
            row.evaluated_at = now
            if value is not None and value >= profile.score_threshold and row.opportunity is None:
                row.opportunity = Opportunity(status="new", status_changed_at=now, created_at=now)
                outcome.created.append(row.opportunity)
            elif (
                row.opportunity is not None and result.matched and previous_price is not None
                and row.opportunity.status != "acquired"
                and drop_percent(previous_price, listing.price_amount) >= drop_min_percent
            ):
                outcome.price_drops.append((row.opportunity, f"price_drop:{listing.price_amount:.2f}"))
    return outcome
