# Phase 4 extraction scope

Included: canonical domain and source port, ORM mappings for the existing Hunter tables, the persistence ports and Unit of Work of ADR-0013 with their single SQLAlchemy adapter, SQLAlchemy store and Engine injection, pipeline, scheduling lock, circuit breaker, matching, scoring, listing reconciliation, price history, availability maintenance, notification outbox, Telegram channel, standalone CLI and CI.

Not included: PCH-supplied implementations of SettingsStore or channels (the SQLAlchemy adapter already works on PCH's tables), application services for profile/opportunity management used by PCH's API, PCH API route migration, PCH worker rewiring, PCH dependency removal, Alembic migration generation or ownership, Alembic stamp, schema changes, production database access, Vinted Scanner 0.2 standalone support, PyPI publication, release tags, or archive/delete operations.

PCH continues using its existing pch.hunter implementation and pinned dependencies until the later integration phase. This keeps current production execution unchanged while the extracted implementation is reviewed.
