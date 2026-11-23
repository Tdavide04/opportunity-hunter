"""Local matching of a Listing against a Search Profile (ADR-0004). Pure: no I/O."""
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from opportunity_hunter.db.models import Listing, SearchProfile


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    reasons: dict[str, Any]


def normalize(text: str | None) -> str:
    """Casefold, drop accents, turn punctuation into spaces; padded so whole-word checks are substring checks."""
    if not text:
        return " "
    decomposed = unicodedata.normalize("NFKD", text.casefold())
    letters = "".join(ch if ch.isalnum() else " " for ch in decomposed if not unicodedata.combining(ch))
    return f" {' '.join(letters.split())} "


def contains_phrase(normalized_text: str, phrase: str) -> bool:
    words = normalize(phrase)
    return words.strip() != "" and words in normalized_text


def match(listing: Listing, profile: SearchProfile) -> MatchResult:
    """Keywords (any, whole words; title only if the profile says so), Excluded Terms (whole words, title),
    price range and minimum publication date. A price that is not comparable (missing, on request, other
    currency) does not discard the Listing: scoring penalises it instead.
    """
    title = normalize(listing.title)
    searchable = title if profile.title_only else title + normalize(listing.description)
    keywords = [k for k in profile.keywords if contains_phrase(searchable, k)]
    excluded = [t for t in profile.excluded_terms if contains_phrase(title, t)]
    price = price_position(listing, profile)
    listed_since = "unknown" if listing.published_at is None else (
        "too_old" if profile.listed_since and listing.published_at.date() < profile.listed_since else "ok"
    )
    cost = known_cost(listing, profile)
    reasons = {
        "total_cost": f"{cost:.2f}" if cost is not None else None,  # certain minimum: price + fees (+ shipping if known)
        "shipping": listing.shipping_status or "unknown",
        "keywords": keywords,
        "keywords_in_title": any(contains_phrase(title, k) for k in keywords),
        "excluded": excluded,
        "price": price,
        "listed_since": listed_since,
    }
    matched = bool(keywords) and not excluded and price not in ("below_min", "above_max") and listed_since != "too_old"
    return MatchResult(matched=matched, reasons=reasons)


def known_cost(listing: Listing, profile: SearchProfile) -> Decimal | None:
    """Certain minimum of the total cost: price + mandatory fees + shipping when known (unknown shipping is not zero,
    it is simply not included, so the real total can only be higher). None if the price is not comparable."""
    if listing.price_status == "free":
        price = Decimal(0)
    elif (
        listing.price_status == "priced"
        and listing.currency == profile.currency
        and listing.price_amount is not None
    ):
        price = listing.price_amount
    else:
        return None
    shipping = listing.shipping_amount if listing.shipping_status == "known" else Decimal(0)
    return price + (listing.fees_amount or Decimal(0)) + (shipping or Decimal(0))


def price_position(listing: Listing, profile: SearchProfile) -> str:
    """Total cost vs the profile range: 'within' | 'below_min' | 'above_max' | 'unknown' (missing, on request,
    another currency). With unknown shipping only the certain minimum is compared: above the maximum it is
    certainly too dear; otherwise it may still fit."""
    amount = known_cost(listing, profile)
    if amount is None:
        return "unknown"
    if profile.price_min is not None and amount < profile.price_min:
        return "below_min"
    if profile.price_max is not None and amount > profile.price_max:
        return "above_max"
    return "within"
