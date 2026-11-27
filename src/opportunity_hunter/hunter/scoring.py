"""Score of a Match: two explained numbers, Relevance and Deal, and the Score that combines them. Pure: no I/O.

- Relevance (0-100): is it the object the user wants? Learned from their decisions (relevance.py).
- Deal (0-100): how far the total cost is below a reference price. 50 = at the reference, 100 = half of it.
  The reference is the median total cost of the profile's relevant Matches, pulled toward the profile's
  target price while there are few of them (empirical Bayes shrinkage).
- Score = geometric mean of the two: both must be high. It orders Opportunities and meets the Score Threshold.

Weights live in code and are versioned: bump SCORING_VERSION whenever a rule or weight changes,
so stored Matches say which rules produced them.
"""
import math
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from opportunity_hunter.db.models import Listing, SearchProfile
from opportunity_hunter.hunter.matching import MatchResult
from opportunity_hunter.hunter.relevance import Judgement

# v2: Price Drop factor (#12); v3: total cost with shipping and fees (#9, ADR-0011); v4: Relevance and Deal
SCORING_VERSION = 4

NEUTRAL_DEAL = 50  # nothing to compare with
UNKNOWN_PRICE_DEAL = 10  # missing / on request / other currency: kept, but ranked low
SHRINKAGE_WEIGHT = 5  # the target price counts as this many observed Matches
MIN_MARKET_WITHOUT_TARGET = 3  # with no target price, fewer relevant Matches than this give no reference
SUSPICIOUS_DISCOUNT = Decimal("0.6")  # cheaper than 40 % of the reference: often a part, a box or a broken item
SUSPICIOUS_DEAL_CAP = 70
UNKNOWN_SHIPPING_PENALTY = 5  # the real total can only be higher than what we compared
PRICE_DROP_POINTS = 10
FULL_DROP_PERCENT = Decimal(20)  # a drop of 20 % or more earns all the price drop points


@dataclass(frozen=True)
class Scored:
    score: int
    relevance: int
    deal: int
    breakdown: dict[str, Any]


def reference_price(market: list[Decimal], profile: SearchProfile) -> Decimal | None:
    """Median total cost of the relevant Matches, shrunk toward the profile's target (or maximum) price."""
    prior = profile.price_target if profile.price_target is not None else profile.price_max
    if prior is not None and prior <= 0:
        prior = None
    if not market:
        return prior
    middle = Decimal(median(market))
    if prior is None:
        return middle if len(market) >= MIN_MARKET_WITHOUT_TARGET and middle > 0 else None
    n = len(market)
    return (n * middle + SHRINKAGE_WEIGHT * prior) / (n + SHRINKAGE_WEIGHT)


def score(
    listing: Listing,
    result: MatchResult,
    relevance: Judgement,
    reference: Decimal | None,
    market_size: int,
    previous_price: Decimal | None = None,
) -> Scored:
    """`previous_price`: the amount before a Price Drop observed in this run, if any."""
    deal, deal_breakdown = _deal(listing, result, reference, previous_price)
    deal_breakdown["market_size"] = market_size
    breakdown = {
        "relevance": {"value": relevance.value, "words": [list(w) for w in relevance.words], "learning": relevance.learning},
        "deal": deal_breakdown,
    }
    return Scored(round(math.sqrt(relevance.value * deal)), relevance.value, deal, breakdown)


def _deal(listing: Listing, result: MatchResult, reference: Decimal | None, previous_price: Decimal | None) -> tuple[int, dict[str, Any]]:
    details: dict[str, Any] = {"reference": f"{reference:.2f}" if reference is not None else None, "discount_percent": None, "suspicious": False}
    if result.reasons["price"] == "unknown":
        value = UNKNOWN_PRICE_DEAL
    elif reference is None:
        value = NEUTRAL_DEAL
    else:
        discount = (reference - Decimal(result.reasons["total_cost"])) / reference
        details["discount_percent"] = round(discount * 100)
        value = round(NEUTRAL_DEAL + 100 * discount)
        if discount > SUSPICIOUS_DISCOUNT:
            details["suspicious"] = True
            value = min(value, SUSPICIOUS_DEAL_CAP)
    if result.reasons["price"] != "unknown" and result.reasons["shipping"] == "unknown":
        value -= UNKNOWN_SHIPPING_PENALTY
    drop = _price_drop_points(listing, previous_price)
    details["price_drop"] = drop
    value = max(0, min(100, value + drop))
    details["value"] = value
    return value, details


def drop_percent(previous: Decimal, current: Decimal) -> Decimal:
    return (previous - current) / previous * 100 if previous else Decimal(0)


def _price_drop_points(listing: Listing, previous_price: Decimal | None) -> int:
    if previous_price is None or listing.price_amount is None or listing.price_amount >= previous_price:
        return 0
    return round(PRICE_DROP_POINTS * min(Decimal(1), drop_percent(previous_price, listing.price_amount) / FULL_DROP_PERCENT))
