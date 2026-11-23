"""Deterministic fixture source and recording notification channel; no network access."""
import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from opportunity_hunter.adapter import (
    InvalidParams,
    Page,
    PermanentMarketplaceError,
    Rejected,
    TransientMarketplaceError,
)
from opportunity_hunter.channels.base import ChannelError, OutgoingMessage
from opportunity_hunter.db.store import SqlAlchemyStore
from opportunity_hunter.domain import ListingCandidate
from opportunity_hunter.ports import UnitOfWorkFactory, WorkerLock

_FIXTURE_NAME = re.compile(r"[a-z0-9_-]{1,64}")


def sqlalchemy_ports(session_factory: Any) -> tuple[UnitOfWorkFactory, WorkerLock]:
    """The Pipeline's persistence ports on a host-supplied session factory, as PCH will hand them over."""
    store = SqlAlchemyStore(session_factory=session_factory)
    return store.unit_of_work, store.worker_lock


class FixtureAdapter:
    """Serve canned pages from <root>/<fixture>.json using the same contract as real scanners."""

    marketplace = "fixture"
    capabilities = frozenset({"description", "images", "location", "seller", "condition", "published_at"})

    def __init__(self, root: Path):
        self.root = root

    def validate_params(self, raw: dict[str, Any]) -> dict[str, Any]:
        errors = [((key,), "unknown parameter") for key in raw if key != "fixture"]
        name = raw.get("fixture")
        if not isinstance(name, str) or not _FIXTURE_NAME.fullmatch(name):
            errors.append((("fixture",), "required: 1-64 characters among a-z, 0-9, '_' and '-'"))
        if errors:
            raise InvalidParams(errors)
        return {"fixture": name}

    def fetch_page(self, params: dict[str, Any], cursor: Any | None) -> Page:
        path = self.root / f"{self.validate_params(params)['fixture']}.json"
        try:
            pages = json.loads(path.read_text())["pages"]
        except (OSError, ValueError, KeyError) as exc:
            raise PermanentMarketplaceError(f"fixture {path.name} unreadable: {exc}") from exc
        index = cursor or 0
        if index >= len(pages):
            return Page(candidates=[])
        page = pages[index]
        if page.get("error") == "transient":
            raise TransientMarketplaceError(f"simulated transient error on page {index}")
        if page.get("error") == "permanent":
            raise PermanentMarketplaceError(f"simulated permanent error on page {index}")
        candidates = []
        rejected = []
        for raw in page.get("items", []):
            try:
                candidates.append(self._normalize(raw))
            except (ValueError, TypeError, InvalidOperation) as exc:
                rejected.append(Rejected(external_id=raw.get("id") if isinstance(raw, dict) else None, reason=str(exc)))
        next_cursor = index + 1 if index + 1 < len(pages) else None
        return Page(candidates=candidates, rejected=rejected, next_cursor=next_cursor, newest_first=bool(page.get("newest_first")))

    @staticmethod
    def _normalize(raw: Any) -> ListingCandidate:
        if not isinstance(raw, dict):
            raise ValueError("item is not a JSON object")
        price = raw.get("price")
        published = raw.get("published_at")
        return ListingCandidate(
            external_id=raw.get("id"),
            title=raw.get("title"),
            url=raw.get("url"),
            price_status=raw.get("price_status", "priced" if price is not None else "missing"),
            price_amount=Decimal(price) if price is not None else None,
            currency=raw.get("currency"),
            description=raw.get("description"),
            images=list(raw.get("images", [])),
            location=raw.get("location"),
            seller_external_id=raw.get("seller_id"),
            seller_name=raw.get("seller_name"),
            condition=raw.get("condition"),
            condition_raw=raw.get("condition_raw"),
            published_at=datetime.fromisoformat(published) if published else None,
            raw_payload=raw,
        )


class RecordingChannel:
    """Keep delivered messages and allow deterministic failures for outbox retry tests."""

    name = "recording"

    def __init__(self) -> None:
        self.sent: list[OutgoingMessage] = []
        self._failures: list[str] = []

    def fail_next(self, times: int, error: str = "simulated failure") -> None:
        self._failures = [error] * times

    def send(self, message: OutgoingMessage) -> None:
        if self._failures:
            raise ChannelError(self._failures.pop(0))
        self.sent.append(message)
