"""Circuit breaker per Marketplace, persisted in app_setting as `circuit:<marketplace>`.

After N consecutive failed runs (setting circuit_failures, default 3), or two of them failing with a
permanent error, the Marketplace is suspended for circuit_suspend_minutes (default 60): scheduling skips
its queries, manual scans included, until then. Failing again after expiry suspends it again at once.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from opportunity_hunter.db.models import AcquisitionRun
from opportunity_hunter.hunter.notifications import read_settings
from opportunity_hunter.ports import RunRepository, SettingsStore, UnitOfWork

PERMANENT_FAILURES_TO_SUSPEND = 2
RECENT_RUNS = 20


@dataclass(frozen=True)
class MarketplaceState:
    marketplace: str
    suspended: bool
    suspended_until: datetime | None
    consecutive_failures: int
    last_run: AcquisitionRun | None


def _key(marketplace: str) -> str:
    return f"circuit:{marketplace}"


def _recent_runs(runs: RunRepository, marketplace: str) -> list[AcquisitionRun]:
    return runs.recent_finished(marketplace, RECENT_RUNS)


def _failure_streak(runs: list[AcquisitionRun]) -> list[str | None]:
    """error_kind of the latest runs that failed, back to the last one that did not."""
    streak = []
    for run in runs:
        if run.status != "failed":
            break
        streak.append(run.error_kind)
    return streak


def suspended_until(settings: SettingsStore, marketplace: str, now: datetime) -> datetime | None:
    value = settings.get(_key(marketplace))
    until = datetime.fromisoformat(value["suspended_until"]) if value else None
    return until if until and until > now else None


def record_outcome(uow: UnitOfWork, marketplace: str, now: datetime) -> datetime | None:
    """Call after a run of this Marketplace finished; returns the new suspension end if it tripped."""
    settings = read_settings(uow.settings)
    streak = _failure_streak(_recent_runs(uow.runs, marketplace))
    tripped = len(streak) >= settings["circuit_failures"] or streak.count("permanent") >= PERMANENT_FAILURES_TO_SUSPEND
    if not tripped:
        if not streak:
            uow.settings.delete(_key(marketplace))
        return None
    until = now + timedelta(minutes=settings["circuit_suspend_minutes"])
    uow.settings.put(_key(marketplace), {"suspended_until": until.isoformat(), "failures": len(streak)})
    return until


def marketplace_states(uow: UnitOfWork, now: datetime) -> list[MarketplaceState]:
    """One entry per Marketplace that has Marketplace Queries."""
    states = []
    for name in uow.profiles.marketplaces():
        runs = _recent_runs(uow.runs, name)
        until = suspended_until(uow.settings, name, now)
        states.append(MarketplaceState(name, until is not None, until, len(_failure_streak(runs)), runs[0] if runs else None))
    return states
