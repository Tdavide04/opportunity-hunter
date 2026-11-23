"""Alembic migrations for the tables Opportunity Hunter owns (ADR-0013, phase 5).

Two ways in, one history:

- a new database runs ``upgrade``: the ``oh0001`` baseline creates the tables;
- a database that already has them (Personal Collection Hub's) is *adopted* with
  ``adopt_existing``: the live schema is compared with the frozen baseline, built in a
  throw-away schema inside a savepoint, and only an exact match is stamped ``oh0001``.
  No table, row or column is created, changed or dropped.

The version lives in ``oh_alembic_version``, so a host's own ``alembic_version`` is never read
or written. There is no downgrade.
"""

import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, Engine, inspect, text

VERSION_TABLE = "oh_alembic_version"
BASELINE = "oh0001"
OWNED_TABLES = frozenset({
    "search_profile",
    "marketplace_query",
    "acquisition_run",
    "listing",
    "match",
    "opportunity",
    "notification",
    "app_setting",
    "listing_price_history",
})
_HERE = Path(__file__).resolve().parent
_BASELINE_SQL = _HERE / "versions" / "oh0001_baseline.sql"


class MigrationRefused(RuntimeError):
    """The database is not in a state this operation may touch; nothing was changed."""


@dataclass(frozen=True)
class AdoptResult:
    revision: str
    already_adopted: bool
    row_counts: dict[str, int] = field(default_factory=dict)


def alembic_config(connection: Connection | None = None) -> Config:
    config = Config()
    config.set_main_option("script_location", str(_HERE))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def current_revision(connection: Connection) -> str | None:
    """The Opportunity Hunter revision of this database, or None when it was never stamped."""
    context = MigrationContext.configure(connection, opts={"version_table": VERSION_TABLE})
    return context.get_current_revision()


def _present_tables(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names()) & OWNED_TABLES


def upgrade(engine: Engine, revision: str = "head") -> None:
    """Bring a new or already adopted database to ``revision``."""
    with engine.begin() as connection:
        if current_revision(connection) is None and _present_tables(connection):
            raise MigrationRefused(
                "Hunter tables already exist but were never stamped: adopt them with adopt_existing() first"
            )
        command.upgrade(alembic_config(connection), revision)


def row_counts(connection: Connection) -> dict[str, int]:
    return {
        table: connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()
        for table in sorted(_present_tables(connection))
    }


def _fingerprint(connection: Connection, schema: str) -> set[tuple[str, ...]]:
    """Columns, constraints and indexes of the owned tables in ``schema``, with the schema name removed."""
    strip = re.compile(rf'\b"?{re.escape(schema)}"?\.')
    tables = sorted(OWNED_TABLES)
    rows: set[tuple[str, ...]] = set()
    for row in connection.execute(
        text(
            "SELECT table_name, column_name, data_type, udt_name,"
            " coalesce(character_maximum_length::text, ''), coalesce(numeric_precision::text, ''),"
            " coalesce(numeric_scale::text, ''), is_nullable, coalesce(column_default, ''),"
            " is_identity, coalesce(identity_generation, '')"
            " FROM information_schema.columns WHERE table_schema = :schema AND table_name = ANY(:tables)"
        ),
        {"schema": schema, "tables": tables},
    ):
        rows.add(("column", *(strip.sub("", value) for value in row)))
    for row in connection.execute(
        text(
            "SELECT t.relname, c.conname, c.contype::text, pg_get_constraintdef(c.oid)"
            " FROM pg_constraint c JOIN pg_class t ON t.oid = c.conrelid"
            " JOIN pg_namespace n ON n.oid = t.relnamespace"
            " WHERE n.nspname = :schema AND t.relname = ANY(:tables)"
        ),
        {"schema": schema, "tables": tables},
    ):
        rows.add(("constraint", *(strip.sub("", value) for value in row)))
    for row in connection.execute(
        text(
            "SELECT tablename, indexname, indexdef FROM pg_indexes"
            " WHERE schemaname = :schema AND tablename = ANY(:tables)"
        ),
        {"schema": schema, "tables": tables},
    ):
        rows.add(("index", *(strip.sub("", value) for value in row)))
    return rows


def schema_differences(connection: Connection) -> list[str]:
    """How the owned tables in the current schema differ from the frozen baseline (empty: identical).

    The baseline is built in a temporary schema inside a savepoint that is always rolled back.
    """
    live_schema = connection.execute(text("SELECT current_schema()")).scalar_one()
    missing = sorted(OWNED_TABLES - _present_tables(connection))
    if missing:
        return [f"missing table {name}" for name in missing]
    check_schema = f"oh_baseline_check_{uuid.uuid4().hex[:12]}"
    savepoint = connection.begin_nested()
    try:
        connection.exec_driver_sql(f'CREATE SCHEMA "{check_schema}"')
        connection.exec_driver_sql(_BASELINE_SQL.read_text().replace("public.", f'"{check_schema}".'))
        expected = _fingerprint(connection, check_schema)
    finally:
        savepoint.rollback()
    actual = _fingerprint(connection, live_schema)
    return [f"only in the baseline: {item}" for item in sorted(expected - actual)] + [
        f"only in the database: {item}" for item in sorted(actual - expected)
    ]


def adopt_existing(engine: Engine, *, lock_timeout: str = "5s") -> AdoptResult:
    """Stamp an existing database ``oh0001`` after proving its Hunter tables match the baseline.

    Runs in one transaction: verification, stamp and the row-count check either all happen or
    nothing does. Adopting an already adopted database changes nothing.
    """
    if not re.fullmatch(r"\d+(ms|s)", lock_timeout):
        raise ValueError("lock_timeout must look like 5s or 500ms")
    with engine.begin() as connection:
        connection.exec_driver_sql(f"SET LOCAL lock_timeout = '{lock_timeout}'")
        revision = current_revision(connection)
        if revision is not None:
            return AdoptResult(revision=revision, already_adopted=True, row_counts=row_counts(connection))
        differences = schema_differences(connection)
        if differences:
            raise MigrationRefused(
                "the Hunter tables do not match the oh0001 baseline:\n" + "\n".join(differences)
            )
        before = row_counts(connection)
        command.stamp(alembic_config(connection), BASELINE)
        after = row_counts(connection)
        if before != after:
            raise MigrationRefused(f"row counts changed while stamping: {before} -> {after}")
        return AdoptResult(revision=BASELINE, already_adopted=False, row_counts=after)
