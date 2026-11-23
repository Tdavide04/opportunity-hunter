"""Opportunity Hunter pipeline: the main seam. Runs Marketplace Queries into Acquisition Runs and Listings."""
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from opportunity_hunter.adapter import MarketplaceAdapter, PermanentMarketplaceError, TransientMarketplaceError
from opportunity_hunter.channels.base import NotificationChannel
from opportunity_hunter.db.models import AcquisitionRun, AppSetting, MarketplaceQuery, SearchProfile
from opportunity_hunter.hunter.availability import maintain
from opportunity_hunter.hunter.circuit import record_outcome, suspended_until
from opportunity_hunter.hunter.evaluation import evaluate, profiles_for
from opportunity_hunter.hunter.listings import upsert_page
from opportunity_hunter.hunter.notifications import dispatch, enqueue, read_settings

logger = logging.getLogger(__name__)

# PostgreSQL advisory lock held by the one worker allowed to acquire at a time (ADR-0001)
WORKER_LOCK_KEY = 0x50434857  # "PCHW"


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RunSummary:
    run_id: int | None
    status: str  # succeeded | partial | failed | skipped
    marketplace_query_id: int | None = None
    pages: int = 0
    seen: int = 0
    new: int = 0
    updated: int = 0
    rejected: int = 0
    opportunities: int = 0
    dry_run: bool = False
    error_kind: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class DueResult:
    locked: bool  # False: another worker holds the lock, nothing was run
    summaries: list[RunSummary] = field(default_factory=list)
    dispatched: dict[str, int] = field(default_factory=dict)


class Pipeline:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        registry: dict[str, MarketplaceAdapter],
        now: Callable[[], datetime] = utcnow,
        channels: list[NotificationChannel] | None = None,
        max_attempts: int = 5,
    ):
        self.session_factory = session_factory
        self.registry = registry
        self.now = now
        self.channels = channels or []
        self.max_attempts = max_attempts

    def dispatch(self) -> dict[str, int]:
        """Deliver pending Notifications (after the runs committed them) and retry failed ones."""
        return dispatch(self.session_factory, self.channels, self.max_attempts, self.now())

    def run_query(self, marketplace_query_id: int, *, dry_run: bool = False) -> RunSummary:
        """Run one Marketplace Query as an Acquisition Run; one transaction per page.

        Returns `skipped` (and records nothing) when the query already has a running run.
        Unexpected exceptions are recorded on the run as `internal` and re-raised.
        """
        with self.session_factory() as session:
            query = session.get(MarketplaceQuery, marketplace_query_id)
            if query is None:
                raise LookupError(f"Marketplace Query {marketplace_query_id} not found")
            marketplace, params, max_pages = query.marketplace, dict(query.params), query.max_pages
            settings = read_settings(session)
            drop_min_percent = Decimal(str(settings["price_drop_min_percent"]))
            dry_run = dry_run or settings["dry_run"]
            baseline = query.baseline_completed_at is None
            # Suppressed Notifications are recorded but never sent (glossary)
            if dry_run:
                suppressed = "dry_run"
            elif settings["notifications_paused"]:
                suppressed = "paused"
            elif marketplace in settings["shadow_marketplaces"]:
                suppressed = "shadow_mode"  # PCH acquires and evaluates, the Legacy Scanner keeps notifying
            elif baseline:
                suppressed = "baseline"
            else:
                suppressed = None
            started_at = self.now()
            run = AcquisitionRun(marketplace_query_id=marketplace_query_id, status="running", started_at=started_at, dry_run=dry_run)
            session.add(run)
            query.last_started_at = started_at
            query.manual_run_requested_at = None
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                logger.info("run skipped: query already running", extra={"event": "run_skipped", "marketplace_query_id": marketplace_query_id})
                return RunSummary(run_id=None, status="skipped", marketplace_query_id=marketplace_query_id)
            run_id = run.id

        log = {"run_id": run_id, "marketplace": marketplace, "marketplace_query_id": marketplace_query_id}
        logger.info("run started", extra={"event": "run_started", **log})
        adapter = self.registry[marketplace]
        totals = {"pages": 0, "seen": 0, "new": 0, "updated": 0, "rejected": 0, "opportunities": 0}
        error_kind = error_message = None
        cursor = None
        try:
            while totals["pages"] < max_pages:
                page = adapter.fetch_page(params, cursor)
                totals["rejected"] += len(page.rejected)
                for rejected in page.rejected:
                    logger.warning(
                        "listing rejected",
                        extra={
                            "event": "listing_rejected",
                            "external_id": rejected.external_id,
                            "reason": rejected.reason,
                            **log,
                        },
                    )
                if not page.candidates and not page.rejected:
                    totals["pages"] += 1  # an empty page was still read; saved by _finish
                    break
                with self.session_factory() as session, session.begin():
                    now = self.now()
                    counts = upsert_page(session, marketplace, page.candidates, run_id, now)
                    # profiles are re-read per page so edits made during a long run apply from the next page
                    outcome = evaluate(
                        session, counts.seen, counts.changed, profiles_for(session, marketplace), now, counts.price_drops, drop_min_percent
                    )
                    # same transaction as the Opportunities (ADR-0003); a Legacy Seen Listing was already notified by the
                    # Legacy Scanner, so its Opportunities are recorded as suppressed
                    legacy = [o for o in outcome.created if o.match.listing_id in counts.promoted]
                    fresh = [o for o in outcome.created if o.match.listing_id not in counts.promoted]
                    enqueue(session, fresh, self.channels, suppressed, now, outcome.price_drops)
                    enqueue(session, legacy, self.channels, suppressed or "legacy_seen", now)
                    totals["opportunities"] += len(outcome.created)
                    totals["pages"] += 1
                    totals["seen"] += len({c.external_id for c in page.candidates})
                    totals["new"] += counts.new
                    totals["updated"] += counts.updated
                    self._save_progress(session, run_id, totals)
                fully_unchanged = page.candidates and counts.new == counts.updated == 0
                if page.next_cursor is None or (page.newest_first and fully_unchanged):
                    break
                cursor = page.next_cursor
        except (TransientMarketplaceError, PermanentMarketplaceError) as exc:
            error_kind = "transient" if isinstance(exc, TransientMarketplaceError) else "permanent"
            error_message = str(exc)
        except Exception as exc:
            self._finish(run_id, totals, "internal", f"{type(exc).__name__}: {exc}")
            logger.exception("run crashed", extra={"event": "run_crashed", **log})
            raise

        status = self._finish(run_id, totals, error_kind, error_message, complete_baseline=baseline and not dry_run)
        logger.info("run finished", extra={"event": "run_finished", "status": status, "dry_run": dry_run, **totals, **log})
        return RunSummary(
            run_id=run_id, status=status, marketplace_query_id=marketplace_query_id, dry_run=dry_run,
            error_kind=error_kind, error_message=error_message, **totals
        )

    def run_due(self) -> DueResult:
        """Run every due Marketplace Query (and every manual scan request), holding the worker lock.

        Due: query active, Search Profile active, never started or started at least one interval ago.
        A manual request runs once even if the query is not due or is paused (not if its profile is archived).
        One failing query does not stop the others.
        """
        with self.worker_lock() as locked:
            if not locked:
                logger.info("another worker is running: exiting", extra={"event": "worker_busy"})
                return DueResult(locked=False)
            self._record_pass()
            self._close_abandoned_runs()
            summaries = []
            for query_id, marketplace in self._due_queries():
                with self.session_factory() as session:
                    if suspended_until(session, marketplace, self.now()):
                        continue  # circuit breaker: skip until the suspension ends (manual requests stay pending)
                try:
                    summaries.append(self.run_query(query_id))
                except Exception:
                    # already recorded on the run as `internal` and logged by run_query
                    summaries.append(RunSummary(run_id=None, status="failed", marketplace_query_id=query_id, error_kind="internal"))
                with self.session_factory() as session, session.begin():
                    until = record_outcome(session, marketplace, self.now())
                if until:
                    logger.warning("marketplace suspended", extra={"event": "marketplace_suspended", "marketplace": marketplace, "until": until.isoformat()})
            maintain(self.session_factory, self.now())  # after the runs refreshed Last Seen
            return DueResult(locked=True, summaries=summaries, dispatched=self.dispatch())

    @contextmanager
    def worker_lock(self) -> Iterator[bool]:
        """Session-level advisory lock on a dedicated connection, released on exit (or if the process dies)."""
        engine = self.session_factory.kw["bind"]
        with engine.connect() as conn:
            locked = conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK_KEY}).scalar()
            conn.commit()
            try:
                yield locked
            finally:
                if locked:
                    conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK_KEY})
                    conn.commit()

    def _record_pass(self) -> None:
        with self.session_factory() as session, session.begin():
            session.merge(AppSetting(key="worker_last_pass_at", value=self.now().isoformat()))

    def _due_queries(self) -> list[tuple[int, str]]:
        now = self.now()
        with self.session_factory() as session:
            rows = session.execute(
                select(MarketplaceQuery.id, MarketplaceQuery.marketplace, MarketplaceQuery.scan_interval_minutes, MarketplaceQuery.last_started_at,
                       MarketplaceQuery.manual_run_requested_at, MarketplaceQuery.status, SearchProfile.status)
                .join(SearchProfile)
                .where(or_(MarketplaceQuery.manual_run_requested_at.is_not(None), MarketplaceQuery.status == "active"))
                .where(SearchProfile.status != "archived")
                .order_by(MarketplaceQuery.manual_run_requested_at.asc().nulls_last(), MarketplaceQuery.last_started_at.asc().nulls_first())
            ).all()
        due = []
        for query_id, marketplace, interval, last_started, manual, query_status, profile_status in rows:
            scheduled = (
                query_status == "active" and profile_status == "active"
                and (last_started is None or last_started + timedelta(minutes=interval) <= now)
            )
            if manual is not None or scheduled:
                due.append((query_id, marketplace))
        return due

    def _close_abandoned_runs(self) -> None:
        """Holding the worker lock, any run still `running` belonged to a worker that died mid-run."""
        with self.session_factory() as session, session.begin():
            closed = session.execute(
                update(AcquisitionRun)
                .where(AcquisitionRun.status == "running")
                .values(status="failed", error_kind="internal", error_message="abandoned: the worker stopped during the run", finished_at=self.now())
            ).rowcount
        if closed:
            logger.warning("abandoned runs closed", extra={"event": "runs_abandoned", "count": closed})

    def _save_progress(self, session: Session, run_id: int, totals: dict[str, int]) -> None:
        run = session.get(AcquisitionRun, run_id)
        run.pages = totals["pages"]
        run.listings_seen = totals["seen"]
        run.listings_new = totals["new"]
        run.listings_updated = totals["updated"]
        run.rejected = totals["rejected"]

    def _finish(
        self, run_id: int, totals: dict[str, int], error_kind: str | None, error_message: str | None, complete_baseline: bool = False
    ) -> str:
        if error_kind is None:
            status = "succeeded"
        else:
            status = "partial" if totals["pages"] else "failed"
        with self.session_factory() as session, session.begin():
            self._save_progress(session, run_id, totals)
            run = session.get(AcquisitionRun, run_id)
            run.status = status
            run.finished_at = self.now()
            run.error_kind = error_kind
            run.error_message = error_message
            if complete_baseline and status == "succeeded":
                # the first successful run is the Baseline: from now on new Opportunities notify
                session.get(MarketplaceQuery, run.marketplace_query_id).baseline_completed_at = run.finished_at
        return status
