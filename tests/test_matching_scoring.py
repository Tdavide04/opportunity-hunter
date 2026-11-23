"""Table tests for the pure matching and scoring functions (internal seam of the pipeline)."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from opportunity_hunter.db.models import Listing, SearchProfile
from opportunity_hunter.hunter.matching import match
from opportunity_hunter.hunter.scoring import SCORING_VERSION, score

NOW = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def profile(**overrides) -> SearchProfile:
    fields = dict(
        name="RE", keywords=["Resident Evil", "Biohazard"], title_only=False, excluded_terms=["cover", "replica"],
        price_min=None, price_max=Decimal("25.00"), price_target=Decimal("15.00"), currency="EUR", listed_since=None, score_threshold=0,
    )
    return SearchProfile(**{**fields, **overrides})


def listing(**overrides) -> Listing:
    fields = dict(
        marketplace="fixture", external_id="1", title="Resident Evil 2 PS1 PAL", description=None,
        price_status="priced", price_amount=Decimal("12.00"), currency="EUR", published_at=NOW - timedelta(hours=2),
        shipping_status="unknown", shipping_amount=None, fees_amount=None,
        first_seen_at=NOW, last_seen_at=NOW,
    )
    return Listing(**{**fields, **overrides})


# --- matching ---------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("listing_fields", "profile_fields", "matched"),
    [
        ({}, {}, True),
        ({"title": "BIOHAZARD 3 last escape"}, {}, True),  # any keyword, case-insensitive
        ({"title": "Résident Évil 4"}, {}, True),  # accents ignored
        ({"title": "Resident-Evil Code Veronica"}, {}, True),  # punctuation is a separator
        ({"title": "Silent Hill 2"}, {}, False),
        ({"title": "Lotto giochi PS1", "description": "contiene Resident Evil 2"}, {}, True),
        ({"title": "Lotto giochi PS1", "description": "contiene Resident Evil 2"}, {"title_only": True}, False),
        ({"title": "Residential Evilness"}, {}, False),  # whole words only
        ({"title": "Resident Evil 2 cover custom"}, {}, False),  # excluded term
        ({"title": "Resident Evil 2 COVER"}, {}, False),
        ({"title": "Resident Evil 2 discovery edition"}, {}, True),  # "cover" inside a word is not excluded
        ({"title": "Resident Evil 2", "description": "non è una replica"}, {}, True),  # exclusions look at the title only
        ({"price_amount": Decimal("30.00")}, {}, False),  # above max
        ({"price_amount": Decimal("25.00")}, {}, True),  # max is inclusive
        ({"price_amount": Decimal("3.00")}, {"price_min": Decimal("5.00")}, False),  # below min
        ({"price_status": "free", "price_amount": None}, {}, True),
        ({"price_status": "free", "price_amount": None}, {"price_min": Decimal("5.00")}, False),  # free counts as 0
        ({"price_status": "missing", "price_amount": None}, {}, True),  # not discarded, penalised in the Score
        ({"price_status": "on_request", "price_amount": None}, {"price_min": Decimal("5.00")}, True),
        ({"price_amount": Decimal("30.00"), "currency": "GBP"}, {}, True),  # other currency: not comparable, penalised
        ({"published_at": datetime(2026, 10, 1, tzinfo=UTC)}, {"listed_since": date(2026, 11, 1)}, False),
        ({"published_at": datetime(2026, 11, 1, 0, 30, tzinfo=UTC)}, {"listed_since": date(2026, 11, 1)}, True),
        ({"published_at": None}, {"listed_since": date(2026, 11, 1)}, True),  # unknown date is not a reason to drop
        # total cost = price + shipping + mandatory fees; unknown shipping is never zero
        ({"price_amount": Decimal("20.00"), "shipping_status": "known", "shipping_amount": Decimal("6.00")}, {}, False),  # 26 > 25
        ({"price_amount": Decimal("20.00"), "fees_amount": Decimal("5.50")}, {}, False),  # 25.50 > 25 even before shipping
        ({"price_amount": Decimal("20.00"), "fees_amount": Decimal("1.50")}, {}, True),  # 21.50 + unknown shipping: maybe
        ({"price_amount": Decimal("20.00"), "shipping_status": "pickup_only"}, {}, True),
    ],
)
def test_matching_rules(listing_fields, profile_fields, matched):
    assert match(listing(**listing_fields), profile(**profile_fields)).matched is matched


def test_match_reasons_explain_the_decision():
    result = match(listing(title="Resident Evil 2 replica", price_amount=Decimal("30.00")), profile())
    assert result.matched is False
    assert result.reasons["keywords"] == ["Resident Evil"]
    assert result.reasons["excluded"] == ["replica"]
    assert result.reasons["price"] == "above_max"

    ok = match(listing(price_status="missing", price_amount=None), profile()).reasons
    assert (ok["keywords_in_title"], ok["excluded"], ok["price"], ok["listed_since"]) == (True, [], "unknown", "ok")


# --- scoring ----------------------------------------------------------------------------------

def scored(**listing_fields):
    lst, prof = listing(**listing_fields), profile()
    return score(lst, prof, match(lst, prof), NOW)


def test_score_is_bounded_and_explained():
    value, breakdown = scored()
    assert 0 <= value <= 100
    assert set(breakdown) == {"price", "freshness", "title", "price_drop"}
    assert value == sum(breakdown.values())
    assert SCORING_VERSION >= 1


@pytest.mark.parametrize(
    ("better", "worse"),
    [
        ({"price_amount": Decimal("10.00")}, {"price_amount": Decimal("20.00")}),  # cheaper is better
        ({"price_amount": Decimal("15.00")}, {"price_amount": Decimal("25.00")}),  # at target beats at max
        ({"price_status": "free", "price_amount": None}, {"price_amount": Decimal("20.00")}),  # free beats above-target
        ({"price_amount": Decimal("24.00")}, {"price_status": "missing", "price_amount": None}),  # unknown price is penalised
        ({"price_amount": Decimal("24.00")}, {"price_status": "on_request", "price_amount": None}),
        ({"published_at": NOW - timedelta(hours=1)}, {"published_at": NOW - timedelta(days=10)}),  # fresher is better
        ({"title": "Resident Evil 2"}, {"title": "Lotto giochi", "description": "Resident Evil 2"}),  # keyword in title
    ],
)
def test_score_orders_matches_sensibly(better, worse):
    assert scored(**better)[0] > scored(**worse)[0]


def test_price_at_or_below_target_gets_the_full_price_factor():
    assert scored(price_amount=Decimal("15.00"))[1]["price"] == scored(price_amount=Decimal("1.00"))[1]["price"]


def test_unknown_publication_date_gets_a_neutral_freshness():
    fresh = scored(published_at=NOW)[1]["freshness"]
    stale = scored(published_at=NOW - timedelta(days=60))[1]["freshness"]
    unknown = scored(published_at=None)[1]["freshness"]
    assert stale < unknown < fresh


def test_profile_without_price_bounds_scores_price_neutrally():
    lst, prof = listing(), profile(price_max=None, price_target=None)
    _, breakdown = score(lst, prof, match(lst, prof), NOW)
    assert 0 < breakdown["price"] < scored(price_amount=Decimal("1.00"))[1]["price"]


def test_reasons_carry_total_cost_and_shipping_state():
    lst = listing(price_amount=Decimal("20.00"), fees_amount=Decimal("1.50"))
    reasons = match(lst, profile()).reasons
    assert (reasons["total_cost"], reasons["shipping"]) == ("21.50", "unknown")
    known = listing(price_amount=Decimal("10.00"), shipping_status="known", shipping_amount=Decimal("4.00"), fees_amount=Decimal("1.00"))
    assert match(known, profile()).reasons["total_cost"] == "15.00"


def test_unknown_shipping_is_penalised_against_known_shipping():
    known = scored(price_amount=Decimal("10.00"), shipping_status="known", shipping_amount=Decimal("0.00"))
    unknown = scored(price_amount=Decimal("10.00"))
    assert unknown[0] < known[0]
    assert known[1]["price"] - unknown[1]["price"] == 5


def test_score_compares_total_cost_with_target():
    cheap_total = scored(price_amount=Decimal("10.00"), shipping_status="known", shipping_amount=Decimal("3.00"))
    dear_total = scored(price_amount=Decimal("10.00"), shipping_status="known", shipping_amount=Decimal("12.00"))
    assert cheap_total[0] > dear_total[0]
