"""Persistence ports (ADR-0013): services depend on them only, and the SQLAlchemy adapter honours each contract."""
import ast
import inspect as pyinspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import sessionmaker

import opportunity_hunter.hunter as hunter_package
from opportunity_hunter.config import OpportunityHunterConfig
from opportunity_hunter.db.models import AcquisitionRun, AppSetting, Base, Listing, MarketplaceQuery, Match, Notification, SearchProfile
from opportunity_hunter.db.store import SqlAlchemyStore
from opportunity_hunter.engine import OpportunityHunter
from opportunity_hunter.hunter.pipeline import Pipeline
from opportunity_hunter.ports import WORKER_LOCK_KEY
from tests.support import FixtureAdapter
from tests.test_pipeline import fixtures, item  # noqa: F401  (fixtures)

T0 = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)


# --- architecture -----------------------------------------------------------------------------

def _imported_modules(path: Path) -> set[str]:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("module", sorted(p.name for p in Path(hunter_package.__file__).parent.glob("*.py")))
def test_services_and_pipeline_do_not_import_sqlalchemy(module):
    imported = _imported_modules(Path(hunter_package.__file__).parent / module)
    assert not {name for name in imported if name.split(".")[0] == "sqlalchemy"}
    assert "opportunity_hunter.db.repositories" not in imported and "opportunity_hunter.db.store" not in imported


def test_pipeline_is_built_from_ports_not_from_a_session_factory():
    parameters = list(pyinspect.signature(Pipeline).parameters)
    assert parameters[:2] == ["unit_of_work", "worker_lock"]
    assert "session_factory" not in parameters


def test_worker_lock_key_is_unchanged():
    assert WORKER_LOCK_KEY == 0x50434857


# --- SQLAlchemy adapter contracts (PostgreSQL) ------------------------------------------------

@pytest.fixture
def store(engine):
    return SqlAlchemyStore(session_factory=sessionmaker(engine, expire_on_commit=False))


@pytest.fixture
def query_id(store):
    with store.unit_of_work() as uow:
        uow.session.add(MarketplaceQuery(
            search_profile=SearchProfile(name="RE", keywords=["resident evil"], currency="EUR", score_threshold=0),
            marketplace="fixture", params={"fixture": "x"}, scan_interval_minutes=60, max_pages=1,
        ))
        uow.commit()
    with store.unit_of_work() as uow:
        value = uow.session.scalar(select(MarketplaceQuery.id))
    assert value is not None
    return value


def _listing(external_id: str, **overrides) -> Listing:
    values = {"marketplace": "fixture", "external_id": external_id, "title": external_id, "url": f"https://x.test/{external_id}",
              "price_status": "priced", "price_amount": Decimal("10.00"), "currency": "EUR", "first_seen_at": T0, "last_seen_at": T0}
    return Listing(**{**values, **overrides})


def test_unit_of_work_commits_only_when_asked(store):
    with store.unit_of_work() as uow:
        uow.settings.put("a", 1)
    with store.unit_of_work() as uow:
        assert uow.settings.get("a") is None
        uow.settings.put("a", 1)
        uow.commit()
    with store.unit_of_work() as uow:
        assert uow.settings.get("a") == 1


def test_unit_of_work_rolls_back_on_error_and_cannot_be_reused_while_active(store):
    with pytest.raises(LookupError), store.unit_of_work() as uow:
        uow.settings.put("a", 1)
        uow.flush()
        raise LookupError("service failure")
    with store.unit_of_work() as uow:
        assert uow.settings.get("a") is None
        with pytest.raises(RuntimeError, match="already active"):
            uow.__enter__()
    with pytest.raises(RuntimeError, match="not active"):
        uow.flush()


def test_settings_store(store):
    with store.unit_of_work() as uow:
        uow.settings.put("x", {"k": [1, 2]})
        uow.settings.put("y", True)
        uow.settings.put("y", False)  # overwrite
        uow.settings.delete("missing")  # no-op
        uow.commit()
    with store.unit_of_work() as uow:
        assert uow.settings.get_many(["x", "y", "z"]) == {"x": {"k": [1, 2]}, "y": False}
        uow.settings.delete("x")
        uow.commit()
    with store.unit_of_work() as uow:
        assert uow.settings.get("x") is None
        assert uow.session.scalar(select(AppSetting.value).where(AppSetting.key == "y")) is False


def test_run_start_records_once_per_query_and_leaves_the_transaction_usable(store, query_id):
    with store.unit_of_work() as uow:
        query = uow.profiles.get_query(query_id)
        run = uow.runs.start(query, T0, dry_run=False)
        uow.commit()
    assert run is not None and run.id is not None
    with store.unit_of_work() as uow:
        query = uow.profiles.get_query(query_id)
        assert uow.runs.start(query, T0 + timedelta(minutes=5), dry_run=False) is None  # already running
        uow.settings.put("after_skip", 1)  # the outer transaction still works
        uow.commit()
    with store.unit_of_work() as uow:
        query = uow.profiles.get_query(query_id)
        assert query.last_started_at == T0  # the skipped start changed nothing
        assert uow.settings.get("after_skip") == 1
        assert uow.runs.close_abandoned(T0 + timedelta(hours=1)) == 1
        uow.commit()
    with store.unit_of_work() as uow:
        (finished,) = uow.runs.recent_finished("fixture", 20)
        assert (finished.status, finished.error_kind) == ("failed", "internal")
        assert uow.runs.recent_finished("subito", 20) == []


def test_recent_finished_runs_are_newest_first_and_exclude_running(store, query_id):
    with store.unit_of_work() as uow:
        for minutes, status in ((0, "failed"), (10, "succeeded"), (20, "running")):
            uow.session.add(AcquisitionRun(marketplace_query_id=query_id, status=status, started_at=T0 + timedelta(minutes=minutes)))
        uow.commit()
    with store.unit_of_work() as uow:
        assert [r.status for r in uow.runs.recent_finished("fixture", 20)] == ["succeeded", "failed"]
        assert len(uow.runs.recent_finished("fixture", 1)) == 1


def test_profiles_and_schedule_candidates(store, query_id):
    with store.unit_of_work() as uow:
        archived = SearchProfile(name="old", keywords=["x"], currency="EUR", score_threshold=0, status="archived")
        uow.session.add(MarketplaceQuery(search_profile=archived, marketplace="subito", params={}, scan_interval_minutes=60, max_pages=1))
        uow.commit()
    with store.unit_of_work() as uow:
        assert [p.name for p in uow.profiles.active_profiles_for("fixture")] == ["RE"]
        assert uow.profiles.active_profiles_for("subito") == []
        assert uow.profiles.marketplaces() == ["fixture", "subito"]
        (row,) = uow.profiles.schedule_candidates()  # the archived profile's query is not a candidate
        assert row == (query_id, "fixture", 60, None, None, "active", "active")


def test_listings_lock_and_availability(store):
    with store.unit_of_work() as uow:
        uow.listings.add(_listing("old", last_seen_at=T0 - timedelta(days=10)))
        uow.listings.add(_listing("fresh"))
        uow.listings.add(_listing("legacy", origin="legacy_seen", last_seen_at=T0 - timedelta(days=10)))
        uow.commit()
    with store.unit_of_work() as uow:
        assert set(uow.listings.lock_known("fixture", ["old", "fresh", "unknown"])) == {"old", "fresh"}
        assert uow.listings.lock_known("subito", ["old"]) == {}
        assert uow.listings.mark_not_seen(T0 - timedelta(days=7)) == 1  # only acquired Listings
        uow.commit()
    with store.unit_of_work() as uow:
        states = dict(uow.session.execute(select(Listing.external_id, Listing.availability)).all())
        assert states == {"old": "not_seen", "fresh": "active", "legacy": "active"}


def test_price_history(store):
    with store.unit_of_work() as uow:
        listing = _listing("a")
        uow.listings.add(listing)
        for minutes, amount in ((0, "20.00"), (1, "15.00"), (2, "12.00")):
            uow.price_history.record(listing, "priced", Decimal(amount), "EUR", T0 + timedelta(minutes=minutes))
        uow.price_history.record(listing, "on_request", None, None, T0 + timedelta(minutes=3))
        uow.flush()
        assert uow.price_history.latest_two([listing.id]) == {listing.id: [(None, None), (Decimal("12.00"), "EUR")]}
        assert uow.price_history.amounts(listing.id) == [Decimal("20.00"), Decimal("15.00"), Decimal("12.00"), None]
        assert uow.price_history.latest_two([]) == {}


def test_matches_opportunities_and_outbox(store, query_id):
    with store.unit_of_work() as uow:
        listing = _listing("a")
        uow.listings.add(listing)
        uow.flush()
        (profile,) = uow.profiles.active_profiles_for("fixture")
        match = Match(search_profile_id=profile.id, listing_id=listing.id, matched=True, reasons={}, score=80, scoring_version=3, evaluated_at=T0)
        uow.matches.add(match)
        opportunity = uow.opportunities.create(match, T0)
        uow.outbox.add(opportunity, "telegram", "new_opportunity", "pending", None, T0)
        uow.outbox.add(opportunity, "telegram", "baseline_copy", "suppressed", "baseline", T0)
        uow.flush()
        row = {"opportunity_id": opportunity.id, "channel": "telegram", "reason": "price_drop:8.00", "status": "pending",
               "suppressed_reason": None, "created_at": T0, "attempts": 0}
        uow.outbox.add_ignoring_duplicates([row])
        uow.outbox.add_ignoring_duplicates([row])  # same (opportunity, channel, reason): skipped, not an error
        uow.outbox.add_ignoring_duplicates([])
        uow.commit()
        listing_id, profile_id = listing.id, profile.id
    with store.unit_of_work() as uow:
        (found,) = uow.matches.for_pairs([listing_id], [profile_id])
        assert found.opportunity is not None and found.opportunity.status == "new"
        assert uow.matches.for_pairs([listing_id], [profile_id + 1]) == []
        ids = uow.outbox.deliverable_ids(["telegram"], max_attempts=3)
        assert len(ids) == 2  # the suppressed one is never delivered
        assert uow.outbox.deliverable_ids(["other"], max_attempts=3) == []
        claimed = uow.outbox.claim(ids[0])
        assert claimed is not None and claimed.opportunity.match.listing.external_id == "a"
        with store.unit_of_work() as other:
            assert other.outbox.claim(ids[0]) is None  # locked by the first worker: skipped
            assert other.outbox.claim(ids[1]) is not None
    with store.unit_of_work() as uow:
        failed = uow.session.get(Notification, ids[0])
        failed.status, failed.attempts = "failed", 3
        uow.commit()
    with store.unit_of_work() as uow:
        assert uow.outbox.deliverable_ids(["telegram"], max_attempts=3) == [ids[1]]
        assert uow.outbox.deliverable_ids(["telegram"], max_attempts=4) == ids


def test_worker_lock_is_exclusive(store):
    with store.worker_lock() as first:
        assert first is True
        with store.worker_lock() as second:
            assert second is False
    with store.worker_lock() as again:
        assert again is True


# --- embedded mode ----------------------------------------------------------------------------

def test_embedded_mode_uses_the_host_session_factory_and_tables(engine, tmp_path, fixtures):  # noqa: F811
    host_sessions = sessionmaker(engine, expire_on_commit=False)  # what PCH hands over
    tables_before = set(inspect(engine).get_table_names())
    with host_sessions() as session:
        query = MarketplaceQuery(
            search_profile=SearchProfile(name="RE", keywords=["Resident Evil"], currency="EUR", score_threshold=0),
            marketplace="fixture", params={"fixture": fixtures("embedded", [{"items": [item("e1")]}])}, scan_interval_minutes=60, max_pages=1,
        )
        session.add(query)
        session.commit()
        query_id = query.id

    hunter = OpportunityHunter(config=OpportunityHunterConfig(), store=host_sessions, channels=[], sources={"fixture": FixtureAdapter(tmp_path)})
    assert hunter.store.session_factory is host_sessions
    summary = hunter.run_query(query_id)
    hunter.close()  # must not dispose the host's Engine

    assert (summary.status, summary.new) == ("succeeded", 1)
    assert set(inspect(engine).get_table_names()) == tables_before == set(Base.metadata.tables)  # no parallel tables
    with host_sessions() as session:  # the host sees the rows in its own database, Engine still usable
        assert session.scalars(select(Listing.external_id)).all() == ["e1"]
