import pytest

from opportunity_hunter.config import OpportunityHunterConfig


def test_config_from_oh_environment():
    config = OpportunityHunterConfig.from_env({
        "OH_DATABASE_URL": "postgresql+psycopg://u:p@localhost/db",
        "OH_TELEGRAM_BOT_TOKEN": "token",
        "OH_TELEGRAM_CHAT_ID": "123",
        "OH_NOTIFICATION_MAX_ATTEMPTS": "7",
        "OH_LOG_LEVEL": "DEBUG",
    })
    assert config.database_url.endswith("/db")
    assert config.notification_max_attempts == 7
    assert config.log_level == "DEBUG"


def test_telegram_settings_must_be_paired():
    with pytest.raises(ValueError, match="together"):
        OpportunityHunterConfig(telegram_bot_token="token")


def test_attempts_must_be_positive():
    with pytest.raises(ValueError, match="at least 1"):
        OpportunityHunterConfig(notification_max_attempts=0)
