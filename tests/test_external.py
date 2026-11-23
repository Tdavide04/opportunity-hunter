"""The external/ manifest must match the git dependencies pinned in pyproject.toml."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GIT_REQUIREMENT = re.compile(r"^(?P<name>[\w.-]+) @ git\+(?P<repository>https://\S+?)@(?P<rev>[0-9a-f]{40})$")


def _pinned() -> dict[str, tuple[str, str, str]]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pins = {}
    for extra, requirements in pyproject["project"]["optional-dependencies"].items():
        for requirement in requirements:
            match = GIT_REQUIREMENT.match(requirement)
            if match:
                pins[match["name"]] = (match["repository"], match["rev"], extra)
    return pins


def _manifest() -> dict[str, dict[str, str]]:
    return {
        path.stem: tomllib.loads(path.read_text()) for path in sorted((ROOT / "external").glob("*.toml"))
    }


def test_every_git_dependency_has_a_manifest_file() -> None:
    assert set(_pinned()) == set(_manifest())


def test_manifest_matches_the_pins() -> None:
    manifest = _manifest()
    for name, (repository, rev, extra) in _pinned().items():
        entry = manifest[name]
        assert entry["name"] == name
        assert entry["repository"] == repository
        assert entry["rev"] == rev
        assert entry["extra"] == extra


def test_readme_table_names_each_pinned_commit() -> None:
    readme = (ROOT / "external" / "README.md").read_text()
    for name, (_, rev, _) in _pinned().items():
        assert f"[`{rev[:7]}`]" in readme, name
