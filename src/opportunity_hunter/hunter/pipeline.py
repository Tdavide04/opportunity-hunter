"""Opportunity Hunter pipeline: the main seam. Runs Marketplace Queries into Acquisition Runs and Listings."""
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opportunity_hunter.adapter import MarketplaceAdapter, PermanentMarketplaceError, PricingSource, TransientMarketplaceError
from opportunity_hunter.channels.base import NotificationChannel
from opportunity_hunter.hunter.availability import maintain
from opportunity_hunter.hunter.circuit import record_outcome, suspended_until
from opportunity_hunter.hunter.evaluation import evaluate, profiles_for
from opportunity_hunter.hunter.listings import upsert_page
from opportunity_hunter.hunter.notifications import dispatch, enqueue, read_settings
from opportunity_hunter.hunter.pricing import complete_pricing
from opportunity_hunter.ports import WORKER_LOCK_KEY, RunRepository, UnitOfWorkFactory, WorkerLock

__all__ = ["WORKER_LOCK_KEY", "DueResult", "Pipeline", "RunSummary", "utcnow"]

logger = logging.getLogger(__name__)


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
    """Depends only on the persistence ports: a Unit of Work factory and the worker lock (ADR-0013)."""

    def __init__(
        self,
        unit_of_work: UnitOfWorkFactory,
        worker_lock: WorkerLock,
        registry: dict[str, MarketplaceAdapter],
        now: Callable[[], datetime] = utcnow,
        channels: list[NotificationChannel] | None = None,
        max_attempts: int = 5,
        pricing_per_page: int = 10,
    ):
        self.unit_of_work = unit_of_work
        self._worker_lock = worker_lock
        self.registry = registry
        self.now = now
        self.channels = channels or []
        self.max_attempts = max_attempts
        self.pricing_per_page = pricing_per_page  # item pages per list page, for adapters that price on demand

    def dispatch(self) -> dict[str, int]:
        """Deliver pending Notifications (after the runs committed them) and retry failed ones."""
        return dispatch(self.unit_of_work, self.channels, self.max_attempts, self.now())

    def run_query(self, marketplace_query_id: int, *, dry_run: bool = False) -> RunSummary:
        """Run one Marketplace Query as an Acquisition Run; one transaction per page.

        Returns `skipped` (and records nothing) when the query already has a running run.
        Unexpected exceptions are recorded on the run as `internal` and re-raised.
        """
        with self.unit_of_work() as uow:
            query = uow.profiles.get_query(marketplace_query_id)
            if query is None:
                raise LookupError(f"Marketplace Query {marketplace_query_id} not found")
            marketplace, params, max_pages = query.marketplace, dict(query.params), query.max_pages
            settings = read_settings(uow.settings)
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
            run = uow.runs.start(query, self.now(), dry_run)
            if run is None:
                logger.info("run skipped: query already running", extra={"event": "run_skipped", "marketplace_query_id": marketplace_query_id})
                return RunSummary(run_id=None, status="skipped", marketplace_query_id=marketplace_query_id)
            uow.commit()
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
                with self.unit_of_work() as uow:
                    now = self.now()
                    counts = upsert_page(uow, marketplace, page.candidates, run_id, now)
                    # profiles are re-read per page so edits made during a long run apply from the next page
                    profiles = profiles_for(uow.profiles, marketplace)
                    if isinstance(adapter, PricingSource):
                        complete_pricing(adapter, params, counts.changed, profiles, self.pricing_per_page)
                    outcome = evaluate(uow, counts.seen, counts.changed, profiles, now, counts.price_drops, drop_min_percent)
                    # same transaction as the Opportunities (ADR-0003); a Legacy Seen Listing was already notified by the
                    # Legacy Scanner, so its Opportunities are recorded as suppressed
                    legacy = [o for o in outcome.created if o.match.listing_id in counts.promoted]
                    fresh = [o for o in outcome.created if o.match.listing_id not in counts.promoted]
                    enqueue(uow, fresh, self.channels, suppressed, now, outcome.price_drops)
                    enqueue(uow, legacy, self.channels, suppressed or "legacy_seen", now)
                    totals["opportunities"] += len(outcome.created)
                    totals["pages"] += 1
                    totals["seen"] += len({c.external_id for c in page.candidates})
                    totals["new"] += counts.new
                    totals["updated"] += counts.updated
                    self._save_progress(uow.runs, run_id, totals)
                    uow.commit()
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
                with self.unit_of_work() as uow:
                    if suspended_until(uow.settings, marketplace, self.now()):
                        continue  # circuit breaker: skip until the suspension ends (manual requests stay pending)
                try:
                    summaries.append(self.run_query(query_id))
                except Exception:
                    # already recorded on the run as `internal` and logged by run_query
                    summaries.append(RunSummary(run_id=None, status="failed", marketplace_query_id=query_id, error_kind="internal"))
                with self.unit_of_work() as uow:
                    until = record_outcome(uow, marketplace, self.now())
                    uow.commit()
                if until:
                    logger.warning("marketplace suspended", extra={"event": "marketplace_suspended", "marketplace": marketplace, "until": until.isoformat()})
            maintain(self.unit_of_work, self.now())  # after the runs refreshed Last Seen
            return DueResult(locked=True, summaries=summaries, dispatched=self.dispatch())

    def worker_lock(self) -> AbstractContextManager[bool]:
        """The worker lock port: True if this process is the one acquiring worker until the block ends."""
        return self._worker_lock()

    def _record_pass(self) -> None:
        with self.unit_of_work() as uow:
            uow.settings.put("worker_last_pass_at", self.now().isoformat())
            uow.commit()

    def _due_queries(self) -> list[tuple[int, str]]:
        now = self.now()
        with self.unit_of_work() as uow:
            rows = uow.profiles.schedule_candidates()
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
        with self.unit_of_work() as uow:
            closed = uow.runs.close_abandoned(self.now())
            uow.commit()
        if closed:
            logger.warning("abandoned runs closed", extra={"event": "runs_abandoned", "count": closed})

    def _save_progress(self, runs: RunRepository, run_id: int, totals: dict[str, int]) -> None:
        run = runs.get(run_id)
        if run is None:
            raise LookupError(f"Acquisition Run {run_id} not found")
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
        with self.unit_of_work() as uow:
            self._save_progress(uow.runs, run_id, totals)
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"Acquisition Run {run_id} not found")
            finished_at = self.now()
            run.status = status
            run.finished_at = finished_at
            run.error_kind = error_kind
            run.error_message = error_message
            if complete_baseline and status == "succeeded":
                # the first successful run is the Baseline: from now on new Opportunities notify
                query = uow.profiles.get_query(run.marketplace_query_id)
                if query is None:
                    raise LookupError(f"Marketplace Query {run.marketplace_query_id} not found")
                query.baseline_completed_at = finished_at
            uow.commit()
        return status
