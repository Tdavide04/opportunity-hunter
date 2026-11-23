"""Standalone CLI for Opportunity Hunter."""
import argparse
import json
import logging
import sys
from dataclasses import asdict
from typing import Any

from opportunity_hunter import __version__
from opportunity_hunter.config import OpportunityHunterConfig
from opportunity_hunter.db.store import SqlAlchemyStore
from opportunity_hunter.engine import OpportunityHunter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opportunity-hunter", description="Run the Opportunity Hunter engine.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-db", help="create tables in a new, empty standalone database")
    init.add_argument("--yes-empty-db", action="store_true", help="confirm this is a new database with no user tables")
    sub.add_parser("run-due", help="run scheduled and manually requested marketplace queries")
    run = sub.add_parser("run-query", help="run one stored Marketplace Query")
    run.add_argument("query_id", type=int)
    run.add_argument("--dry-run", action="store_true")
    sub.add_parser("dispatch", help="deliver pending notifications")
    preview = sub.add_parser("preview", help="fetch one page without persistence")
    preview.add_argument("marketplace")
    preview.add_argument("params_json", help="JSON object with source query parameters")
    preview.add_argument("--max-items", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    config = OpportunityHunterConfig.from_env()
    logging.basicConfig(level=config.log_level.upper(), stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    if args.command == "init-db":
        if not args.yes_empty_db:
            print("Refusing: pass --yes-empty-db only for a new, empty PostgreSQL database.", file=sys.stderr)
            return 2
        if not config.database_url:
            print("OH_DATABASE_URL is required.", file=sys.stderr)
            return 2
        store = SqlAlchemyStore(database_url=config.database_url)
        try:
            store.initialize_fresh_database()
        finally:
            store.close()
        print("Fresh standalone schema created. This is not an Alembic migration.")
        return 0

    try:
        hunter = OpportunityHunter(config=config)
    except (ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    try:
        if args.command == "run-due":
            _json(asdict(hunter.run_due()))
        elif args.command == "run-query":
            _json(asdict(hunter.run_query(args.query_id, dry_run=args.dry_run)))
        elif args.command == "dispatch":
            _json(hunter.dispatch())
        elif args.command == "preview":
            if args.max_items < 1:
                print("--max-items must be at least 1", file=sys.stderr)
                return 2
            params = json.loads(args.params_json)
            if not isinstance(params, dict):
                raise ValueError("params_json must decode to an object")
            _json([asdict(item) for item in hunter.preview(args.marketplace, params, args.max_items)])
        return 0
    except (ValueError, KeyError, LookupError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        hunter.close()


def _json(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
