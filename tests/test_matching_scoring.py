"""Table tests for the pure matching and scoring functions (internal seam of the pipeline)."""
import math
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from opportunity_hunter.db.models import Listing, SearchProfile
from opportunity_hunter.hunter import relevance
from opportunity_hunter.hunter.matching import match
from opportunity_hunter.hunter.relevance import Judgement
from opportunity_hunter.hunter.scoring import SCORING_VERSION, reference_price, score

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

NEUTRAL = Judgement(50, [], learning=True)


def scored(relevance: Judgement = NEUTRAL, reference: Decimal | None = Decimal("15.00"), previous_price=None, **listing_fields):
    lst, prof = listing(**listing_fields), profile()
    return score(lst, match(lst, prof), relevance, reference, 7, previous_price)


def test_score_is_the_geometric_mean_of_relevance_and_deal():
    result = scored(price_amount=Decimal("15.00"), shipping_status="known", shipping_amount=Decimal("0.00"))
    assert (result.relevance, result.deal, result.score) == (50, 50, 50)
    assert scored(Judgement(0, [], learning=False)).score == 0  # the wrong object never ranks, however cheap
    assert SCORING_VERSION == 4


def test_breakdown_explains_both_numbers():
    words = Judgement(80, [("completo", 1.5), ("pal", 0.4)], learning=False)
    result = scored(words, price_amount=Decimal("12.00"), shipping_status="known", shipping_amount=Decimal("0.00"))
    assert result.breakdown["relevance"] == {"value": 80, "words": [["completo", 1.5], ["pal", 0.4]], "learning": False}
    assert result.breakdown["deal"] == {
        "value": 70, "reference": "15.00", "discount_percent": 20, "suspicious": False, "price_drop": 0, "market_size": 7,
    }


@pytest.mark.parametrize(
    ("total", "deal"),
    [("15.00", 50), ("12.00", 70), ("7.50", 100), ("30.00", 0), ("3.00", 70)],  # 3.00 is -80 %: suspicious, capped
)
def test_deal_is_the_discount_on_the_reference(total, deal):
    assert scored(price_amount=Decimal(total), shipping_status="known", shipping_amount=Decimal("0.00")).deal == deal


def test_a_suspiciously_low_price_is_flagged():
    result = scored(price_amount=Decimal("3.00"), shipping_status="known", shipping_amount=Decimal("0.00"))
    assert result.breakdown["deal"]["suspicious"] is True


def test_unknown_price_and_unknown_shipping_lower_the_deal():
    assert scored(price_status="missing", price_amount=None).deal == 10
    known = scored(price_amount=Decimal("15.00"), shipping_status="known", shipping_amount=Decimal("0.00")).deal
    assert known - scored(price_amount=Decimal("15.00")).deal == 5


def test_no_reference_gives_a_neutral_deal():
    assert scored(reference=None, price_amount=Decimal("1.00"), shipping_status="known", shipping_amount=Decimal("0.00")).deal == 50


def test_a_price_drop_raises_the_deal():
    flat = scored(price_amount=Decimal("15.00"), shipping_status="known", shipping_amount=Decimal("0.00"))
    dropped = scored(previous_price=Decimal("20.00"), price_amount=Decimal("15.00"), shipping_status="known", shipping_amount=Decimal("0.00"))
    assert dropped.deal - flat.deal == 10 and dropped.breakdown["deal"]["price_drop"] == 10


@pytest.mark.parametrize(
    ("market", "target", "maximum", "expected"),
    [
        ([], Decimal("15.00"), Decimal("25.00"), Decimal("15.00")),  # no market: the target
        ([], None, Decimal("25.00"), Decimal("25.00")),  # no target: the maximum
        ([], None, None, None),
        ([Decimal("20.00")] * 5, Decimal("10.00"), None, Decimal("15.00")),  # 5 Matches weigh as much as the target
        ([Decimal("20.00")] * 95, Decimal("10.00"), None, Decimal("19.50")),  # a large market dominates
        ([Decimal("8.00"), Decimal("9.00")], None, None, None),  # too few Matches and nothing to anchor them
        ([Decimal("8.00"), Decimal("9.00"), Decimal("100.00")], None, None, Decimal("9.00")),  # median: outliers do not count
    ],
)
def test_reference_price(market, target, maximum, expected):
    assert reference_price(market, profile(price_target=target, price_max=maximum)) == expected


def test_reasons_carry_total_cost_and_shipping_state():
    lst = listing(price_amount=Decimal("20.00"), fees_amount=Decimal("1.50"))
    reasons = match(lst, profile()).reasons
    assert (reasons["total_cost"], reasons["shipping"]) == ("21.50", "unknown")
    known = listing(price_amount=Decimal("10.00"), shipping_status="known", shipping_amount=Decimal("4.00"), fees_amount=Decimal("1.00"))
    assert match(known, profile()).reasons["total_cost"] == "15.00"


# --- relevance --------------------------------------------------------------------------------

CONSOLES = ["Nintendo ds"]


def examples(kept: list[str], dismissed: list[str], reason: str | None = "other_item") -> list[relevance.Example]:
    taught = relevance.DISMISSED_FOR.get(reason or "", relevance.DISMISSED_WITHOUT_REASON)
    return [relevance.Example(t, *relevance.KEPT) for t in kept] + [relevance.Example(t, *taught) for t in dismissed]


def console_model(reason: str | None = "other_item") -> relevance.RelevanceModel | None:
    kept = ["Nintendo DS Lite nero", "Nintendo DS Lite bianco console", "Console Nintendo DS phat", "Nintendo DS Lite rosa"]
    dismissed = ["Pokemon Platino Nintendo DS gioco", "Mario Kart Nintendo DS gioco", "Gioco Nintendo DS Layton", "Custodia Nintendo DS"]
    return relevance.train(examples(kept, dismissed, reason), CONSOLES)


def test_relevance_learns_what_the_user_keeps():
    model = console_model()
    console = relevance.judge(model, "Nintendo DS Lite blu", CONSOLES)
    game = relevance.judge(model, "Zelda Nintendo DS gioco", CONSOLES)
    assert game.value < relevance.NEUTRAL < console.value
    assert console.words[0][0] == "lite" and console.words[0][1] > 0
    assert game.words[0] == ("gioco", game.words[0][1]) and game.words[0][1] < 0


def test_relevance_is_neutral_on_unknown_words_and_while_learning():
    assert relevance.judge(console_model(), "Nintendo DS", CONSOLES).value == relevance.NEUTRAL
    assert relevance.judge(None, "Nintendo DS gioco", CONSOLES) == relevance.Judgement(relevance.NEUTRAL, [], learning=True)
    too_few = relevance.train(examples(["Nintendo DS Lite"] * 2, ["gioco"] * 9), CONSOLES)
    assert too_few is None


def test_a_dismissal_for_price_or_condition_teaches_the_object_is_right():
    model = console_model(reason="too_expensive")  # the games were the right object, only too dear
    assert relevance.train(examples([], ["x"] * 3, "too_expensive"), CONSOLES) is None  # all relevant: no contrast
    assert relevance.judge(model, "Zelda Nintendo DS gioco", CONSOLES).value >= relevance.NEUTRAL


def test_older_dismissals_without_reason_still_teach_the_wrong_object():
    assert relevance.judge(console_model(None), "Zelda Nintendo DS gioco", CONSOLES).value < relevance.NEUTRAL


@pytest.mark.parametrize(
    ("status", "reason", "taught"),
    [
        ("new", None, None),
        ("seen", None, (True, 1.0)),
        ("pursued", None, (True, 1.0)),
        ("acquired", None, (True, 1.0)),
        ("dismissed", "other_item", (False, 1.0)),
        ("dismissed", "accessory", (False, 1.0)),
        ("dismissed", "region", (False, 1.0)),
        ("dismissed", "too_expensive", (True, 1.0)),
        ("dismissed", "condition", (True, 1.0)),
        ("dismissed", None, (False, 0.5)),
    ],
)
def test_what_each_decision_teaches(status, reason, taught):
    assert relevance.label(status, reason) == taught


def test_profile_keywords_are_not_evidence():
    assert relevance.words("Nintendo DS Lite, 2 giochi!", CONSOLES) == {"lite", "2", "giochi"}


def test_articles_prepositions_and_numbers_weigh_but_are_not_shown_as_reasons():
    model = relevance.RelevanceModel({"per": -1.5, "custodia": -1.0, "2": -0.5})
    judged = model.judge("Custodia per Nintendo DS 2", CONSOLES)
    assert judged.value == round(100 / (1 + math.exp(3.0)))
    assert judged.words == [("custodia", -1.0)]
