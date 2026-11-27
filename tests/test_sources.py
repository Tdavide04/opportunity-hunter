"""Scanner source adapters (anti-corruption layer): scanner pages and errors become OH's source port. Fake scanners, no network."""
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import pytest

from opportunity_hunter.adapter import InvalidParams, ItemPricing, PermanentMarketplaceError, TransientMarketplaceError
from opportunity_hunter.domain import ListingCandidate
from opportunity_hunter.sources.subito import SubitoSourceAdapter, UnavailableSource, default_sources
from opportunity_hunter.sources.vinted import VintedSourceAdapter


@dataclass(frozen=True)
class Listing:  # the shape shared by SubitoListing and VintedListing
    external_id: str
    title: str
    url: str
    price_status: str
    raw_payload: dict[str, Any]
    price_amount: Decimal | None = None
    currency: str | None = None
    description: str | None = None
    images: list[str] = field(default_factory=list)
    location: dict[str, Any] | None = None
    seller_external_id: str | None = None
    seller_name: str | None = None
    condition: str | None = None
    condition_raw: str | None = None
    published_at: Any = None
    shipping_status: str = "unknown"
    shipping_amount: Decimal | None = None
    fees_amount: Decimal | None = None


@dataclass(frozen=True)
class Rejected:
    external_id: str | None
    reason: str


@dataclass(frozen=True)
class ScannerPage:
    listings: list[Listing]
    rejected: list[Rejected]
    next_cursor: int | None
    newest_first: bool


class InvalidQueryError(ValueError):
    def __init__(self, errors):
        super().__init__("invalid")
        self.errors = errors


class TransientVintedError(Exception):
    pass


class PermanentVintedError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class TransientSubitoError(Exception):
    pass


class FakeScanner:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple] = []

    def validate_params(self, raw):
        if self.error:
            raise self.error
        return {**raw, "validated": True}

    def fetch_page(self, params, cursor):
        self.calls.append(("fetch_page", params, cursor))
        if self.error:
            raise self.error
        item = Listing("1", "Resident Evil", "https://www.vinted.it/items/1", "priced", {"id": 1}, Decimal("32.00"), "EUR",
                       images=["https://img.test/1.webp"], seller_name="s", condition="good", condition_raw="Buone", fees_amount=Decimal("2.30"))
        return ScannerPage([item], [Rejected(None, "no title")], 2, True)

    def search_catalogs(self, domain, text):
        if self.error:
            raise self.error
        return [{"id": "3026", "label": "Giochi"}]

    def list_filters(self, domain, catalog_id):
        return [{"code": "brand", "title": "Brand", "searchable": True}]

    def filter_options(self, domain, code, catalog_id, text, searchable=False):
        return [{"id": "1", "label": text, "items_count": None, "searchable": searchable}]


@pytest.mark.parametrize("adapter_class", [VintedSourceAdapter, SubitoSourceAdapter])
def test_scanner_page_becomes_canonical_candidates(adapter_class):
    scanner = FakeScanner()
    source = adapter_class(scanner)
    assert source.validate_params({"search_text": "x"}) == {"search_text": "x", "validated": True}
    page = source.fetch_page({"search_text": "x"}, 1)
    (candidate,) = page.candidates
    assert isinstance(candidate, ListingCandidate)
    assert (candidate.external_id, candidate.price_amount, candidate.fees_amount, candidate.condition) == ("1", Decimal("32.00"), Decimal("2.30"), "good")
    assert candidate.images == ["https://img.test/1.webp"] and candidate.raw_payload == {"id": 1}
    assert [(r.external_id, r.reason) for r in page.rejected] == [(None, "no title")]
    assert (page.next_cursor, page.newest_first) == (2, True)
    assert scanner.calls == [("fetch_page", {"search_text": "x"}, 1)]


def test_vinted_errors_map_to_the_source_port():
    with pytest.raises(InvalidParams) as invalid:
        VintedSourceAdapter(FakeScanner(InvalidQueryError([(("domain",), "bad")]))).validate_params({})
    assert invalid.value.errors == [(("domain",), "bad")]
    with pytest.raises(TransientMarketplaceError):
        VintedSourceAdapter(FakeScanner(TransientVintedError("503"))).fetch_page({}, None)
    with pytest.raises(PermanentMarketplaceError) as permanent:
        VintedSourceAdapter(FakeScanner(PermanentVintedError("403", status=403))).fetch_page({}, None)
    assert permanent.value.status == 403
    with pytest.raises(PermanentMarketplaceError):
        VintedSourceAdapter(FakeScanner(PermanentVintedError("blocked"))).search_catalogs("vinted.it", "x")
    with pytest.raises(KeyError):  # anything else is a bug and propagates unchanged
        VintedSourceAdapter(FakeScanner(KeyError("x"))).fetch_page({}, None)


def test_subito_errors_are_not_confused_with_vinted_ones():
    with pytest.raises(TransientMarketplaceError):
        SubitoSourceAdapter(FakeScanner(TransientSubitoError("timeout"))).fetch_page({}, None)
    with pytest.raises(TransientVintedError):
        SubitoSourceAdapter(FakeScanner(TransientVintedError("x"))).fetch_page({}, None)


def test_vinted_discovery_is_passed_through():
    source = VintedSourceAdapter(FakeScanner())
    assert source.search_catalogs("vinted.it", "giochi") == [{"id": "3026", "label": "Giochi"}]
    assert source.list_filters("vinted.it", "3026")[0]["code"] == "brand"
    assert source.filter_options("vinted.it", "brand", "3026", "capcom", searchable=True)[0]["searchable"] is True


def test_default_sources_without_the_extras(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_scanners(name, *args, **kwargs):
        if name.split(".")[0] in ("vinted_scanner", "subito_scanner"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_scanners)
    sources = default_sources()
    assert "subito" not in sources
    assert isinstance(sources["vinted"], UnavailableSource)
    with pytest.raises(PermanentMarketplaceError, match="'vinted' extra"):
        sources["vinted"].fetch_page({}, None)


def test_real_vinted_scanner_through_the_adapter():
    """With the `vinted` extra installed: the real scanner on its own anonymised fixture, no network."""
    testing = pytest.importorskip("vinted_scanner.testing")
    scanner = testing.scanner_with(testing.FakeVinted([testing.response(200, testing.load_fixture("catalog_page"))]))
    source = VintedSourceAdapter(scanner)
    page = source.fetch_page(source.validate_params({"search_text": "resident evil"}), None)
    assert len(page.candidates) == 5 and page.next_cursor == 2 and page.newest_first is True
    assert page.candidates[0].fees_amount == Decimal("2.30")
    with pytest.raises(InvalidParams):
        source.validate_params({})


def test_real_vinted_item_pricing_through_the_adapter():
    """With the `vinted` extra installed: shipping and buyer protection from the scanner's real item page fixture."""
    testing = pytest.importorskip("vinted_scanner.testing")
    scanner = testing.scanner_with(testing.FakeVinted([], item_pages={"9000000001": testing.item_page_response()}))
    source = VintedSourceAdapter(scanner)
    params = source.validate_params({"search_text": "resident evil"})
    assert source.item_pricing(params, "9000000001") == ItemPricing("known", Decimal("4.99"), Decimal("1.35"), "EUR")
    with pytest.raises(PermanentMarketplaceError) as exc:
        source.item_pricing(params, "404")
    assert exc.value.status == 404


def test_real_subito_scanner_errors_through_the_adapter():
    """With the `subito` extra installed: a 503 from the real scanner becomes a transient source error."""
    pytest.importorskip("subito_scanner")
    from subito_scanner import SubitoScanner, SubitoScannerConfig
    from subito_scanner.testing import FakeSession, FakeTime, response

    time = FakeTime()
    config = SubitoScannerConfig(min_interval_s=0, timeout_s=5, max_retries=0, backoff_base_s=0, backoff_cap_s=0, max_retry_after_s=0)
    scanner = SubitoScanner(config=config, session=FakeSession(time, [response(503)]), clock=time.clock, sleep=time.sleep)
    source = SubitoSourceAdapter(scanner)
    with pytest.raises(TransientMarketplaceError):
        source.fetch_page(source.validate_params({"search_text": "x"}), None)
