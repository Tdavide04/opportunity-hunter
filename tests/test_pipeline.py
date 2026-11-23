"""The Opportunity Hunter pipeline, tested from its seam `run_query` with the Fixture adapter and real PostgreSQL."""
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from opportunity_hunter.db.models import AcquisitionRun, Listing, MarketplaceQuery, SearchProfile
from opportunity_hunter.hunter.pipeline import Pipeline
from tests.support import FixtureAdapter, sqlalchemy_ports

T0 = datetime(2026, 11, 21, 9, 0, tzinfo=UTC)


def item(external_id: str, **overrides):
    return {
        "id": external_id,
        "title": f"Resident Evil {external_id}",
        "url": f"https://marketplace.test/{external_id}",
        "price": "12.00",
        "currency": "EUR",
        "price_status": "priced",
        **overrides,
    }


class Clock:
    def __init__(self):
        self.current = T0

    def __call__(self) -> datetime:
        self.current += timedelta(seconds=1)
        return self.current


@pytest.fixture
def fixtures(tmp_path):
    def write(name: str, pages: list) -> str:
        (tmp_path / f"{name}.json").write_text(json.dumps({"pages": pages}))
        return name

    return write


@pytest.fixture
def session_factory(engine):
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def pipeline(session_factory, tmp_path, clock):
    return Pipeline(*sqlalchemy_ports(session_factory), {"fixture": FixtureAdapter(tmp_path)}, now=clock)


@pytest.fixture
def make_query(session_factory):
    def make(fixture: str, max_pages: int = 5) -> int:
        with session_factory() as session:
            profile = SearchProfile(name="RE", keywords=["Resident Evil"], currency="EUR", score_threshold=0)
            query = MarketplaceQuery(
                search_profile=profile, marketplace="fixture", params={"fixture": fixture}, scan_interval_minutes=60, max_pages=max_pages
            )
            session.add(query)
            session.commit()
            return query.id

    return make


def listings(session_factory) -> list[Listing]:
    with session_factory() as session:
        return list(session.scalars(select(Listing).order_by(Listing.external_id)))


def runs(session_factory) -> list[AcquisitionRun]:
    with session_factory() as session:
        return list(session.scalars(select(AcquisitionRun).order_by(AcquisitionRun.id)))


# --- acquisition and deduplication ------------------------------------------------------------

def test_run_saves_canonical_listings_from_every_page(pipeline, fixtures, make_query, session_factory):
    query_id = make_query(fixtures("re", [{"items": [item("a1"), item("a2")]}, {"items": [item("a3", price=None, price_status="free")]}]))

    summary = pipeline.run_query(query_id)

    assert (summary.status, summary.pages, summary.seen, summary.new, summary.updated, summary.rejected) == ("succeeded", 2, 3, 3, 0, 0)
    saved = listings(session_factory)
    assert [lst.external_id for lst in saved] == ["a1", "a2", "a3"]
    a1 = saved[0]
    assert (a1.marketplace, a1.title, a1.url, a1.price_amount, a1.currency, a1.price_status) == (
        "fixture", "Resident Evil a1", "https://marketplace.test/a1", Decimal("12.00"), "EUR", "priced"
    )
    assert a1.origin == "acquired" and a1.availability == "active"
    assert a1.raw_payload == item("a1")
    assert a1.first_seen_at == a1.last_seen_at and a1.last_seen_run_id == summary.run_id
    assert (saved[2].price_status, saved[2].price_amount) == ("free", None)

    (run,) = runs(session_factory)
    assert (run.status, run.pages, run.listings_seen, run.listings_new) == ("succeeded", 2, 3, 3)
    assert run.started_at < run.finished_at


def test_same_listing_across_pages_runs_and_queries_is_stored_once(pipeline, fixtures, make_query, session_factory):
    first = make_query(fixtures("first", [{"items": [item("a1")]}, {"items": [item("a1"), item("a2")]}]))
    second = make_query(fixtures("second", [{"items": [item("a1")]}]))

    assert pipeline.run_query(first).new == 2
    first_seen = listings(session_factory)[0].first_seen_at
    again = pipeline.run_query(first)
    other = pipeline.run_query(second)

    assert (again.new, again.updated) == (0, 0)
    assert (other.new, other.updated) == (0, 0)
    saved = listings(session_factory)
    assert [lst.external_id for lst in saved] == ["a1", "a2"]
    assert saved[0].first_seen_at == first_seen
    assert saved[0].last_seen_at > first_seen
    assert saved[0].last_seen_run_id == other.run_id


def test_changed_listing_is_updated_with_new_hash_and_payload(pipeline, fixtures, make_query, session_factory, tmp_path):
    query_id = make_query(fixtures("re", [{"items": [item("a1")]}]))
    pipeline.run_query(query_id)
    before = listings(session_factory)[0]

    fixtures("re", [{"items": [item("a1", title="Resident Evil 2 - ribassato", price="9.50")]}])
    summary = pipeline.run_query(query_id)

    assert (summary.new, summary.updated) == (0, 1)
    after = listings(session_factory)[0]
    assert (after.title, after.price_amount) == ("Resident Evil 2 - ribassato", Decimal("9.50"))
    assert after.content_hash != before.content_hash
    assert after.raw_payload["price"] == "9.50"
    assert after.first_seen_at == before.first_seen_at


def test_unchanged_listing_only_moves_last_seen(pipeline, fixtures, make_query, session_factory):
    query_id = make_query(fixtures("re", [{"items": [item("a1")]}]))
    pipeline.run_query(query_id)
    before = listings(session_factory)[0]

    summary = pipeline.run_query(query_id)

    after = listings(session_factory)[0]
    assert (summary.new, summary.updated, summary.seen) == (0, 0, 1)
    assert after.content_hash == before.content_hash
    assert after.last_seen_at > before.last_seen_at
    assert after.last_seen_run_id == summary.run_id


# --- errors -----------------------------------------------------------------------------------

def test_transient_error_after_a_saved_page_gives_a_partial_run(pipeline, fixtures, make_query, session_factory):
    query_id = make_query(fixtures("re", [{"items": [item("a1")]}, {"error": "transient"}, {"items": [item("a2")]}]))

    summary = pipeline.run_query(query_id)

    assert (summary.status, summary.pages, summary.error_kind) == ("partial", 1, "transient")
    assert summary.error_message
    assert [lst.external_id for lst in listings(session_factory)] == ["a1"]
    (run,) = runs(session_factory)
    assert (run.status, run.error_kind, run.error_message) == ("partial", "transient", summary.error_message)


def test_transient_error_on_the_first_page_gives_a_failed_run(pipeline, fixtures, make_query, session_factory):
    query_id = make_query(fixtures("re", [{"error": "transient"}, {"items": [item("a1")]}]))

    summary = pipeline.run_query(query_id)

    assert (summary.status, summary.pages, summary.error_kind) == ("failed", 0, "transient")
    assert listings(session_factory) == []


def test_permanent_error_stops_the_run_immediately(pipeline, fixtures, make_query, session_factory):
    query_id = make_query(fixtures("re", [{"items": [item("a1")]}, {"error": "permanent"}, {"items": [item("a2")]}]))

    summary = pipeline.run_query(query_id)

    assert (summary.status, summary.pages, summary.error_kind) == ("partial", 1, "permanent")
    assert [lst.external_id for lst in listings(session_factory)] == ["a1"]


def test_unexpected_error_is_recorded_and_raised(session_factory, make_query, clock):
    class Broken:
        marketplace = "fixture"
        capabilities = frozenset()

        def validate_params(self, raw):
            return raw

        def fetch_page(self, params, cursor):
            raise RuntimeError("bug in adapter")

    query_id = make_query("anything")
    with pytest.raises(RuntimeError):
        Pipeline(*sqlalchemy_ports(session_factory), {"fixture": Broken()}, now=clock).run_query(query_id)

    (run,) = runs(session_factory)
    assert (run.status, run.error_kind) == ("failed", "internal")
    assert run.finished_at is not None


def test_malformed_candidates_are_rejected_without_stopping_the_run(pipeline, fixtures, make_query, session_factory):
    bad = [item("x1", title=""), item("x2", price=None), item("x3", price="-1.00"), {"title": "no id"}]
    query_id = make_query(fixtures("re", [{"items": [item("a1"), *bad]}, {"items": [item("a2")]}]))

    summary = pipeline.run_query(query_id)

    assert (summary.status, summary.seen, summary.new, summary.rejected) == ("succeeded", 2, 2, 4)
    assert runs(session_factory)[0].rejected == 4


# --- pagination stop --------------------------------------------------------------------------

def test_stops_at_max_pages(pipeline, fixtures, make_query):
    query_id = make_query(fixtures("re", [{"items": [item(f"a{i}")]} for i in range(5)]), max_pages=2)
    assert pipeline.run_query(query_id).pages == 2


def test_stops_at_an_empty_page(pipeline, fixtures, make_query):
    query_id = make_query(fixtures("re", [{"items": [item("a1")]}, {"items": []}, {"items": [item("a2")]}]))
    summary = pipeline.run_query(query_id)
    assert (summary.pages, summary.new) == (2, 1)


def test_stops_when_there_is_no_next_page(pipeline, fixtures, make_query):
    query_id = make_query(fixtures("re", [{"items": [item("a1")]}]), max_pages=5)
    assert pipeline.run_query(query_id).pages == 1


def test_stops_at_a_fully_unchanged_page_only_when_newest_first(pipeline, fixtures, make_query):
    pages = [{"items": [item("a1")]}, {"items": [item("a2")]}, {"items": [item("a3")]}]
    query_id = make_query(fixtures("re", pages))
    pipeline.run_query(query_id)

    fixtures("re", [{"newest_first": True, "items": [item("new")]}, {"newest_first": True, "items": [item("a2")]}, {"newest_first": True, "items": [item("a3")]}])
    assert pipeline.run_query(query_id).pages == 2

    fixtures("re", [{"items": [item("a1")]}, {"items": [item("a2")]}, {"items": [item("late")]}])
    summary = pipeline.run_query(query_id)
    assert (summary.pages, summary.new) == (3, 1)


# --- overlap and integrity --------------------------------------------------------------------

def test_a_query_already_running_is_skipped(pipeline, fixtures, make_query, session_factory):
    query_id = make_query(fixtures("re", [{"items": [item("a1")]}]))
    with session_factory() as session:
        session.add(AcquisitionRun(marketplace_query_id=query_id, status="running", started_at=T0))
        session.commit()

    summary = pipeline.run_query(query_id)

    assert (summary.status, summary.run_id) == ("skipped", None)
    assert len(runs(session_factory)) == 1
    assert listings(session_factory) == []


def test_database_rejects_priced_listing_without_amount(engine):
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO listing (marketplace, external_id, title, url, price_status, origin, first_seen_at, last_seen_at, content_hash, raw_payload)"
            " VALUES ('fixture', 'z', 't', 'https://x.test', 'priced', 'acquired', now(), now(), 'h', '{}')"
        ))
