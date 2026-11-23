"""Anti-corruption layer shared by the scanner source adapters: scanner pages and errors -> OH's source port."""
from collections.abc import Callable
from importlib import import_module
from typing import Any, cast

from opportunity_hunter.adapter import InvalidParams, Page, PermanentMarketplaceError, Rejected, TransientMarketplaceError
from opportunity_hunter.domain import ListingCandidate

# the canonical fields every scanner listing carries (subito_scanner.SubitoListing, vinted_scanner.VintedListing)
LISTING_FIELDS = (
    "external_id", "title", "url", "price_status", "raw_payload", "price_amount", "currency", "description", "location",
    "seller_external_id", "seller_name", "condition", "condition_raw", "published_at", "shipping_status", "shipping_amount", "fees_amount",
)


def load_scanner(module_name: str, class_name: str, extra: str) -> Any:
    """A scanner from an optional extra, or RuntimeError naming the extra to install."""
    try:
        module = import_module(module_name)
    except ImportError as exc:
        raise RuntimeError(f"install Opportunity Hunter with the '{extra}' extra to enable {extra.capitalize()}") from exc
    return getattr(module, class_name)()


def translate_errors[T](call: Callable[[], T], transient: str, permanent: str) -> T:
    """Run a scanner call, mapping its errors (matched by class name: the scanners are optional imports) to the source port's."""
    try:
        return call()
    except Exception as exc:
        name = type(exc).__name__
        if name == "InvalidQueryError":
            errors = getattr(exc, "errors", None)
            if isinstance(errors, list):
                raise InvalidParams(cast(list[tuple[tuple[str, ...], str]], errors)) from exc
        if name == transient:
            raise TransientMarketplaceError(str(exc)) from exc
        if name == permanent:
            raise PermanentMarketplaceError(str(exc), status=getattr(exc, "status", None)) from exc
        raise


def to_page(page: Any) -> Page:
    """A scanner page (listings, rejected, next_cursor, newest_first) as an OH Page of ListingCandidates."""
    candidates = [
        ListingCandidate(**{name: getattr(item, name) for name in LISTING_FIELDS}, images=list(item.images))
        for item in page.listings
    ]
    rejected = [Rejected(external_id=item.external_id, reason=item.reason) for item in page.rejected]
    return Page(candidates=candidates, rejected=rejected, next_cursor=page.next_cursor, newest_first=page.newest_first)
