"""Adapter from standalone Subito Scanner to the OH canonical ListingCandidate."""
from importlib import import_module
from typing import Any

from opportunity_hunter.adapter import MarketplaceAdapter, Page, PermanentMarketplaceError, Rejected, TransientMarketplaceError
from opportunity_hunter.domain import ListingCandidate


class SubitoSourceAdapter:
    marketplace = "subito"
    capabilities = frozenset({"description", "images", "location", "seller", "condition", "published_at"})

    def __init__(self, scanner: Any | None = None) -> None:
        if scanner is None:
            try:
                module = import_module("subito_scanner")
            except ImportError as exc:
                raise RuntimeError("install Opportunity Hunter with the 'subito' extra to enable Subito") from exc
            scanner = module.SubitoScanner()
        self.scanner = scanner

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.scanner.validate_params(raw)
        except Exception as exc:
            if type(exc).__name__ == "InvalidQueryError":
                from opportunity_hunter.adapter import InvalidParams
                raise InvalidParams(exc.errors) from exc
            raise

    def fetch_page(self, params: dict[str, Any], cursor: Any | None) -> Page:
        try:
            page = self.scanner.fetch_page(params, cursor)
        except Exception as exc:
            if type(exc).__name__ == "TransientSubitoError":
                raise TransientMarketplaceError(str(exc)) from exc
            if type(exc).__name__ == "PermanentSubitoError":
                raise PermanentMarketplaceError(str(exc), status=getattr(exc, "status", None)) from exc
            raise
        candidates = [
            ListingCandidate(
                external_id=item.external_id,
                title=item.title,
                url=item.url,
                price_status=item.price_status,
                raw_payload=item.raw_payload,
                price_amount=item.price_amount,
                currency=item.currency,
                description=item.description,
                images=list(item.images),
                location=item.location,
                seller_external_id=item.seller_external_id,
                seller_name=item.seller_name,
                condition=item.condition,
                condition_raw=item.condition_raw,
                published_at=item.published_at,
                shipping_status=item.shipping_status,
                shipping_amount=item.shipping_amount,
                fees_amount=item.fees_amount,
            )
            for item in page.listings
        ]
        rejected = [Rejected(external_id=item.external_id, reason=item.reason) for item in page.rejected]
        return Page(candidates=candidates, rejected=rejected, next_cursor=page.next_cursor, newest_first=page.newest_first)


class UnavailableSource:
    def __init__(self, marketplace: str, reason: str) -> None:
        self.marketplace = marketplace
        self.reason = reason
        self.capabilities = frozenset()

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def fetch_page(self, params: dict[str, Any], cursor: Any | None) -> Page:
        raise PermanentMarketplaceError(f"{self.marketplace}: {self.reason}")


def default_sources() -> dict[str, MarketplaceAdapter]:
    """Load optional scanners without making them mandatory dependencies of the core."""
    sources: dict[str, MarketplaceAdapter] = {
        "vinted": UnavailableSource(
            "vinted",
            "vinted-scanner has not yet been extracted as a standalone package; provide a compatible adapter",
        )
    }
    try:
        sources["subito"] = SubitoSourceAdapter()
    except RuntimeError:
        pass
    return sources
