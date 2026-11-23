"""Score of a Match, 0-100, as the sum of explained factors. Pure: no I/O.

Weights live in code and are versioned: bump SCORING_VERSION whenever a rule or weight changes,
so stored Matches say which rules produced them.
"""
from datetime import datetime
from decimal import Decimal

from opportunity_hunter.db.models import Listing, SearchProfile
from opportunity_hunter.hunter.matching import MatchResult

SCORING_VERSION = 3  # v2: Price Drop factor (#12); v3: total cost with shipping and fees (#9, ADR-0011)

PRICE_WEIGHT = 45
FRESHNESS_WEIGHT = 25
TITLE_WEIGHT = 20
PRICE_DROP_WEIGHT = 10
FULL_DROP_PERCENT = Decimal(20)  # a drop of 20 % or more earns the whole factor

UNKNOWN_PRICE_POINTS = 3  # missing / on request / other currency: kept, but ranked below known prices
NO_BOUNDS_PRICE_POINTS = 22  # the profile gives nothing to compare with
AT_MAX_PRICE_POINTS = 9
FRESH_HOURS = 24
STALE_DAYS = 30
TITLE_IN_DESCRIPTION_POINTS = 8
UNKNOWN_SHIPPING_PENALTY = 5  # the real total can only be higher than what we compared


def score(
    listing: Listing, profile: SearchProfile, result: MatchResult, now: datetime, previous_price: Decimal | None = None
) -> tuple[int, dict[str, int]]:
    """`previous_price`: the amount before a Price Drop observed in this run, if any."""
    breakdown = {
        "price": _price_points(listing, profile, result),
        "freshness": _freshness_points(listing, now),
        "title": TITLE_WEIGHT if result.reasons["keywords_in_title"] else TITLE_IN_DESCRIPTION_POINTS if result.reasons["keywords"] else 0,
        "price_drop": _price_drop_points(listing, previous_price),
    }
    return sum(breakdown.values()), breakdown


def _price_points(listing: Listing, profile: SearchProfile, result: MatchResult) -> int:
    if result.reasons["price"] == "unknown":
        return UNKNOWN_PRICE_POINTS
    points = _cost_points(Decimal(result.reasons["total_cost"]), profile)
    if result.reasons["shipping"] == "unknown":
        points = max(0, points - UNKNOWN_SHIPPING_PENALTY)
    return points


def _cost_points(amount: Decimal, profile: SearchProfile) -> int:
    target, ceiling = profile.price_target, profile.price_max
    if target is None and ceiling is None:
        return NO_BOUNDS_PRICE_POINTS
    if target is not None and amount <= target:
        return PRICE_WEIGHT
    if ceiling is None or (target is not None and target >= ceiling):
        # above target with no usable maximum: lose points proportionally, down to the at-max floor at twice the target
        excess = min(Decimal(1), (amount - target) / target) if target else Decimal(1)
        return round(PRICE_WEIGHT - (PRICE_WEIGHT - AT_MAX_PRICE_POINTS) * excess)
    start = target if target is not None else Decimal(0)
    position = min(Decimal(1), max(Decimal(0), (amount - start) / (ceiling - start))) if ceiling > start else Decimal(1)
    return round(PRICE_WEIGHT - (PRICE_WEIGHT - AT_MAX_PRICE_POINTS) * position)


def drop_percent(previous: Decimal, current: Decimal) -> Decimal:
    return (previous - current) / previous * 100 if previous else Decimal(0)


def _price_drop_points(listing: Listing, previous_price: Decimal | None) -> int:
    if previous_price is None or listing.price_amount is None or listing.price_amount >= previous_price:
        return 0
    return round(PRICE_DROP_WEIGHT * min(Decimal(1), drop_percent(previous_price, listing.price_amount) / FULL_DROP_PERCENT))


def _freshness_points(listing: Listing, now: datetime) -> int:
    if listing.published_at is None:
        return FRESHNESS_WEIGHT // 2
    age_hours = max(0.0, (now - listing.published_at).total_seconds() / 3600)
    if age_hours <= FRESH_HOURS:
        return FRESHNESS_WEIGHT
    remaining = 1 - min(1.0, (age_hours - FRESH_HOURS) / (STALE_DAYS * 24 - FRESH_HOURS))
    return round(FRESHNESS_WEIGHT * remaining)
