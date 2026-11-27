"""The SQLAlchemy adapter of every persistence port (ADR-0013).

The queries are the ones the pipeline ran directly on a Session before the ports existed (and that PCH's
`pch.hunter` runs today): same tables, same locks, same ordering. Works on an Engine owned by Opportunity
Hunter (standalone) or on one supplied by the host (embedded).
"""
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from sqlalchemy import Engine, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, sessionmaker

from opportunity_hunter.db.models import (
    AcquisitionRun,
    AppSetting,
    Listing,
    ListingPriceHistory,
    MarketplaceQuery,
    Match,
    Notification,
    Opportunity,
    SearchProfile,
)
from opportunity_hunter.ports import WORKER_LOCK_KEY


class SqlAlchemySearchProfileRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get_query(self, marketplace_query_id: int) -> MarketplaceQuery | None:
        return self.session.get(MarketplaceQuery, marketplace_query_id)

    def all_profiles(self) -> list[SearchProfile]:
        return list(self.session.scalars(select(SearchProfile).order_by(SearchProfile.id)))

    def active_profiles_for(self, marketplace: str) -> list[SearchProfile]:
        on_marketplace = select(MarketplaceQuery.search_profile_id).where(MarketplaceQuery.marketplace == marketplace)
        return list(self.session.scalars(select(SearchProfile).where(SearchProfile.status == "active", SearchProfile.id.in_(on_marketplace))))

    def schedule_candidates(self) -> list[tuple[int, str, int, datetime | None, datetime | None, str, str]]:
        rows = self.session.execute(
            select(MarketplaceQuery.id, MarketplaceQuery.marketplace, MarketplaceQuery.scan_interval_minutes, MarketplaceQuery.last_started_at,
                   MarketplaceQuery.manual_run_requested_at, MarketplaceQuery.status, SearchProfile.status)
            .join(SearchProfile)
            .where(or_(MarketplaceQuery.manual_run_requested_at.is_not(None), MarketplaceQuery.status == "active"))
            .where(SearchProfile.status != "archived")
            .order_by(MarketplaceQuery.manual_run_requested_at.asc().nulls_last(), MarketplaceQuery.last_started_at.asc().nulls_first())
        ).all()
        return [tuple(row) for row in rows]  # type: ignore[misc]

    def marketplaces(self) -> list[str]:
        return list(self.session.scalars(select(MarketplaceQuery.marketplace).distinct().order_by(MarketplaceQuery.marketplace)))


class SqlAlchemyRunRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def start(self, query: MarketplaceQuery, started_at: datetime, dry_run: bool) -> AcquisitionRun | None:
        # a savepoint, so a second running run of the same query (unique index) leaves the outer transaction usable
        try:
            with self.session.begin_nested():
                run = AcquisitionRun(marketplace_query_id=query.id, status="running", started_at=started_at, dry_run=dry_run)
                self.session.add(run)
                query.last_started_at = started_at
                query.manual_run_requested_at = None
        except IntegrityError:
            return None
        return run

    def get(self, run_id: int) -> AcquisitionRun | None:
        return self.session.get(AcquisitionRun, run_id)

    def close_abandoned(self, finished_at: datetime) -> int:
        result = self.session.execute(
            update(AcquisitionRun)
            .where(AcquisitionRun.status == "running")
            .values(status="failed", error_kind="internal", error_message="abandoned: the worker stopped during the run", finished_at=finished_at)
        )
        rowcount = getattr(result, "rowcount", 0)
        return rowcount if isinstance(rowcount, int) else 0

    def recent_finished(self, marketplace: str, limit: int) -> list[AcquisitionRun]:
        return list(self.session.scalars(
            select(AcquisitionRun).join(MarketplaceQuery)
            .where(MarketplaceQuery.marketplace == marketplace, AcquisitionRun.status != "running")
            .order_by(AcquisitionRun.started_at.desc(), AcquisitionRun.id.desc())
            .limit(limit)
        ))


class SqlAlchemyListingRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def lock_known(self, marketplace: str, external_ids: Iterable[str]) -> dict[str, Listing]:
        return {
            listing.external_id: listing
            for listing in self.session.scalars(
                select(Listing).where(Listing.marketplace == marketplace, Listing.external_id.in_(list(external_ids))).with_for_update()
            )
        }

    def add(self, listing: Listing) -> None:
        self.session.add(listing)

    def mark_not_seen(self, cutoff: datetime) -> int:
        result = self.session.execute(
            update(Listing)
            .where(Listing.availability == "active", Listing.origin == "acquired", Listing.last_seen_at < cutoff)
            .values(availability="not_seen")
        )
        rowcount = getattr(result, "rowcount", 0)
        return rowcount if isinstance(rowcount, int) else 0


class SqlAlchemyPriceHistoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(self, listing: Listing, price_status: str, price_amount: Decimal | None, currency: str | None, observed_at: datetime) -> None:
        self.session.add(ListingPriceHistory(listing=listing, price_status=price_status, price_amount=price_amount, currency=currency,
                                             observed_at=observed_at))

    def latest_two(self, listing_ids: Sequence[int]) -> dict[int, list[tuple[Decimal | None, str | None]]]:
        ranked = (
            select(
                ListingPriceHistory.listing_id, ListingPriceHistory.price_amount, ListingPriceHistory.currency,
                func.row_number().over(
                    partition_by=ListingPriceHistory.listing_id, order_by=(ListingPriceHistory.observed_at.desc(), ListingPriceHistory.id.desc())
                ).label("rank"),
            )
            .where(ListingPriceHistory.listing_id.in_(listing_ids))
            .subquery()
        )
        latest: dict[int, list[tuple[Decimal | None, str | None]]] = {}
        for listing_id, amount, currency, _ in self.session.execute(
            select(ranked).where(ranked.c.rank <= 2).order_by(ranked.c.listing_id, ranked.c.rank)
        ):
            latest.setdefault(listing_id, []).append((amount, currency))
        return latest

    def amounts(self, listing_id: int) -> list[Decimal | None]:
        return list(self.session.scalars(
            select(ListingPriceHistory.price_amount).where(ListingPriceHistory.listing_id == listing_id)
            .order_by(ListingPriceHistory.observed_at, ListingPriceHistory.id)
        ))


class SqlAlchemyMatchRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def for_pairs(self, listing_ids: Sequence[int], profile_ids: Sequence[int]) -> list[Match]:
        return list(self.session.scalars(select(Match).where(Match.listing_id.in_(listing_ids), Match.search_profile_id.in_(profile_ids))))

    def add(self, match: Match) -> None:
        self.session.add(match)

    def matched_for_profile(self, profile_id: int) -> list[Match]:
        return list(self.session.scalars(
            select(Match).where(Match.search_profile_id == profile_id, Match.matched).options(joinedload(Match.listing)).order_by(Match.id)
        ))

    def decisions(self, profile_id: int) -> list[tuple[str | None, str, str | None]]:
        rows = self.session.execute(
            select(Listing.title, Opportunity.status, Opportunity.dismiss_reason)
            .join(Match, Match.id == Opportunity.match_id).join(Listing, Listing.id == Match.listing_id)
            .where(Match.search_profile_id == profile_id, Opportunity.status != "new")
            .order_by(Opportunity.id)
        )
        return [(title, status, reason) for title, status, reason in rows]

    def relevant_costs(self, profile_id: int, seen_since: datetime, min_relevance: int) -> list[Decimal]:
        cost = Match.reasons["total_cost"].astext
        return [Decimal(value) for value in self.session.scalars(
            select(cost).join(Listing, Listing.id == Match.listing_id)
            .where(
                Match.search_profile_id == profile_id, Match.matched, cost.is_not(None), Listing.last_seen_at >= seen_since,
                or_(Match.relevance.is_(None), Match.relevance >= min_relevance),
            )
        )]


class SqlAlchemyOpportunityRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(self, match: Match, created_at: datetime) -> Opportunity:
        match.opportunity = Opportunity(status="new", status_changed_at=created_at, created_at=created_at)
        return match.opportunity


class SqlAlchemyNotificationOutbox:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, opportunity: Opportunity, channel: str, reason: str, status: str, suppressed_reason: str | None, created_at: datetime) -> None:
        self.session.add(Notification(
            opportunity=opportunity, channel=channel, reason=reason, status=status, suppressed_reason=suppressed_reason, created_at=created_at,
        ))

    def add_ignoring_duplicates(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            self.session.execute(insert(Notification).values(list(rows)).on_conflict_do_nothing(constraint="uq_notification_opportunity_channel_reason"))

    def deliverable_ids(self, channels: Iterable[str], max_attempts: int) -> list[int]:
        return list(self.session.scalars(
            select(Notification.id)
            .where(Notification.channel.in_(list(channels)))
            .where((Notification.status == "pending") | ((Notification.status == "failed") & (Notification.attempts < max_attempts)))
            .order_by(Notification.id)
        ))

    def claim(self, notification_id: int) -> Notification | None:
        return self.session.scalars(
            select(Notification).where(Notification.id == notification_id).with_for_update(skip_locked=True, of=Notification)
            .options(joinedload(Notification.opportunity).joinedload(Opportunity.match).joinedload(Match.listing),
                     joinedload(Notification.opportunity).joinedload(Opportunity.match).joinedload(Match.search_profile))
        ).first()


class SqlAlchemySettingsStore:
    """Settings in `app_setting` (JSONB values)."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, key: str) -> Any | None:
        row = self.session.get(AppSetting, key)
        return None if row is None else row.value

    def get_many(self, keys: Iterable[str]) -> dict[str, Any]:
        return {row.key: row.value for row in self.session.scalars(select(AppSetting).where(AppSetting.key.in_(list(keys))))}

    def put(self, key: str, value: Any) -> None:
        self.session.merge(AppSetting(key=key, value=value))

    def delete(self, key: str) -> None:
        row = self.session.get(AppSetting, key)
        if row is not None:
            self.session.delete(row)


class SqlAlchemyUnitOfWork:
    """One Session, one transaction. Leaving the block without commit() rolls back; the Session is always closed."""

    profiles: SqlAlchemySearchProfileRepository
    runs: SqlAlchemyRunRepository
    listings: SqlAlchemyListingRepository
    price_history: SqlAlchemyPriceHistoryRepository
    matches: SqlAlchemyMatchRepository
    opportunities: SqlAlchemyOpportunityRepository
    outbox: SqlAlchemyNotificationOutbox
    settings: SqlAlchemySettingsStore

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    @property
    def session(self) -> Session:
        if self._session is None:
            raise RuntimeError("the Unit of Work is not active: use it in a with block")
        return self._session

    def __enter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("the Unit of Work is already active")
        session = self._session = self._session_factory()
        self.profiles = SqlAlchemySearchProfileRepository(session)
        self.runs = SqlAlchemyRunRepository(session)
        self.listings = SqlAlchemyListingRepository(session)
        self.price_history = SqlAlchemyPriceHistoryRepository(session)
        self.matches = SqlAlchemyMatchRepository(session)
        self.opportunities = SqlAlchemyOpportunityRepository(session)
        self.outbox = SqlAlchemyNotificationOutbox(session)
        self.settings = SqlAlchemySettingsStore(session)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        session, self._session = self.session, None
        try:
            session.rollback()  # no-op after commit()
        finally:
            session.close()

    def flush(self) -> None:
        self.session.flush()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


class SqlAlchemyWorkerLock:
    """Session-level PostgreSQL advisory lock on a dedicated connection, released on exit (or if the process dies)."""

    def __init__(self, engine: Engine, key: int = WORKER_LOCK_KEY) -> None:
        self.engine = engine
        self.key = key

    @contextmanager
    def __call__(self) -> Iterator[bool]:
        with self.engine.connect() as conn:
            locked = bool(conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key}).scalar())
            conn.commit()
            try:
                yield locked
            finally:
                if locked:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
                    conn.commit()


if TYPE_CHECKING:  # static proof (mypy) that the adapter implements every port
    from opportunity_hunter import ports

    def _conforms(session: Session, factory: sessionmaker[Session], engine: Engine) -> None:
        _profiles: ports.SearchProfileRepository = SqlAlchemySearchProfileRepository(session)
        _runs: ports.RunRepository = SqlAlchemyRunRepository(session)
        _listings: ports.ListingRepository = SqlAlchemyListingRepository(session)
        _history: ports.PriceHistoryRepository = SqlAlchemyPriceHistoryRepository(session)
        _matches: ports.MatchRepository = SqlAlchemyMatchRepository(session)
        _opportunities: ports.OpportunityRepository = SqlAlchemyOpportunityRepository(session)
        _outbox: ports.NotificationOutbox = SqlAlchemyNotificationOutbox(session)
        _settings: ports.SettingsStore = SqlAlchemySettingsStore(session)
        _uow: ports.UnitOfWork = SqlAlchemyUnitOfWork(factory)
        _lock: ports.WorkerLock = SqlAlchemyWorkerLock(engine)
