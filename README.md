# Opportunity Hunter

Standalone and embeddable marketplace opportunity engine.

This repository contains the marketplace engine extracted from Personal Collection Hub (PCH): canonical listing types, source protocol, orchestration, matching, scoring, deduplication, price history, acquisition runs, notification outbox, circuit breaking and availability maintenance.

## Architecture

- Standalone: OpportunityHunter owns or receives a PostgreSQL-backed SqlAlchemyStore.
- Embedded: PCH passes its existing SQLAlchemy Engine or session factory. OH maps the same Hunter tables; no rows are copied.
- OH never imports PCH. PCH integration is through Engine, source and notification-channel injection.
- Subito Scanner 0.2.0 has a built-in optional adapter.
- Vinted Scanner 0.1.0 is not yet standalone and still imports pch-marketplace-kit. It is deliberately not bundled or enabled here until its standalone extraction is complete.

## Safety boundary

No schema migration, Alembic stamp, production database change or PCH runtime switch is part of this extraction. The init-db command is a fresh-database bootstrap only and refuses to run when any user tables already exist. Existing databases remain untouched.

## Install

    uv sync
    uv run pytest

Install Subito support with uv sync --extra subito. Vinted support must be injected by an application that supplies a compliant standalone source adapter after the Vinted scanner extraction.

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
