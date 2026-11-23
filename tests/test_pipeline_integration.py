import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from opportunity_hunter.adapter import Page
from opportunity_hunter.db.models import (
    AcquisitionRun, Listing, ListingPriceHistory, MarketplaceQuery, Match, Opportunity, SearchProfile,
)
from opportunity_hunter.domain import ListingCandidate
from opportunity_hunter.hunter.pipeline import Pipeline, WORKER_LOCK_KEY

NOW = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)


class OnePageSource:
    marketplace = "subito"
    capabilities = frozenset({"description", "images", "location", "seller", "condition", "published_at"})

    def validate_params(self, raw):
        return dict(raw)

    def fetch_page(self, params, cursor):
        if cursor is not None:
            raise AssertionError("unexpected second page")
        candidate = ListingCandidate(
            external_id="90001",
            title="Resident Evil 2 GameCube",
            url="https://www.subito.it/videogiochi/resident-evil-2-90001.htm",
            price_status="priced",
            price_amount=Decimal("25.00"),
            currency="EUR",
            description="Complete edition",
            images=["https://images.example.test/item.jpg"],
            location={"region": "Lombardia", "city": "Milano"},
            published_at=NOW - timedelta(hours=1),
            shipping_status="known",
            shipping_amount=Decimal("2.99"),
            raw_payload={"id": "90001"},
        )
        return Page(candidates=[candidate], next_cursor=None, newest_first=True)


@pytest.mark.skipif(not os.getenv("OH_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set")
def test_pipeline_runs_against_the_extracted_orm_and_existing_table_contract():
    engine = create_engine(os.environ["OH_TEST_DATABASE_URL"], pool_pre_ping=True)
    created_schema = False
    try:
        # CI supplies a fresh dedicated database. Never truncate or drop unknown user tables.
        from sqlalchemy import inspect
        existing = set(inspect(engine).get_table_names())
        if existing:
            pytest.skip(f"database is not empty; refusing destructive setup: {sorted(existing)}")

        from opportunity_hunter.db.models import Base
        Base.metadata.create_all(engine)
        created_schema = True
        sessions = sessionmaker(engine, expire_on_commit=False)

        with sessions() as session, session.begin():
            profile = SearchProfile(
                name="Resident Evil",
                keywords=["resident evil"],
                title_only=False,
                excluded_terms=[],
                currency="EUR",
                price_target=Decimal("30.00"),
                price_max=Decimal("50.00"),
                score_threshold=0,
                status="active",
            )
            query = MarketplaceQuery(
                search_profile=profile,
                marketplace="subito",
                params={"search_text": "resident evil"},
                scan_interval_minutes=60,
                max_pages=1,
                baseline_completed_at=NOW - timedelta(days=1),
            )
            session.add(query)

        with sessions() as session:
            query_id = session.scalar(select(MarketplaceQuery.id))

        def clock() -> datetime:
            return NOW

        pipeline = Pipeline(sessions, {"subito": OnePageSource()}, now=clock, channels=[])
        summary = pipeline.run_query(query_id)

        assert (summary.status, summary.pages, summary.seen, summary.new, summary.updated) == ("succeeded", 1, 1, 1, 0)
        assert summary.opportunities == 1
        with sessions() as session:
            listing = session.scalar(select(Listing).where(Listing.external_id == "90001"))
            assert listing is not None
            assert (listing.marketplace, listing.price_amount, listing.shipping_amount) == ("subito", Decimal("25.00"), Decimal("2.99"))
            assert session.scalar(select(Match).where(Match.listing_id == listing.id)) is not None
            assert session.scalar(select(Opportunity)) is not None
            assert session.scalar(select(ListingPriceHistory).where(ListingPriceHistory.listing_id == listing.id)) is not None
            run = session.scalar(select(AcquisitionRun))
            assert run is not None and run.status == "succeeded"
        assert WORKER_LOCK_KEY == 0x50434857
    finally:
        # Drop only the exact OH-owned tables from this fresh, isolated test database.
        if created_schema:
            from opportunity_hunter.db.models import Base
            Base.metadata.drop_all(engine)
        engine.dispose()
