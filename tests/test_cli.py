"""The standalone CLI answers with a message and an exit code, never a traceback."""

import json
import os

import pytest

from opportunity_hunter import cli
from opportunity_hunter import engine as engine_module
from opportunity_hunter.adapter import TransientMarketplaceError


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert "opportunity-hunter" in capsys.readouterr().out


def test_init_db_needs_the_confirmation_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["init-db"]) == 2
    assert "--yes-empty-db" in capsys.readouterr().err


def test_adopt_db_needs_the_confirmation_flag(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OH_DATABASE_URL", "postgresql+psycopg://unused@localhost/unused")
    assert cli.main(["adopt-db"]) == 2
    assert "--yes-existing-db" in capsys.readouterr().err


@pytest.mark.skipif(not os.getenv("OH_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set")
def test_init_db_on_a_non_empty_database_is_a_clean_refusal(
    engine: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OH_DATABASE_URL", os.environ["OH_TEST_DATABASE_URL"])
    assert cli.main(["init-db", "--yes-empty-db"]) == 1
    assert "non-empty" in capsys.readouterr().err


class _Unreachable:
    def validate_params(self, raw: dict[str, object]) -> dict[str, object]:
        return raw

    def fetch_page(self, params: dict[str, object], cursor: object) -> object:
        raise TransientMarketplaceError("subito: connection error after 4 attempts")


@pytest.mark.skipif(not os.getenv("OH_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set")
def test_preview_reports_an_unreachable_marketplace(
    engine: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("OH_DATABASE_URL", os.environ["OH_TEST_DATABASE_URL"])
    monkeypatch.setattr(engine_module, "default_sources", lambda: {"subito": _Unreachable()})
    assert cli.main(["preview", "subito", '{"search_text": "zelda"}']) == 1
    assert "connection error" in capsys.readouterr().err



@pytest.mark.skipif(not os.getenv("OH_TEST_DATABASE_URL"), reason="PostgreSQL integration URL not set")
def test_rescore_reports_the_matches_scored_per_profile(
    engine: object, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from sqlalchemy.orm import Session

    from opportunity_hunter.db.models import SearchProfile

    with Session(engine) as session:  # type: ignore[arg-type]
        session.add(SearchProfile(name="DS", keywords=["Nintendo DS"], currency="EUR", score_threshold=0))
        session.commit()
    monkeypatch.setenv("OH_DATABASE_URL", os.environ["OH_TEST_DATABASE_URL"])
    monkeypatch.setattr(engine_module, "default_sources", dict)  # the real ones would import the scanner extras
    assert cli.main(["rescore"]) == 0
    assert list(json.loads(capsys.readouterr().out).values()) == [0]
