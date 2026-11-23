"""Price history, Price Drop scoring and price_drop Notifications (seam run_query, real PostgreSQL)."""
from decimal import Decimal

import pytest
from sqlalchemy import select

from opportunity_hunter.db.models import ListingPriceHistory, Match, Notification, Opportunity
from tests.test_notifications import add_query, channel, notifications, pipeline, set_setting  # noqa: F401  (fixtures)
from tests.test_pipeline import clock, fixtures, item, session_factory  # noqa: F401  (fixtures)


def history(session_factory) -> list[tuple]:
    with session_factory() as session:
        rows = session.scalars(select(ListingPriceHistory).order_by(ListingPriceHistory.id))
        return [(r.price_amount, r.price_status) for r in rows]


def reasons(session_factory) -> list[str]:
    return [n.reason for n in notifications(session_factory)]


def test_history_grows_only_when_price_or_status_change(pipeline, add_query, fixtures, session_factory):
    query_id = add_query([{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)
    fixtures("re", [{"items": [item("a1", price="20.00", title="Resident Evil 2 (titolo cambiato)")]}])  # updated, same price
    pipeline.run_query(query_id)
    fixtures("re", [{"items": [item("a1", price="15.00")]}])
    pipeline.run_query(query_id)
    fixtures("re", [{"items": [item("a1", price=None, price_status="on_request")]}])
    pipeline.run_query(query_id)

    assert history(session_factory) == [(Decimal("20.00"), "priced"), (Decimal("15.00"), "priced"), (None, "on_request")]


def test_significant_drop_rescores_and_notifies_once_per_amount(pipeline, add_query, fixtures, session_factory, channel):
    query_id = add_query([{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)
    pipeline.dispatch()
    with session_factory() as session:
        before = session.scalars(select(Match)).one()
        assert before.score_breakdown["price_drop"] == 0

    fixtures("re", [{"items": [item("a1", price="15.00")]}])  # -25 %
    pipeline.run_query(query_id)
    pipeline.run_query(query_id)  # same price again: nothing new
    pipeline.dispatch()

    assert reasons(session_factory) == ["new_opportunity", "price_drop:15.00"]
    assert len(channel.sent) == 2 and channel.sent[1].note == "Prezzo sceso da 20,00 a 15,00 EUR"
    with session_factory() as session:
        after = session.scalars(select(Match)).one()
        assert after.score_breakdown["price_drop"] > 0 and after.score > before.score


def test_small_drop_rescores_without_notifying(pipeline, add_query, fixtures, session_factory):
    query_id = add_query([{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)
    fixtures("re", [{"items": [item("a1", price="19.50")]}])  # -2.5 % < 10 % default
    pipeline.run_query(query_id)
    assert reasons(session_factory) == ["new_opportunity"]


def test_threshold_is_configurable(pipeline, add_query, fixtures, session_factory):
    set_setting(session_factory, "price_drop_min_percent", 2)
    query_id = add_query([{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)
    fixtures("re", [{"items": [item("a1", price="19.50")]}])
    pipeline.run_query(query_id)
    assert reasons(session_factory) == ["new_opportunity", "price_drop:19.50"]


@pytest.mark.parametrize(("status", "notified"), [("dismissed", True), ("pursued", True), ("seen", True), ("acquired", False)])
def test_drop_on_an_existing_opportunity_by_status(pipeline, add_query, fixtures, session_factory, status, notified):
    query_id = add_query([{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)
    with session_factory() as session:
        session.scalars(select(Opportunity)).one().status = status
        session.commit()

    fixtures("re", [{"items": [item("a1", price="12.00")]}])
    pipeline.run_query(query_id)

    assert ("price_drop:12.00" in reasons(session_factory)) is notified


def test_drop_that_creates_the_opportunity_only_notifies_it_as_new(pipeline, add_query, fixtures, session_factory):
    from opportunity_hunter.db.models import SearchProfile

    query_id = add_query([{"items": [item("a1", price="40.00")]}])
    with session_factory() as session:
        session.scalars(select(SearchProfile)).one().price_max = Decimal("25.00")
        session.commit()
    pipeline.run_query(query_id)
    assert reasons(session_factory) == []  # above the maximum: no Opportunity

    fixtures("re", [{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)
    assert reasons(session_factory) == ["new_opportunity"]


def test_price_increase_is_not_a_drop(pipeline, add_query, fixtures, session_factory):
    query_id = add_query([{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)
    fixtures("re", [{"items": [item("a1", price="30.00")]}])
    pipeline.run_query(query_id)
    assert reasons(session_factory) == ["new_opportunity"]
    with session_factory() as session:
        assert session.scalars(select(Match)).one().score_breakdown["price_drop"] == 0


def test_drop_in_a_dry_run_is_suppressed(pipeline, add_query, fixtures, session_factory):
    query_id = add_query([{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)
    fixtures("re", [{"items": [item("a1", price="10.00")]}])
    pipeline.run_query(query_id, dry_run=True)
    with session_factory() as session:
        drop = session.scalars(select(Notification).where(Notification.reason == "price_drop:10.00")).one()
        assert (drop.status, drop.suppressed_reason) == ("suppressed", "dry_run")
