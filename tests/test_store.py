import os

import pytest
from sqlalchemy import inspect

from opportunity_hunter.db.models import Base
from opportunity_hunter.db.store import SqlAlchemyStore


def test_metadata_has_only_hunter_tables():
    assert set(Base.metadata.tables) == {
        "search_profile", "marketplace_query", "acquisition_run", "listing",
        "match", "opportunity", "notification", "app_setting", "listing_price_history",
    }


@pytest.mark.skipif(not os.getenv("OH_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set")
def test_fresh_database_bootstrap_is_guarded():
    store = SqlAlchemyStore(database_url=os.environ["OH_TEST_DATABASE_URL"])
    try:
        store.initialize_fresh_database()
        assert set(inspect(store.engine).get_table_names()) == set(Base.metadata.tables)
        with pytest.raises(RuntimeError, match="non-empty"):
            store.initialize_fresh_database()
        Base.metadata.drop_all(store.engine)
    finally:
        store.close()
