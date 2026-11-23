"""Circuit breaker per Marketplace, persisted in app_setting as `circuit:<marketplace>`.

After N consecutive failed runs (setting circuit_failures, default 3), or two of them failing with a
permanent error, the Marketplace is suspended for circuit_suspend_minutes (default 60): scheduling skips
its queries, manual scans included, until then. Failing again after expiry suspends it again at once.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from opportunity_hunter.db.models import AcquisitionRun, AppSetting, MarketplaceQuery
from opportunity_hunter.hunter.notifications import read_settings

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


def _recent_runs(session: Session, marketplace: str) -> list[AcquisitionRun]:
    return list(session.scalars(
        select(AcquisitionRun).join(MarketplaceQuery)
        .where(MarketplaceQuery.marketplace == marketplace, AcquisitionRun.status != "running")
        .order_by(AcquisitionRun.started_at.desc(), AcquisitionRun.id.desc())
        .limit(RECENT_RUNS)
    ))


def _failure_streak(runs: list[AcquisitionRun]) -> list[str | None]:
    """error_kind of the latest runs that failed, back to the last one that did not."""
    streak = []
    for run in runs:
        if run.status != "failed":
            break
        streak.append(run.error_kind)
    return streak


def suspended_until(session: Session, marketplace: str, now: datetime) -> datetime | None:
    row = session.get(AppSetting, _key(marketplace))
    until = datetime.fromisoformat(row.value["suspended_until"]) if row else None
    return until if until and until > now else None


def record_outcome(session: Session, marketplace: str, now: datetime) -> datetime | None:
    """Call after a run of this Marketplace finished; returns the new suspension end if it tripped."""
    settings = read_settings(session)
    streak = _failure_streak(_recent_runs(session, marketplace))
    tripped = len(streak) >= settings["circuit_failures"] or streak.count("permanent") >= PERMANENT_FAILURES_TO_SUSPEND
    if not tripped:
        if not streak:
            row = session.get(AppSetting, _key(marketplace))
            if row is not None:
                session.delete(row)
        return None
    until = now + timedelta(minutes=settings["circuit_suspend_minutes"])
    session.merge(AppSetting(key=_key(marketplace), value={"suspended_until": until.isoformat(), "failures": len(streak)}))
    return until


def marketplace_states(session: Session, now: datetime) -> list[MarketplaceState]:
    """One entry per Marketplace that has Marketplace Queries."""
    names = session.scalars(select(MarketplaceQuery.marketplace).distinct().order_by(MarketplaceQuery.marketplace))
    states = []
    for name in names:
        runs = _recent_runs(session, name)
        until = suspended_until(session, name, now)
        states.append(MarketplaceState(name, until is not None, until, len(_failure_streak(runs)), runs[0] if runs else None))
    return states
