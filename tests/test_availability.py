"""Availability maintenance in the worker, with a fake clock: active -> not seen -> active again. Nothing is deleted."""
from datetime import timedelta

from sqlalchemy import func, select

from opportunity_hunter.db.models import AppSetting, Listing
from tests.test_notifications import add_query, channel, pipeline, set_setting  # noqa: F401  (fixtures)
from tests.test_pipeline import clock, fixtures, item, session_factory  # noqa: F401  (fixtures)


def availability(session_factory) -> dict[str, str]:
    with session_factory() as session:
        return {lst.external_id: lst.availability for lst in session.scalars(select(Listing))}


def test_listing_not_seen_for_n_days_becomes_not_seen_and_comes_back(pipeline, add_query, fixtures, session_factory, clock):
    query_id = add_query([{"items": [item("a1"), item("a2")]}])
    pipeline.run_due()
    assert availability(session_factory) == {"a1": "active", "a2": "active"}

    fixtures("re", [{"items": [item("a2")]}])  # a1 disappears from the results
    clock.current += timedelta(days=8)
    pipeline.run_due()
    assert availability(session_factory) == {"a1": "not_seen", "a2": "active"}

    fixtures("re", [{"items": [item("a1"), item("a2")]}])  # a1 shows up again
    clock.current += timedelta(hours=2)
    with session_factory() as session:
        before = session.scalars(select(Listing).where(Listing.external_id == "a1")).one().last_seen_at
    pipeline.run_query(query_id)
    with session_factory() as session:
        a1 = session.scalars(select(Listing).where(Listing.external_id == "a1")).one()
        assert (a1.availability, a1.last_seen_at > before) == ("active", True)


def test_threshold_is_configurable(pipeline, add_query, fixtures, session_factory, clock):
    set_setting(session_factory, "not_seen_after_days", 2)
    add_query([{"items": [item("a1")]}])
    pipeline.run_due()
    fixtures("re", [{"items": []}])
    clock.current += timedelta(days=3)
    pipeline.run_due()
    assert availability(session_factory) == {"a1": "not_seen"}


def test_maintenance_runs_at_most_once_per_interval(pipeline, add_query, fixtures, session_factory, clock):
    add_query([{"items": [item("a1")]}])
    pipeline.run_due()  # maintenance ran (nothing to mark)
    fixtures("re", [{"items": []}])
    clock.current += timedelta(days=8, minutes=-30)
    with session_factory() as session:  # pretend the last check was 10 minutes ago
        session.merge(AppSetting(key="availability_checked_at", value=(clock.current - timedelta(minutes=10)).isoformat()))
        session.commit()

    pipeline.run_due()
    assert availability(session_factory) == {"a1": "active"}  # skipped: checked less than an hour ago

    clock.current += timedelta(hours=1)
    pipeline.run_due()
    assert availability(session_factory) == {"a1": "not_seen"}


def test_nothing_is_ever_deleted(pipeline, add_query, fixtures, session_factory, clock):
    add_query([{"items": [item("a1"), item("a2"), item("a3")]}])
    pipeline.run_due()
    fixtures("re", [{"items": []}])
    clock.current += timedelta(days=60)
    pipeline.run_due()
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Listing)) == 3
