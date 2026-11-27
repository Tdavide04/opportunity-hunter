"""Completes the total cost of Listings whose list page has no shipping (ADR-0011): one item page per Listing that
matches a Search Profile, before it is scored. Listings no profile wants cost no request."""
import logging
from typing import Any

from opportunity_hunter.adapter import ItemPricing, PermanentMarketplaceError, PricingSource, TransientMarketplaceError
from opportunity_hunter.db.models import Listing, SearchProfile
from opportunity_hunter.hunter.matching import match

logger = logging.getLogger(__name__)


def complete_pricing(source: PricingSource, params: dict[str, Any], listings: list[Listing], profiles: list[SearchProfile], limit: int) -> int:
    """Price the Listings with unknown shipping that match at least one profile, at most `limit` item pages.

    shortcut: a Listing beyond the limit or whose page fails stays unknown until it changes again; add a backlog
    pass if baselines of large queries leave too many unknown. A transient error ends the round (the Marketplace
    is struggling); a permanent one skips that Listing. Returns the item pages requested.
    """
    requested = 0
    for listing in listings:
        if requested >= limit:
            break
        if listing.shipping_status != "unknown" or not any(match(listing, profile).matched for profile in profiles):
            continue
        requested += 1
        log = {"marketplace": listing.marketplace, "external_id": listing.external_id}
        try:
            apply_pricing(listing, source.item_pricing(params, listing.external_id))
        except TransientMarketplaceError as exc:
            logger.warning("item pricing stopped", extra={"event": "pricing_stopped", "reason": str(exc), **log})
            break
        except PermanentMarketplaceError as exc:
            logger.warning("item pricing failed", extra={"event": "pricing_failed", "status": exc.status, "reason": str(exc), **log})
    return requested


def apply_pricing(listing: Listing, pricing: ItemPricing) -> None:
    """Copy shipping and fees onto the Listing; amounts in another currency than its price are not comparable."""
    if pricing.currency is not None and pricing.currency != listing.currency:
        return
    if pricing.shipping_status != "unknown":
        listing.shipping_status = pricing.shipping_status
        listing.shipping_amount = pricing.shipping_amount
    if pricing.fees_amount is not None:
        listing.fees_amount = pricing.fees_amount
