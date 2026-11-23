"""Alembic environment for the tables Opportunity Hunter owns.

Runs inside a host's database too (PCH): the version table is ``oh_alembic_version`` and
autogenerate only looks at Opportunity Hunter's tables, so the host's own tables and
``alembic_version`` are never touched.
"""

import os

from alembic import context
from sqlalchemy import create_engine, pool

import opportunity_hunter.db.models  # noqa: F401  (registers the models on Base.metadata)
from opportunity_hunter.db import Base
from opportunity_hunter.migrations import OWNED_TABLES, VERSION_TABLE

config = context.config
target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    if type_ == "table":
        return name in OWNED_TABLES
    table = getattr(obj, "table", None)
    return table is None or table.name in OWNED_TABLES


def _configure(**kwargs) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        include_object=include_object,
        compare_type=True,
        **kwargs,
    )


connection = config.attributes.get("connection")
if connection is not None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
else:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("OH_DATABASE_URL")
    if not url:
        raise SystemExit("OH_DATABASE_URL is not set")
    if context.is_offline_mode():
        _configure(url=url, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        with create_engine(url, poolclass=pool.NullPool).connect() as conn:
            _configure(connection=conn)
            with context.begin_transaction():
                context.run_migrations()
