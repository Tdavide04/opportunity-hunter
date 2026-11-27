"""Completing the total cost from item pages (ADR-0011): only Listings a profile wants, within the limit. No DB, no network."""
from decimal import Decimal

from opportunity_hunter.adapter import ItemPricing, PermanentMarketplaceError, TransientMarketplaceError
from opportunity_hunter.db.models import Listing, SearchProfile
from opportunity_hunter.hunter.matching import known_cost
from opportunity_hunter.hunter.pricing import apply_pricing, complete_pricing


def listing(external_id: str, title: str = "Resident Evil 4", price: str = "10.00", **fields) -> Listing:
    values = {"marketplace": "vinted", "price_status": "priced", "currency": "EUR", "shipping_status": "unknown", "fees_amount": Decimal("1.20"), **fields}
    return Listing(external_id=external_id, title=title, price_amount=Decimal(price), **values)


PROFILE = SearchProfile(name="RE", keywords=["Resident Evil"], excluded_terms=["cover"], title_only=False, currency="EUR", price_max=Decimal("20.00"))
QUOTE = ItemPricing("known", shipping_amount=Decimal("4.99"), fees_amount=Decimal("1.35"), currency="EUR")


class Source:
    def __init__(self, outcomes: dict[str, object] | None = None):
        self.outcomes = outcomes or {}
        self.calls: list[tuple[dict, str]] = []

    def item_pricing(self, params, external_id):
        self.calls.append((params, external_id))
        outcome = self.outcomes.get(external_id, QUOTE)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_only_listings_a_profile_wants_cost_a_request():
    wanted = listing("1")
    unwanted = [listing("2", title="Silent Hill"), listing("3", title="Resident Evil cover"), listing("4", price="25.00"),
                listing("5", shipping_status="known", shipping_amount=Decimal("3.00"))]
    source = Source()
    assert complete_pricing(source, {"domain": "vinted.it"}, [wanted, *unwanted], [PROFILE], limit=10) == 1
    assert source.calls == [({"domain": "vinted.it"}, "1")]
    assert (wanted.shipping_status, wanted.shipping_amount, wanted.fees_amount) == ("known", Decimal("4.99"), Decimal("1.35"))
    assert known_cost(wanted, PROFILE) == Decimal("16.34")


def test_limit_caps_the_item_pages_per_round():
    listings = [listing(str(i)) for i in range(5)]
    source = Source()
    assert complete_pricing(source, {}, listings, [PROFILE], limit=2) == 2
    assert [lst.shipping_status for lst in listings] == ["known", "known", "unknown", "unknown", "unknown"]


def test_permanent_error_skips_the_listing_transient_error_ends_the_round():
    listings = [listing("gone"), listing("ok"), listing("busy"), listing("after")]
    source = Source({"gone": PermanentMarketplaceError("HTTP 404", status=404), "busy": TransientMarketplaceError("HTTP 429")})
    complete_pricing(source, {}, listings, [PROFILE], limit=10)
    assert [lst.shipping_status for lst in listings] == ["unknown", "known", "unknown", "unknown"]
    assert [call[1] for call in source.calls] == ["gone", "ok", "busy"]


def test_pickup_only_unknown_and_foreign_currency():
    pickup = listing("p")
    apply_pricing(pickup, ItemPricing("pickup_only", fees_amount=Decimal("0.90"), currency="EUR"))
    assert (pickup.shipping_status, pickup.shipping_amount, pickup.fees_amount) == ("pickup_only", None, Decimal("0.90"))

    unknown = listing("u")
    apply_pricing(unknown, ItemPricing("unknown"))
    assert (unknown.shipping_status, unknown.fees_amount) == ("unknown", Decimal("1.20"))  # never zero, fee kept

    foreign = listing("f")
    apply_pricing(foreign, ItemPricing("known", shipping_amount=Decimal("15.00"), currency="PLN"))
    assert foreign.shipping_status == "unknown"
