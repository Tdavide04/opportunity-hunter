# Opportunity Hunter

Standalone and embeddable marketplace opportunity engine.

This repository contains the marketplace engine extracted from Personal Collection Hub (PCH): canonical listing types, source protocol, orchestration, matching, scoring, deduplication, price history, acquisition runs, notification outbox, circuit breaking and availability maintenance.

## Architecture

- Standalone: OpportunityHunter owns or receives a PostgreSQL-backed SqlAlchemyStore.
- Embedded: PCH passes its existing SQLAlchemy Engine or session factory. OH maps the same Hunter tables; no rows are copied.
- OH never imports PCH. PCH integration is through Engine, source and notification-channel injection.
- The engine depends only on the persistence ports in `opportunity_hunter.ports` (repositories, Unit of Work, worker lock); `SqlAlchemyStore` provides their PostgreSQL implementation.
- Subito Scanner 0.2.0 and Vinted Scanner 0.2.0 have built-in optional adapters (`opportunity_hunter.sources`), installed with the `subito` and `vinted` extras.

## Safety boundary

No schema migration, Alembic stamp, production database change or PCH runtime switch is part of this extraction. The init-db command is a fresh-database bootstrap only and refuses to run when any user tables already exist. Existing databases remain untouched.

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

The injected Engine remains caller-owned. Construction never creates or changes tables. Alembic ownership and production stamping are a separately authorized phase.
