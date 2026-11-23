"""Notification outbox in the pipeline, with the Recording Channel (no network)."""
import pytest
from sqlalchemy import select

from tests.support import RecordingChannel
from opportunity_hunter.db.models import AppSetting, MarketplaceQuery, Notification, SearchProfile
from opportunity_hunter.hunter.pipeline import Pipeline
from tests.support import FixtureAdapter
from tests.test_pipeline import T0, clock, fixtures, item, session_factory  # noqa: F401  (fixtures)


@pytest.fixture
def channel():
    return RecordingChannel()


@pytest.fixture
def pipeline(session_factory, tmp_path, clock, channel):
    return Pipeline(session_factory, {"fixture": FixtureAdapter(tmp_path)}, now=clock, channels=[channel], max_attempts=3)


@pytest.fixture
def add_query(session_factory, fixtures):
    def add(pages, *, baseline_done: bool = True, name: str = "RE", keywords=("Resident Evil",)) -> int:
        with session_factory() as session:
            query = MarketplaceQuery(
                search_profile=SearchProfile(name=name, keywords=list(keywords), currency="EUR", score_threshold=0),
                marketplace="fixture", params={"fixture": fixtures(name.lower(), pages)}, scan_interval_minutes=60, max_pages=5,
                baseline_completed_at=T0 if baseline_done else None,
            )
            session.add(query)
            session.commit()
            return query.id

    return add


def notifications(session_factory) -> list[Notification]:
    with session_factory() as session:
        return list(session.scalars(select(Notification).order_by(Notification.id)))


def set_setting(session_factory, key, value):
    with session_factory() as session:
        session.merge(AppSetting(key=key, value=value))
        session.commit()


def test_one_message_per_new_opportunity_sent_after_the_run(pipeline, add_query, channel, session_factory):
    query_id = add_query([{"items": [item("a1", title="Resident Evil 2", location={"city": "Bologna"}, images=["https://x.test/a1.jpg"])]}])

    summary = pipeline.run_query(query_id)
    assert channel.sent == []  # created with the Opportunity, not sent inside the run
    (pending,) = notifications(session_factory)
    assert (pending.status, pending.channel, pending.reason) == ("pending", "recording", "new_opportunity")

    assert pipeline.dispatch() == {"sent": 1, "failed": 0}
    (message,) = channel.sent
    assert (message.title, message.price, message.location, message.score, message.url, message.image_url, message.search_profile) == (
        "Resident Evil 2", "12,00 EUR", "Bologna", message.score, "https://marketplace.test/a1", "https://x.test/a1.jpg", "RE"
    )
    assert 0 <= message.score <= 100 and summary.opportunities == 1
    (sent,) = notifications(session_factory)
    assert (sent.status, sent.attempts, sent.sent_at is not None) == ("sent", 1, True)


def test_repeated_runs_and_dispatches_never_send_twice(pipeline, add_query, channel, fixtures):
    query_id = add_query([{"items": [item("a1")]}])
    pipeline.run_query(query_id)
    pipeline.dispatch()

    fixtures("re", [{"items": [item("a1", title="Resident Evil 2 (ritoccato)")]}])  # changed listing, same Opportunity
    pipeline.run_query(query_id)
    pipeline.run_query(query_id)
    pipeline.dispatch()

    assert len(channel.sent) == 1


def test_listing_matching_two_profiles_gives_one_message_per_opportunity(pipeline, add_query, channel, session_factory):
    query_id = add_query([{"items": [item("a1", title="Resident Evil Biohazard")]}])
    add_query([{"items": []}], name="Bio", keywords=("Biohazard",))
    pipeline.run_query(query_id)
    pipeline.dispatch()
    assert sorted(m.search_profile for m in channel.sent) == ["Bio", "RE"]


def test_failed_send_is_retried_until_the_maximum(pipeline, add_query, channel, session_factory):
    channel.fail_next(5, "telegram down")
    pipeline.run_query(add_query([{"items": [item("a1")]}]))

    assert pipeline.dispatch() == {"sent": 0, "failed": 1}
    (failed,) = notifications(session_factory)
    assert (failed.status, failed.attempts, failed.last_error) == ("failed", 1, "telegram down")

    pipeline.dispatch()
    pipeline.dispatch()  # third and last attempt
    assert pipeline.dispatch() == {"sent": 0, "failed": 0}  # max_attempts reached: no more tries
    assert notifications(session_factory)[0].attempts == 3


def test_failed_send_succeeds_on_a_later_pass(pipeline, add_query, channel, session_factory):
    channel.fail_next(1, "timeout")
    pipeline.run_query(add_query([{"items": [item("a1")]}]))
    pipeline.dispatch()
    assert pipeline.dispatch() == {"sent": 1, "failed": 0}
    assert (notifications(session_factory)[0].status, len(channel.sent)) == ("sent", 1)


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda sf: set_setting(sf, "notifications_paused", True), "paused"),
        (lambda sf: set_setting(sf, "dry_run", True), "dry_run"),
    ],
)
def test_pause_and_dry_run_setting_suppress(pipeline, add_query, channel, session_factory, setup, reason):
    setup(session_factory)
    pipeline.run_query(add_query([{"items": [item("a1")]}]))
    pipeline.dispatch()

    assert channel.sent == []
    (suppressed,) = notifications(session_factory)
    assert (suppressed.status, suppressed.suppressed_reason) == ("suppressed", reason)


def test_dry_run_of_a_single_execution(pipeline, add_query, channel, session_factory):
    summary = pipeline.run_query(add_query([{"items": [item("a1")]}]), dry_run=True)
    pipeline.dispatch()
    assert summary.dry_run is True and channel.sent == []
    assert notifications(session_factory)[0].suppressed_reason == "dry_run"


def test_baseline_run_suppresses_and_later_runs_notify(pipeline, add_query, channel, session_factory, fixtures):
    query_id = add_query([{"items": [item("a1")]}], baseline_done=False)
    pipeline.run_query(query_id)
    pipeline.dispatch()
    assert channel.sent == []
    assert notifications(session_factory)[0].suppressed_reason == "baseline"
    with session_factory() as session:
        assert session.get(MarketplaceQuery, query_id).baseline_completed_at is not None

    fixtures("re", [{"items": [item("a1"), item("a2")]}])
    pipeline.run_query(query_id)
    pipeline.dispatch()
    assert [m.url for m in channel.sent] == ["https://marketplace.test/a2"]


def test_a_failed_or_dry_baseline_does_not_complete_the_baseline(pipeline, add_query, session_factory):
    failing = add_query([{"error": "transient"}], baseline_done=False)
    pipeline.run_query(failing)
    dry = add_query([{"items": [item("a1")]}], baseline_done=False, name="Dry")
    pipeline.run_query(dry, dry_run=True)
    with session_factory() as session:
        assert session.get(MarketplaceQuery, failing).baseline_completed_at is None
        assert session.get(MarketplaceQuery, dry).baseline_completed_at is None


def test_without_channels_no_notifications_are_created(session_factory, tmp_path, clock, add_query):
    bare = Pipeline(session_factory, {"fixture": FixtureAdapter(tmp_path)}, now=clock)
    summary = bare.run_query(add_query([{"items": [item("a1")]}]))
    assert summary.opportunities == 1
    assert notifications(session_factory) == []
    assert bare.dispatch() == {"sent": 0, "failed": 0}


def test_run_due_dispatches_at_the_end(pipeline, add_query, channel):
    add_query([{"items": [item("a1")]}])
    result = pipeline.run_due()
    assert result.dispatched == {"sent": 1, "failed": 0}
    assert len(channel.sent) == 1
