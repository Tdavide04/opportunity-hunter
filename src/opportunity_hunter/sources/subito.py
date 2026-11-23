"""Adapter from standalone Subito Scanner (optional extra `subito`) to the OH source port, and the default source set."""
from typing import Any

from opportunity_hunter.adapter import MarketplaceAdapter, Page, PermanentMarketplaceError
from opportunity_hunter.sources.common import load_scanner, to_page, translate_errors

TRANSIENT, PERMANENT = "TransientSubitoError", "PermanentSubitoError"


class SubitoSourceAdapter:
    marketplace = "subito"
    capabilities = frozenset({"description", "images", "location", "seller", "condition", "published_at"})

    def __init__(self, scanner: Any | None = None) -> None:
        self.scanner = scanner if scanner is not None else load_scanner("subito_scanner", "SubitoScanner", "subito")

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = translate_errors(lambda: self.scanner.validate_params(raw), TRANSIENT, PERMANENT)
        return result

    def fetch_page(self, params: dict[str, Any], cursor: Any | None) -> Page:
        return to_page(translate_errors(lambda: self.scanner.fetch_page(params, cursor), TRANSIENT, PERMANENT))


class UnavailableSource:
    def __init__(self, marketplace: str, reason: str) -> None:
        self.marketplace = marketplace
        self.reason = reason
        self.capabilities: frozenset[str] = frozenset()

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        return dict(raw)

    def fetch_page(self, params: dict[str, Any], cursor: Any | None) -> Page:
        raise PermanentMarketplaceError(f"{self.marketplace}: {self.reason}")


def default_sources() -> dict[str, MarketplaceAdapter]:
    """Load the installed optional scanners without making them mandatory dependencies of the core."""
    from opportunity_hunter.sources.vinted import VintedSourceAdapter

    sources: dict[str, MarketplaceAdapter] = {}
    try:
        sources["vinted"] = VintedSourceAdapter()
    except RuntimeError as exc:
        sources["vinted"] = UnavailableSource("vinted", str(exc))
    try:
        sources["subito"] = SubitoSourceAdapter()
    except RuntimeError:
        pass
    return sources
