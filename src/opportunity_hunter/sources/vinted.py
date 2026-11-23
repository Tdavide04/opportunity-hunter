"""Adapter from standalone Vinted Scanner (optional extra `vinted`, GPL-3.0-only) to the OH source port."""
from typing import Any

from opportunity_hunter.adapter import Page
from opportunity_hunter.sources.common import load_scanner, to_page, translate_errors

TRANSIENT, PERMANENT = "TransientVintedError", "PermanentVintedError"


class VintedSourceAdapter:
    marketplace = "vinted"
    # the Vinted catalogue list has no description, location or publication date
    capabilities = frozenset({"images", "seller", "condition"})

    def __init__(self, scanner: Any | None = None) -> None:
        self.scanner = scanner if scanner is not None else load_scanner("vinted_scanner", "VintedScanner", "vinted")

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = translate_errors(lambda: self.scanner.validate_params(raw), TRANSIENT, PERMANENT)
        return result

    def fetch_page(self, params: dict[str, Any], cursor: Any | None) -> Page:
        return to_page(translate_errors(lambda: self.scanner.fetch_page(params, cursor), TRANSIENT, PERMANENT))

    # filter discovery, for applications that help users build Vinted queries
    def search_catalogs(self, domain: str, text: str) -> list[dict[str, str]]:
        result: list[dict[str, str]] = translate_errors(lambda: self.scanner.search_catalogs(domain, text), TRANSIENT, PERMANENT)
        return result

    def list_filters(self, domain: str, catalog_id: str) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = translate_errors(lambda: self.scanner.list_filters(domain, catalog_id), TRANSIENT, PERMANENT)
        return result

    def filter_options(self, domain: str, code: str, catalog_id: str, text: str, searchable: bool = False) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = translate_errors(
            lambda: self.scanner.filter_options(domain, code, catalog_id, text, searchable=searchable), TRANSIENT, PERMANENT
        )
        return result
