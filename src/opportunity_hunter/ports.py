"""Persistence ports of Opportunity Hunter (ADR-0013).

Application services and the pipeline depend only on these contracts. One SQLAlchemy adapter
(`opportunity_hunter.db.repositories`) implements all of them on the existing Hunter tables; a host such as
PCH reaches the same tables by handing its Engine/session factory to `SqlAlchemyStore`.

The ports exchange the mapped entity classes of `opportunity_hunter.db.models`. Inside one Unit of Work the
entities are tracked: attribute changes made by a service are written when the Unit of Work commits, exactly
as the code extracted from PCH behaved with a Session. Pure domain entities are a possible later step, not a
requirement of this phase.
"""
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, Protocol, Self

from opportunity_hunter.db.models import AcquisitionRun, Listing, MarketplaceQuery, Match, Notification, Opportunity, SearchProfile

# PostgreSQL advisory lock key of the one worker allowed to acquire at a time (ADR-0001). Shared with PCH's
# worker so that the two never run together while PCH moves to this library: never change it.
WORKER_LOCK_KEY = 0x50434857  # "PCHW"


class SearchProfileRepository(Protocol):
    """Search Profiles and their Marketplace Queries."""

    def get_query(self, marketplace_query_id: int) -> MarketplaceQuery | None: ...

    def active_profiles_for(self, marketplace: str) -> list[SearchProfile]:
        """Active Search Profiles with at least one Marketplace Query on this Marketplace."""
        ...

    def schedule_candidates(self) -> list[tuple[int, str, int, datetime | None, datetime | None, str, str]]:
        """(query id, marketplace, interval minutes, last started, manual request, query status, profile status) of every
        query that is active or manually requested and whose profile is not archived, manual requests first."""
        ...

    def marketplaces(self) -> list[str]:
        """Distinct Marketplaces that have Marketplace Queries, sorted."""
        ...


class RunRepository(Protocol):
    """Acquisition Runs."""

    def start(self, query: MarketplaceQuery, started_at: datetime, dry_run: bool) -> AcquisitionRun | None:
        """Record a running run and mark the query started. None (nothing recorded) if the query already has a running run."""
        ...

    def get(self, run_id: int) -> AcquisitionRun | None: ...

    def close_abandoned(self, finished_at: datetime) -> int:
        """Fail every run still `running` (its worker died); returns how many."""
        ...

    def recent_finished(self, marketplace: str, limit: int) -> list[AcquisitionRun]:
        """The latest finished runs of a Marketplace, newest first."""
        ...


class ListingRepository(Protocol):
    def lock_known(self, marketplace: str, external_ids: Iterable[str]) -> dict[str, Listing]:
        """Known Listings among these External IDs, locked for update until the Unit of Work ends."""
        ...

    def add(self, listing: Listing) -> None: ...

    def mark_not_seen(self, cutoff: datetime) -> int:
        """Acquired, active Listings last seen before `cutoff` become `not_seen`; returns how many."""
        ...


class PriceHistoryRepository(Protocol):
    def record(self, listing: Listing, price_status: str, price_amount: Decimal | None, currency: str | None, observed_at: datetime) -> None: ...

    def latest_two(self, listing_ids: Sequence[int]) -> dict[int, list[tuple[Decimal | None, str | None]]]:
        """listing id -> its latest one or two observations (amount, currency), newest first."""
        ...

    def amounts(self, listing_id: int) -> list[Decimal | None]:
        """Every observed amount of a Listing, oldest first."""
        ...


class MatchRepository(Protocol):
    def for_pairs(self, listing_ids: Sequence[int], profile_ids: Sequence[int]) -> list[Match]: ...

    def add(self, match: Match) -> None: ...


class OpportunityRepository(Protocol):
    def create(self, match: Match, created_at: datetime) -> Opportunity:
        """Open a `new` Opportunity on a Match."""
        ...


class NotificationOutbox(Protocol):
    """Notification outbox (ADR-0003): written with the Opportunities, delivered after commit."""

    def add(self, opportunity: Opportunity, channel: str, reason: str, status: str, suppressed_reason: str | None, created_at: datetime) -> None: ...

    def add_ignoring_duplicates(self, rows: Sequence[Mapping[str, Any]]) -> None:
        """Insert rows for existing Opportunities; a row already present for (opportunity, channel, reason) is skipped."""
        ...

    def deliverable_ids(self, channels: Iterable[str], max_attempts: int) -> list[int]:
        """Pending Notifications, and failed ones below max_attempts, of these channels, oldest first."""
        ...

    def claim(self, notification_id: int) -> Notification | None:
        """Lock one Notification (skipping it if another worker holds it), with its Opportunity, Match, Listing and profile."""
        ...


class SettingsStore(Protocol):
    """Key/value settings (in PCH: `app_setting`)."""

    def get(self, key: str) -> Any | None: ...

    def get_many(self, keys: Iterable[str]) -> dict[str, Any]: ...

    def put(self, key: str, value: Any) -> None: ...

    def delete(self, key: str) -> None: ...


class WorkerLock(Protocol):
    def __call__(self) -> AbstractContextManager[bool]:
        """Try to become the one acquiring worker; yields False if another worker holds the lock."""
        ...


class UnitOfWork(Protocol):
    """One transaction across all repositories. Leaving the block without commit() rolls back."""

    @property
    def profiles(self) -> SearchProfileRepository: ...

    @property
    def runs(self) -> RunRepository: ...

    @property
    def listings(self) -> ListingRepository: ...

    @property
    def price_history(self) -> PriceHistoryRepository: ...

    @property
    def matches(self) -> MatchRepository: ...

    @property
    def opportunities(self) -> OpportunityRepository: ...

    @property
    def outbox(self) -> NotificationOutbox: ...

    @property
    def settings(self) -> SettingsStore: ...


    def __enter__(self) -> Self: ...

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None: ...

    def flush(self) -> None:
        """Send pending writes (assigning ids) without committing."""
        ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


class UnitOfWorkFactory(Protocol):
    def __call__(self) -> UnitOfWork: ...
