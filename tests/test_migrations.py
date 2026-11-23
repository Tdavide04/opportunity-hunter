"""Phase 5: Alembic ownership of the Hunter tables (new databases and adoption of existing ones)."""

import os
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

from opportunity_hunter import migrations
from opportunity_hunter.db.models import Base


def _drop_everything(engine: Engine) -> None:
    with engine.begin() as connection:
        for table in inspect(connection).get_table_names():
            connection.exec_driver_sql(f'DROP TABLE IF EXISTS "{table}" CASCADE')


@pytest.fixture
def empty_engine() -> Iterator[Engine]:
    url = os.getenv("OH_TEST_DATABASE_URL")
    if not url:
        pytest.skip("OH_TEST_DATABASE_URL not set (PostgreSQL required)")
    engine = create_engine(url)
    _drop_everything(engine)
    try:
        yield engine
    finally:
        _drop_everything(engine)
        engine.dispose()


def _seed(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO app_setting (key, value) VALUES ('paused', 'false')"))
        connection.execute(text("INSERT INTO app_setting (key, value) VALUES ('dry_run', 'true')"))


def test_new_database_gets_the_baseline(empty_engine: Engine) -> None:
    migrations.upgrade(empty_engine)
    with empty_engine.connect() as connection:
        assert migrations.current_revision(connection) == migrations.BASELINE
        assert migrations.OWNED_TABLES <= set(inspect(connection).get_table_names())
        assert migrations.schema_differences(connection) == []


def test_models_match_the_migrations(empty_engine: Engine) -> None:
    """A model change without a new migration fails here."""
    migrations.upgrade(empty_engine)
    with empty_engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "include_object": lambda obj, name, type_, *_: name != migrations.VERSION_TABLE},
        )
        assert compare_metadata(context, Base.metadata) == []


def test_existing_tables_are_adopted_without_touching_rows(empty_engine: Engine) -> None:
    Base.metadata.create_all(empty_engine)
    _seed(empty_engine)

    result = migrations.adopt_existing(empty_engine)

    assert result.revision == migrations.BASELINE
    assert not result.already_adopted
    assert result.row_counts["app_setting"] == 2
    assert result.row_counts["search_profile"] == 0
    with empty_engine.connect() as connection:
        assert migrations.current_revision(connection) == migrations.BASELINE
        assert not [name for name in connection.execute(text("SELECT nspname FROM pg_namespace")).scalars() if name.startswith("oh_baseline_check_")]
    again = migrations.adopt_existing(empty_engine)
    assert again.already_adopted
    assert again.row_counts == result.row_counts
    migrations.upgrade(empty_engine)  # adopted databases upgrade normally


def test_the_hosts_alembic_version_is_left_alone(empty_engine: Engine) -> None:
    Base.metadata.create_all(empty_engine)
    with empty_engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num varchar(32) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0010')")

    migrations.adopt_existing(empty_engine)

    with empty_engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0010"


def test_adoption_refuses_a_schema_that_differs(empty_engine: Engine) -> None:
    Base.metadata.create_all(empty_engine)
    with empty_engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE listing ADD COLUMN surprise text")

    with pytest.raises(migrations.MigrationRefused, match="surprise"):
        migrations.adopt_existing(empty_engine)

    with empty_engine.connect() as connection:
        assert migrations.current_revision(connection) is None


def test_adoption_refuses_missing_tables(empty_engine: Engine) -> None:
    Base.metadata.create_all(empty_engine)
    with empty_engine.begin() as connection:
        connection.exec_driver_sql("DROP TABLE listing_price_history")

    with pytest.raises(migrations.MigrationRefused, match="missing table listing_price_history"):
        migrations.adopt_existing(empty_engine)


def test_upgrade_refuses_unadopted_tables(empty_engine: Engine) -> None:
    Base.metadata.create_all(empty_engine)

    with pytest.raises(migrations.MigrationRefused, match="adopt"):
        migrations.upgrade(empty_engine)


def test_the_baseline_has_no_downgrade(empty_engine: Engine) -> None:
    migrations.upgrade(empty_engine)
    with empty_engine.begin() as connection, pytest.raises(RuntimeError, match="no downgrade"):
        command.downgrade(migrations.alembic_config(connection), "base")
