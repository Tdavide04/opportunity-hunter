"""Matching, scoring and Opportunities inside the pipeline (seam run_query, real PostgreSQL)."""
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from opportunity_hunter.db.models import MarketplaceQuery, Match, Opportunity, SearchProfile
from opportunity_hunter.hunter.scoring import SCORING_VERSION
from tests.test_pipeline import clock, fixtures, item, pipeline, session_factory  # noqa: F401  (fixtures)


@pytest.fixture
def add_profile(session_factory):
    def add(name: str, keywords: list[str], *, marketplace: str | None = "fixture", fixture: str = "unused", status: str = "active", **fields) -> tuple[int, int | None]:
        with session_factory() as session:
            profile = SearchProfile(name=name, keywords=keywords, currency="EUR", score_threshold=fields.pop("score_threshold", 0), status=status, **fields)
            session.add(profile)
            query = None
            if marketplace:
                query = MarketplaceQuery(search_profile=profile, marketplace=marketplace, params={"fixture": fixture}, scan_interval_minutes=60, max_pages=5)
                session.add(query)
            session.commit()
            return profile.id, query.id if query else None

    return add


def matches(session_factory) -> list[Match]:
    with session_factory() as session:
        return list(session.scalars(select(Match).order_by(Match.search_profile_id, Match.listing_id)))


def opportunities(session_factory) -> list[Opportunity]:
    with session_factory() as session:
        return list(session.scalars(select(Opportunity).order_by(Opportunity.id)))


def test_listing_is_evaluated_against_every_active_profile_of_the_marketplace(pipeline, fixtures, add_profile, session_factory):
    re_id, query_id = add_profile("RE", ["Resident Evil"], fixture=fixtures("re", [{"items": [item("a1", title="Resident Evil 2")]}]))
    bio_id, _ = add_profile("Bio", ["Resident Evil", "Biohazard"])  # different query, same Marketplace
    silent_id, _ = add_profile("SH", ["Silent Hill"])
    add_profile("Paused", ["Resident Evil"], status="paused")
    add_profile("Vinted only", ["Resident Evil"], marketplace="vinted")
    add_profile("No query", ["Resident Evil"], marketplace=None)

    pipeline.run_query(query_id)

    evaluated = {(m.search_profile_id, m.matched) for m in matches(session_factory)}
    assert evaluated == {(re_id, True), (bio_id, True), (silent_id, False)}
    assert all(m.scoring_version == SCORING_VERSION for m in matches(session_factory))
    assert len(opportunities(session_factory)) == 2


def test_match_and_opportunity_are_not_duplicated_by_later_runs(pipeline, fixtures, add_profile, session_factory):
    _, query_id = add_profile("RE", ["Resident Evil"], fixture=fixtures("re", [{"items": [item("a1")]}]))
    pipeline.run_query(query_id)
    first = matches(session_factory)[0]

    fixtures("re", [{"items": [item("a1", price="9.00")]}])  # changed: re-evaluated
    pipeline.run_query(query_id)
    pipeline.run_query(query_id)  # unchanged: not re-evaluated

    (again,) = matches(session_factory)
    assert again.id == first.id
    assert again.evaluated_at > first.evaluated_at
    assert len(opportunities(session_factory)) == 1


def test_match_stores_reasons_score_and_breakdown(pipeline, fixtures, add_profile, session_factory):
    _, query_id = add_profile(
        "RE", ["Resident Evil"], fixture=fixtures("re", [{"items": [item("a1", title="Resident Evil cover")]}]), excluded_terms=["cover"]
    )
    pipeline.run_query(query_id)

    (m,) = matches(session_factory)
    assert m.matched is False and m.score is None
    assert m.reasons["excluded"] == ["cover"]
    assert opportunities(session_factory) == []


def test_opportunity_needs_the_score_threshold(pipeline, fixtures, add_profile, session_factory):
    page = [{"items": [item("a1", price=None, price_status="missing")]}]  # matched but scored low
    _, query_id = add_profile("strict", ["Resident Evil"], fixture=fixtures("re", page), score_threshold=90)

    pipeline.run_query(query_id)

    (m,) = matches(session_factory)
    assert m.matched is True and m.score < 90
    assert set(m.score_breakdown) == {"price", "freshness", "title", "price_drop"}
    assert opportunities(session_factory) == []


def test_changed_listing_can_become_an_opportunity(pipeline, fixtures, add_profile, session_factory):
    _, query_id = add_profile(
        "RE", ["Resident Evil"], fixture=fixtures("re", [{"items": [item("a1", price="40.00")]}]), price_max=Decimal("25.00")
    )
    pipeline.run_query(query_id)
    assert matches(session_factory)[0].matched is False

    fixtures("re", [{"items": [item("a1", price="20.00")]}])
    pipeline.run_query(query_id)

    assert matches(session_factory)[0].matched is True
    (opportunity,) = opportunities(session_factory)
    assert opportunity.status == "new"


def test_opportunity_status_survives_reevaluation(pipeline, fixtures, add_profile, session_factory):
    _, query_id = add_profile("RE", ["Resident Evil"], fixture=fixtures("re", [{"items": [item("a1")]}]))
    pipeline.run_query(query_id)
    with session_factory() as session:
        session.scalars(select(Opportunity)).one().status = "dismissed"
        session.commit()

    fixtures("re", [{"items": [item("a1", title="Resident Evil 2 nuovo titolo")]}])
    pipeline.run_query(query_id)

    (opportunity,) = opportunities(session_factory)
    assert opportunity.status == "dismissed"
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Opportunity)) == 1


def test_profile_created_later_applies_to_known_listings_at_the_next_run(pipeline, fixtures, add_profile, session_factory):
    _, query_id = add_profile("RE", ["Resident Evil"], fixture=fixtures("re", [{"items": [item("a1", title="Resident Evil Biohazard")]}]))
    pipeline.run_query(query_id)
    late_id, _ = add_profile("Late", ["Biohazard"])

    pipeline.run_query(query_id)  # the Listing itself is unchanged

    assert {(m.search_profile_id, m.matched) for m in matches(session_factory) if m.search_profile_id == late_id} == {(late_id, True)}


def test_edited_profile_is_reapplied_at_the_next_run(pipeline, fixtures, add_profile, session_factory, clock):
    profile_id, query_id = add_profile("RE", ["Resident Evil"], fixture=fixtures("re", [{"items": [item("a1", title="Resident Evil cover")]}]))
    pipeline.run_query(query_id)
    assert matches(session_factory)[0].matched is True

    with session_factory() as session:
        profile = session.get(SearchProfile, profile_id)
        profile.excluded_terms = ["cover"]
        profile.updated_at = clock()  # the API sets this via onupdate; the test clock keeps time ordering explicit
        session.commit()
    pipeline.run_query(query_id)

    (m,) = matches(session_factory)
    assert m.matched is False and m.reasons["excluded"] == ["cover"]
