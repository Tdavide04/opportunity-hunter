"""PostgreSQL integration fixtures for the extracted Hunter engine."""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from opportunity_hunter.db.models import Base


@pytest.fixture
def engine() -> Engine:
    url = os.getenv("OH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OH_TEST_DATABASE_URL not set (PostgreSQL required)")
    value = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(value)
    try:
        yield value
    finally:
        Base.metadata.drop_all(value)
        value.dispose()
