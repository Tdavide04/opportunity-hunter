"""Circuit breaker per Marketplace with a fake clock: repeated failures suspend it, expiry resumes it."""
from datetime import timedelta

from sqlalchemy import select

from opportunity_hunter.db.models import AcquisitionRun, AppSetting
from opportunity_hunter.hunter.circuit import marketplace_states
from tests.support import sqlalchemy_ports
from tests.test_notifications import add_query, channel, pipeline, set_setting  # noqa: F401  (fixtures)
from tests.test_pipeline import clock, fixtures, item, session_factory  # noqa: F401  (fixtures)


def runs(session_factory) -> int:
    with session_factory() as session:
        return len(session.scalars(select(AcquisitionRun)).all())


def states(session_factory, clock):
    unit_of_work, _ = sqlalchemy_ports(session_factory)
    with unit_of_work() as uow:  # closed at once: an open transaction would block the cleanup TRUNCATE
        return marketplace_states(uow, clock.current)


def pass_after(pipeline, clock, minutes=61):
    clock.current += timedelta(minutes=minutes)
    return pipeline.run_due()


def test_repeated_transient_failures_suspend_the_marketplace_until_expiry(pipeline, add_query, fixtures, session_factory, clock):
    add_query([{"error": "transient"}])
    for _ in range(3):
        pass_after(pipeline, clock)
    (state,) = states(session_factory, clock)
    assert state.marketplace == "fixture" and state.suspended and state.consecutive_failures == 3

    assert pass_after(pipeline, clock, minutes=61 - 60 + 30).summaries == []  # still suspended (60 min)
    assert runs(session_factory) == 3

    fixtures("re", [{"items": [item("a1")]}])  # the Marketplace recovered
    result = pass_after(pipeline, clock, minutes=31)
    assert [s.status for s in result.summaries] == ["succeeded"]
    (state,) = states(session_factory, clock)
    assert not state.suspended and state.consecutive_failures == 0


def test_failing_again_after_expiry_suspends_again_at_once(pipeline, add_query, session_factory, clock):
    add_query([{"error": "transient"}])
    for _ in range(3):
        pass_after(pipeline, clock)
    pass_after(pipeline, clock, minutes=61)  # suspension over: one more try, fails
    assert states(session_factory, clock)[0].suspended


def test_two_permanent_failures_suspend(pipeline, add_query, session_factory, clock):
    add_query([{"error": "permanent"}])
    pass_after(pipeline, clock)
    assert not states(session_factory, clock)[0].suspended
    pass_after(pipeline, clock)
    assert states(session_factory, clock)[0].suspended


def test_a_success_resets_the_count(pipeline, add_query, fixtures, session_factory, clock):
    add_query([{"error": "transient"}])
    pass_after(pipeline, clock)
    pass_after(pipeline, clock)
    fixtures("re", [{"items": [item("a1")]}])
    pass_after(pipeline, clock)
    fixtures("re", [{"error": "transient"}])
    pass_after(pipeline, clock)
    state = states(session_factory, clock)[0]
    assert (state.suspended, state.consecutive_failures) == (False, 1)


def test_thresholds_are_configurable(pipeline, add_query, session_factory, clock):
    set_setting(session_factory, "circuit_failures", 1)
    set_setting(session_factory, "circuit_suspend_minutes", 5)
    add_query([{"error": "transient"}])
    pass_after(pipeline, clock)
    assert states(session_factory, clock)[0].suspended
    clock.current += timedelta(minutes=6)
    assert not states(session_factory, clock)[0].suspended  # the 5-minute suspension is over


def test_manual_scans_wait_while_suspended(pipeline, add_query, session_factory, clock):
    query_id = add_query([{"error": "transient"}])
    for _ in range(3):
        pass_after(pipeline, clock)
    from opportunity_hunter.db.models import MarketplaceQuery

    with session_factory() as session:
        session.get(MarketplaceQuery, query_id).manual_run_requested_at = clock()
        session.commit()
    assert pass_after(pipeline, clock, minutes=1).summaries == []
    with session_factory() as session:
        assert session.get(MarketplaceQuery, query_id).manual_run_requested_at is not None  # still pending


def test_worker_records_each_pass(pipeline, session_factory, clock):
    pipeline.run_due()
    with session_factory() as session:
        assert session.get(AppSetting, "worker_last_pass_at") is not None
