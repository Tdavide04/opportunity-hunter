from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class OutgoingMessage:
    """A Notification already formatted neutrally; each Channel renders it in its own format."""

    title: str
    price: str
    location: str | None
    score: int
    url: str
    image_url: str | None
    search_profile: str
    note: str | None = None  # e.g. the Price Drop, shown first


class ChannelError(Exception):
    """Delivery failed; the message must not contain secrets (it is stored on the Notification)."""


class NotificationChannel(Protocol):
    name: str

    def send(self, message: OutgoingMessage) -> None:
        """Deliver the message or raise ChannelError."""
        ...
