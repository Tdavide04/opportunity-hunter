# External dependencies

Every repository Opportunity Hunter points to has one file here: where it lives, which commit is pinned, its licence and which extra installs it.

| Repository | Pinned commit | Version | Licence | Extra | Used by |
|---|---|---|---|---|---|
| [subito-scanner](https://github.com/Tdavide04/subito-scanner) | [`2cb97be`](https://github.com/Tdavide04/subito-scanner/commit/2cb97be49a7fd5187a6819c8349d7b26fb72d8d3) | 0.2.0 | MIT | `subito` | `opportunity_hunter.sources.subito` |
| [vinted-scanner](https://github.com/Tdavide04/vinted-scanner) | [`788630e`](https://github.com/Tdavide04/vinted-scanner/commit/788630e54063f9cd9c10f0d4270af77ce039272d) | 0.3.0 | GPL-3.0-only | `vinted` | `opportunity_hunter.sources.vinted` |
| [ebay-scanner](https://github.com/Tdavide04/ebay-scanner) | not yet | — | — | — | planned: waits for an eBay Developer keyset |

The scanners never import Opportunity Hunter, and Opportunity Hunter never imports Personal Collection Hub.

## How the pin works

These files describe; `pyproject.toml` installs. Each scanner is a normal Python dependency from git, pinned to a commit, in its own optional extra. `tests/test_external.py` fails when a file here and `pyproject.toml` disagree, or when a git dependency has no file here.

To move a scanner to a new commit:

1. Change the commit in `pyproject.toml` (`[project.optional-dependencies]`).
2. Change `rev` (and `version` if it changed) in the matching file here and in the table above.
3. `uv sync --all-extras && uv run pytest`.

To add a scanner, add a new extra in `pyproject.toml` and a new file here.

## Private repositories

The scanner repositories are public today. Once they become private, `uv sync` needs read access to them: locally through your git credentials (SSH key or a credential helper), in CI through a fine-grained token with read-only *Contents* access to the scanner repositories, saved as the `DEPS_TOKEN` secret and passed to git before `uv sync`:

    git config --global url."https://x-access-token:${DEPS_TOKEN}@github.com/".insteadOf "https://github.com/"

## Why not git submodules

A submodule would be a second copy of the same pin, and the Python install would still come from `pyproject.toml`. One pin, written in one place and checked against this folder, cannot drift.
