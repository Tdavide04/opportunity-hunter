# Opportunity Hunter architecture

## Ownership

Opportunity Hunter owns marketplace orchestration, canonical ListingCandidate, source protocol and registry, listing reconciliation, matching, scoring, opportunity state, price history, acquisition runs, notification outbox, circuit breaker and availability maintenance.

PCH remains the owner of user identity, dashboard, central database and modules outside marketplace acquisition. In embedded mode PCH injects its SQLAlchemy Engine/session factory, channels and source adapters. OH does not import PCH.

## Persistence ports

`opportunity_hunter.ports` defines the ports named by ADR-0013: `SearchProfileRepository`, `RunRepository`, `ListingRepository`, `PriceHistoryRepository`, `MatchRepository`, `OpportunityRepository`, `NotificationOutbox`, `SettingsStore`, `WorkerLock` and `UnitOfWork`. The pipeline and the services in `opportunity_hunter.hunter` depend only on these protocols (a test enforces that they import no SQLAlchemy). `Pipeline` is built from a Unit of Work factory and a worker lock.

`opportunity_hunter.db.repositories` is the single SQLAlchemy adapter. It keeps the queries, row locks (`FOR UPDATE`, `SKIP LOCKED`), ordering and `ON CONFLICT` behaviour that the pipeline used directly before. `SqlAlchemyStore.unit_of_work()` and `SqlAlchemyStore.worker_lock` hand the ports out, on OH's own Engine (standalone) or on the host's Engine/session factory (embedded). A Unit of Work is one transaction: leaving it without `commit()` rolls back.

The ports exchange the mapped entity classes of `opportunity_hunter.db.models`; changes a service makes to those entities are written when the Unit of Work commits. Pure domain entities would be a later, separate step.

## Persistence compatibility

The SQLAlchemy mappings retain the existing table and column names from PCH. The metadata is separate from PCH metadata but targets the same PostgreSQL relations.

Constructing OpportunityHunter never creates tables or alters schema. SqlAlchemyStore.initialize_fresh_database() is an explicit helper for a new standalone database and refuses to operate if any user tables already exist. Alembic baseline ownership and production stamping remain separate and are not implemented here.

## Source compatibility

MarketplaceAdapter keeps validate_params(dict) and fetch_page(params, cursor). Scanner-specific models are translated into ListingCandidate by an anti-corruption adapter. Subito Scanner 0.2.0 and Vinted Scanner 0.2.0 are optional extras; `opportunity_hunter.sources.common` holds the shared translation (listings, rejected items, cursors and the scanners' errors), and the Vinted adapter also passes through the scanner's filter discovery. Vinted Scanner is GPL-3.0-only (see the README).

## Operational invariants

- Worker advisory lock key remains 0x50434857 (PCHW).
- Run states, progress counters, baseline suppression and manual scheduling semantics are preserved.
- Listings, matches, opportunities, price history and outbox entries are committed per page in one transaction.
- Notifications are delivered only after page transactions commit.
- HTTP acquisition details remain owned by scanner packages and are not changed.
