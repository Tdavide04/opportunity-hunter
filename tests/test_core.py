from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from opportunity_hunter.adapter import InvalidParams, Page, PermanentMarketplaceError
from opportunity_hunter.db.models import Base
from opportunity_hunter.domain import ListingCandidate
from opportunity_hunter.hunter.matching import match
from opportunity_hunter.hunter.scoring import score


def candidate(**changes):
    values = {
        "external_id": "1",
        "title": "Resident Evil 2",
        "url": "https://www.subito.it/item.htm",
        "price_status": "priced",
        "price_amount": Decimal("25.00"),
        "currency": "EUR",
        "shipping_status": "known",
        "shipping_amount": Decimal("2.99"),
        "raw_payload": {},
    }
    values.update(changes)
    return ListingCandidate(**values)


def test_canonical_candidate_validates_urls_and_totals():
    item = candidate()
    assert item.external_id == "1"
    assert item.price_amount == Decimal("25.00")
    assert item.shipping_amount == Decimal("2.99")
    with pytest.raises(ValueError, match="http"):
        candidate(url="javascript:alert(1)")
    with pytest.raises(ValueError, match="shipping"):
        candidate(shipping_status="known", shipping_amount=None)


def test_adapter_contract_types():
    page = Page(candidates=[candidate()], next_cursor=2, newest_first=True)
    assert page.candidates[0].external_id == "1"
    assert InvalidParams([(("region",), "invalid")]).errors == [(("region",), "invalid")]
    assert PermanentMarketplaceError("blocked", status=403).status == 403


def test_core_tables_keep_existing_names():
    expected = {
        "search_profile", "marketplace_query", "acquisition_run", "listing",
        "match", "opportunity", "notification", "app_setting", "listing_price_history",
    }
    assert set(Base.metadata.tables) == expected


def test_matching_and_scoring_are_pure_and_deterministic():
    listing = SimpleNamespace(
        title="Resident Evil 2 GameCube",
        description="Complete box",
        price_status="priced",
        price_amount=Decimal("25.00"),
        currency="EUR",
        shipping_status="known",
        shipping_amount=Decimal("2.99"),
        fees_amount=None,
        published_at=None,
    )
    profile = SimpleNamespace(
        title_only=False,
        keywords=["resident evil"],
        excluded_terms=["replica"],
        listed_since=None,
        currency="EUR",
        price_min=None,
        price_max=Decimal("50.00"),
        price_target=Decimal("30.00"),
    )
    result = match(listing, profile)
    assert result.matched
    value, breakdown = score(listing, profile, result, datetime.now(UTC))
    assert 0 <= value <= 100
    assert set(breakdown) == {"price", "freshness", "title", "price_drop"}
