# Opportunity Hunter architecture

## Ownership

Opportunity Hunter owns marketplace orchestration, canonical ListingCandidate, source protocol and registry, listing reconciliation, matching, scoring, opportunity state, price history, acquisition runs, notification outbox, circuit breaker and availability maintenance.

PCH remains the owner of user identity, dashboard, central database and modules outside marketplace acquisition. In embedded mode PCH injects its SQLAlchemy Engine/session factory, channels and source adapters. OH does not import PCH.

## Persistence compatibility

The SQLAlchemy mappings retain the existing table and column names from PCH. The metadata is separate from PCH metadata but targets the same PostgreSQL relations.

Constructing OpportunityHunter never creates tables or alters schema. SqlAlchemyStore.initialize_fresh_database() is an explicit helper for a new standalone database and refuses to operate if any user tables already exist. Alembic baseline ownership and production stamping remain separate and are not implemented here.

## Source compatibility

MarketplaceAdapter keeps validate_params(dict) and fetch_page(params, cursor). Scanner-specific models are translated into ListingCandidate by an anti-corruption adapter. Subito Scanner 0.2.0 is supported as an optional extra. Vinted Scanner 0.1.0 still imports pch-marketplace-kit, so it is deliberately not installed or enabled by this package yet.

## Operational invariants

- Worker advisory lock key remains 0x50434857 (PCHW).
- Run states, progress counters, baseline suppression and manual scheduling semantics are preserved.
- Listings, matches, opportunities, price history and outbox entries are committed per page in one transaction.
- Notifications are delivered only after page transactions commit.
- HTTP acquisition details remain owned by scanner packages and are not changed.
