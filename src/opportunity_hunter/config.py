"""Environment configuration with an OH_ prefix; independent from PCH settings."""
import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class OpportunityHunterConfig:
    database_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    notification_max_attempts: int = 5
    log_level: str = "INFO"

    def __post_init__(self) -> None:
        if (self.telegram_bot_token is None) != (self.telegram_chat_id is None):
            raise ValueError("set OH_TELEGRAM_BOT_TOKEN and OH_TELEGRAM_CHAT_ID together, or neither")
        if self.notification_max_attempts < 1:
            raise ValueError("notification_max_attempts must be at least 1")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "OpportunityHunterConfig":
        values = os.environ if environ is None else environ
        try:
            max_attempts = int(values.get("OH_NOTIFICATION_MAX_ATTEMPTS", "5"))
        except ValueError as exc:
            raise ValueError("OH_NOTIFICATION_MAX_ATTEMPTS must be an integer") from exc
        return cls(
            database_url=values.get("OH_DATABASE_URL") or None,
            telegram_bot_token=values.get("OH_TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=values.get("OH_TELEGRAM_CHAT_ID") or None,
            notification_max_attempts=max_attempts,
            log_level=values.get("OH_LOG_LEVEL", "INFO"),
        )
