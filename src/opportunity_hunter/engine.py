"""Public facade for standalone and embedded execution."""
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from opportunity_hunter.adapter import MarketplaceAdapter
from opportunity_hunter.channels.base import NotificationChannel
from opportunity_hunter.config import OpportunityHunterConfig
from opportunity_hunter.db.store import SqlAlchemyStore
from opportunity_hunter.hunter.pipeline import DueResult, Pipeline, RunSummary, utcnow
from opportunity_hunter.sources.subito import default_sources


class OpportunityHunter:
    """Run standalone or embed against an existing SQLAlchemy Engine.

    In embedded mode PCH injects its Engine/session factory, source adapters and notification channels.
    Construction never creates, migrates or changes tables.
    """

    def __init__(
        self,
        config: OpportunityHunterConfig | None = None,
        store: SqlAlchemyStore | Engine | sessionmaker[Session] | None = None,
        channels: list[NotificationChannel] | None = None,
        sources: Mapping[str, MarketplaceAdapter] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.config = config or OpportunityHunterConfig.from_env()
        self._owns_store = store is None
        if isinstance(store, SqlAlchemyStore):
            self.store = store
        elif isinstance(store, Engine):
            self.store = SqlAlchemyStore(engine=store)
        elif store is not None:
            self.store = SqlAlchemyStore(session_factory=store)
        else:
            if not self.config.database_url:
                raise ValueError("OH_DATABASE_URL is required unless an existing store/engine is injected")
            self.store = SqlAlchemyStore(database_url=self.config.database_url)

        if channels is None:
            from opportunity_hunter.channels.telegram import TelegramChannel
            channels = (
                [TelegramChannel(self.config.telegram_bot_token, self.config.telegram_chat_id)]
                if self.config.telegram_bot_token and self.config.telegram_chat_id
                else []
            )
        self.sources = dict(default_sources() if sources is None else sources)
        self.pipeline = Pipeline(
            self.store.session_factory,
            self.sources,
            now=now,
            channels=channels,
            max_attempts=self.config.notification_max_attempts,
        )

    def run_due(self) -> DueResult:
        return self.pipeline.run_due()

    def run_query(self, marketplace_query_id: int, *, dry_run: bool = False) -> RunSummary:
        return self.pipeline.run_query(marketplace_query_id, dry_run=dry_run)

    def dispatch(self) -> dict[str, int]:
        return self.pipeline.dispatch()

    def preview(self, marketplace: str, params: dict[str, Any], max_items: int = 20) -> list[Any]:
        if max_items < 1:
            raise ValueError("max_items must be at least 1")
        source = self.sources[marketplace]
        normalised = source.validate_params(params)
        page = source.fetch_page(normalised, None)
        return page.candidates[:max_items]

    def close(self) -> None:
        if self._owns_store:
            self.store.close()
