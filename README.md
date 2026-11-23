# Opportunity Hunter

Standalone and embeddable marketplace opportunity engine.

This repository contains the marketplace engine extracted from Personal Collection Hub (PCH): canonical listing types, source protocol, orchestration, matching, scoring, deduplication, price history, acquisition runs, notification outbox, circuit breaking and availability maintenance.

## External dependencies

Opportunity Hunter points to these repositories. Each one is an independent module with its own repository, version and licence; the full list, with the pinned commits, is in [`external/`](external/README.md).

| Module | Role | Licence | Extra |
|---|---|---|---|
| [subito-scanner](https://github.com/Tdavide04/subito-scanner) | Subito.it search | MIT | `subito` |
| [vinted-scanner](https://github.com/Tdavide04/vinted-scanner) | Vinted search | GPL-3.0-only | `vinted` |
| [ebay-scanner](https://github.com/Tdavide04/ebay-scanner) | eBay search | — | planned |

    subito-scanner ───────────┐
    vinted-scanner ───────────┼──> opportunity-hunter ──> personal-collection-hub
    ebay-scanner (planned) ───┘

Arrows point from the module to whoever uses it, never back.

## Architecture

- Standalone: OpportunityHunter owns or receives a PostgreSQL-backed SqlAlchemyStore.
- Embedded: PCH passes its existing SQLAlchemy Engine or session factory. OH maps the same Hunter tables; no rows are copied.
- OH never imports PCH. PCH integration is through Engine, source and notification-channel injection.
- The engine depends only on the persistence ports in `opportunity_hunter.ports` (repositories, Unit of Work, worker lock); `SqlAlchemyStore` provides their PostgreSQL implementation.
- Subito Scanner 0.2.0 and Vinted Scanner 0.2.0 have built-in optional adapters (`opportunity_hunter.sources`), installed with the `subito` and `vinted` extras.

## Database migrations

Opportunity Hunter owns the migrations of its nine tables (`opportunity_hunter.migrations`, ADR-0013 phase 5). Its version is kept in `oh_alembic_version`, so a host's own `alembic_version` is never read or written. There is no downgrade.

- New database: `opportunity-hunter init-db --yes-empty-db` (refuses a database with any table), or `upgrade-db`. The `oh0001` baseline creates the tables exactly as Personal Collection Hub had them at its revision `0010`.
- Existing database that already has the tables (PCH's): `check-db` prints how they differ from the baseline; `adopt-db --yes-existing-db` builds the baseline in a throw-away schema inside a savepoint, compares columns, constraints and indexes, and stamps `oh0001` only on an exact match, with row counts checked before and after, in one transaction. No table, column or row is touched.
- After that, `upgrade-db` applies later revisions.

Adopting a production database is a separately authorized step: back it up and test the restore first, and run `check-db` and `adopt-db` on the restored copy before the real one.

## Install

    uv sync
    uv run pytest

Install scanner support with `uv sync --extra subito`, `--extra vinted` or `--extra all`. Without an extra, that marketplace is simply unavailable.

## Licences of the extras

Opportunity Hunter is MIT, and so is Subito Scanner. Vinted Scanner is **GPL-3.0-only**: parts of it are ported from VintedScanner by Andrea Draghetti. The `vinted` extra is optional for modularity, not to hide that obligation: anyone who distributes Opportunity Hunter together with Vinted Scanner distributes GPL-3.0 code and must honour the GPL for it.

## Configuration

Set OH_DATABASE_URL (PostgreSQL SQLAlchemy URL). Optional Telegram delivery requires both OH_TELEGRAM_BOT_TOKEN and OH_TELEGRAM_CHAT_ID. OH_NOTIFICATION_MAX_ATTEMPTS defaults to 5; OH_LOG_LEVEL defaults to INFO.

## Python API

    from sqlalchemy import create_engine
    from opportunity_hunter import OpportunityHunter, OpportunityHunterConfig, SqlAlchemyStore

    engine = create_engine("postgresql+psycopg://...")
    hunter = OpportunityHunter(
        config=OpportunityHunterConfig(),
        store=SqlAlchemyStore(engine=engine),
        sources={},
        channels=[],
    )
    result = hunter.run_due()

The injected Engine remains caller-owned. Construction never creates or changes tables.
