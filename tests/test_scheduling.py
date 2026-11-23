"""Worker scheduling: run_due with a fake clock, manual scans, the advisory lock and abandoned runs."""
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from opportunity_hunter.db.models import AcquisitionRun, MarketplaceQuery, SearchProfile
from opportunity_hunter.hunter.pipeline import WORKER_LOCK_KEY
from tests.test_pipeline import T0, clock, fixtures, item, pipeline, session_factory  # noqa: F401  (fixtures)


@pytest.fixture
def add_query(session_factory, fixtures):
    fixtures("re", [{"items": [item("a1")]}])

    def add(*, interval: int = 60, status: str = "active", profile_status: str = "active", last_started_at=None, manual=None) -> int:
        with session_factory() as session:
            query = MarketplaceQuery(
                search_profile=SearchProfile(name="RE", keywords=["Resident Evil"], currency="EUR", score_threshold=0, status=profile_status),
                marketplace="fixture", params={"fixture": "re"}, scan_interval_minutes=interval, max_pages=3, status=status,
                last_started_at=last_started_at, manual_run_requested_at=manual,
            )
            session.add(query)
            session.commit()
            return query.id

    return add


def ran(summaries) -> list[int]:
    return sorted(s.marketplace_query_id for s in summaries)


def test_due_queries_run_and_others_wait(pipeline, add_query, clock):
    never = add_query()
    overdue = add_query(interval=30, last_started_at=T0 - timedelta(minutes=31))
    recent = add_query(interval=60, last_started_at=T0 - timedelta(minutes=10))
    paused = add_query(status="paused")
    paused_profile = add_query(profile_status="paused")
    archived_profile = add_query(profile_status="archived")

    result = pipeline.run_due()

    assert result.locked is True
    assert ran(result.summaries) == sorted([never, overdue])
    assert {never, overdue}.isdisjoint({recent, paused, paused_profile, archived_profile}) and recent


def test_run_records_last_start_so_the_query_waits_its_interval(pipeline, add_query, session_factory, clock):
    query_id = add_query(interval=60)
    assert ran(pipeline.run_due().summaries) == [query_id]
    with session_factory() as session:
        assert session.get(MarketplaceQuery, query_id).last_started_at is not None

    assert pipeline.run_due().summaries == []  # just ran
    clock.current += timedelta(minutes=61)
    assert ran(pipeline.run_due().summaries) == [query_id]


def test_manual_scan_runs_even_if_not_due_and_is_cleared(pipeline, add_query, session_factory):
    query_id = add_query(interval=60, last_started_at=T0, manual=T0)

    assert ran(pipeline.run_due().summaries) == [query_id]
    with session_factory() as session:
        assert session.get(MarketplaceQuery, query_id).manual_run_requested_at is None
    assert pipeline.run_due().summaries == []


def test_manual_scan_of_a_paused_query_runs_once(pipeline, add_query):
    query_id = add_query(status="paused", manual=T0)
    assert ran(pipeline.run_due().summaries) == [query_id]
    assert pipeline.run_due().summaries == []


def test_manual_scan_of_an_archived_profile_is_ignored(pipeline, add_query):
    add_query(profile_status="archived", manual=T0)
    assert pipeline.run_due().summaries == []


def test_a_second_worker_exits_immediately_while_the_lock_is_held(pipeline, add_query, engine, caplog):
    add_query()
    with engine.connect() as other_worker:
        assert other_worker.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": WORKER_LOCK_KEY}).scalar() is True
        with caplog.at_level("INFO", logger="opportunity_hunter.hunter.pipeline"):
            result = pipeline.run_due()
        other_worker.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": WORKER_LOCK_KEY})

    assert (result.locked, result.summaries) == (False, [])
    assert any(r.event == "worker_busy" for r in caplog.records)
    assert pipeline.run_due().locked is True  # released: the next worker runs


def test_one_failing_query_does_not_stop_the_others(pipeline, add_query, session_factory, fixtures):
    broken = add_query()
    with session_factory() as session:
        session.get(MarketplaceQuery, broken).params = {"fixture": "missing_file"}  # permanent error: run failed
        session.commit()
    fine = add_query()

    summaries = {s.marketplace_query_id: s.status for s in pipeline.run_due().summaries}
    assert summaries == {broken: "failed", fine: "succeeded"}


def test_runs_left_running_by_a_dead_worker_are_closed_as_abandoned(pipeline, add_query, session_factory):
    query_id = add_query()
    with session_factory() as session:
        session.add(AcquisitionRun(marketplace_query_id=query_id, status="running", started_at=T0 - timedelta(hours=3)))
        session.commit()

    result = pipeline.run_due()

    assert [s.status for s in result.summaries] == ["succeeded"]  # no longer blocked
    with session_factory() as session:
        abandoned = session.scalars(select(AcquisitionRun).order_by(AcquisitionRun.id)).first()
        assert (abandoned.status, abandoned.error_kind) == ("failed", "internal")
        assert "abandoned" in abandoned.error_message and abandoned.finished_at is not None
