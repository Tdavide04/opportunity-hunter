"""Standalone CLI for Opportunity Hunter."""
import argparse
import json
import logging
import sys
from dataclasses import asdict
from typing import Any

from opportunity_hunter import __version__
from opportunity_hunter.adapter import PermanentMarketplaceError, TransientMarketplaceError
from opportunity_hunter.config import OpportunityHunterConfig
from opportunity_hunter.db.store import SqlAlchemyStore
from opportunity_hunter.engine import OpportunityHunter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="opportunity-hunter", description="Run the Opportunity Hunter engine.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init-db", help="create tables in a new, empty standalone database")
    init.add_argument("--yes-empty-db", action="store_true", help="confirm this is a new database with no user tables")
    sub.add_parser("upgrade-db", help="apply pending Opportunity Hunter migrations (new or already adopted database)")
    adopt = sub.add_parser("adopt-db", help="stamp existing Hunter tables after checking them against the baseline")
    adopt.add_argument("--yes-existing-db", action="store_true", help="confirm you have a tested backup of this database")
    sub.add_parser("check-db", help="print the migration revision and how the Hunter tables differ from the baseline")
    sub.add_parser("run-due", help="run scheduled and manually requested marketplace queries")
    run = sub.add_parser("run-query", help="run one stored Marketplace Query")
    run.add_argument("query_id", type=int)
    run.add_argument("--dry-run", action="store_true")
    sub.add_parser("dispatch", help="deliver pending notifications")
    sub.add_parser("rescore", help="score every matched Match again with the current scoring rules")
    preview = sub.add_parser("preview", help="fetch one page without persistence")
    preview.add_argument("marketplace")
    preview.add_argument("params_json", help="JSON object with source query parameters")
    preview.add_argument("--max-items", type=int, default=20)
    return parser


def _database_command(args: argparse.Namespace, database_url: str | None) -> int:
    from sqlalchemy import create_engine

    from opportunity_hunter import migrations

    if not database_url:
        print("OH_DATABASE_URL is required.", file=sys.stderr)
        return 2
    if args.command == "adopt-db" and not args.yes_existing_db:
        print("Refusing: pass --yes-existing-db only after a backup and a test restore of this database.", file=sys.stderr)
        return 2
    engine = create_engine(database_url, connect_args={"connect_timeout": 3})
    try:
        if args.command == "upgrade-db":
            migrations.upgrade(engine)
            print("Opportunity Hunter migrations applied.")
        elif args.command == "adopt-db":
            result = migrations.adopt_existing(engine)
            _json(asdict(result))
        else:
            with engine.connect() as connection:
                revision = migrations.current_revision(connection)
                differences = migrations.schema_differences(connection) if revision is None else []
                connection.rollback()
            _json({"revision": revision, "differences_from_baseline": differences})
    except migrations.MigrationRefused as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        engine.dispose()
    return 0


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
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            store.close()
        print("Fresh standalone schema created at the latest Opportunity Hunter migration.")
        return 0

    if args.command in ("upgrade-db", "adopt-db", "check-db"):
        return _database_command(args, config.database_url)

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
        elif args.command == "rescore":
            _json(hunter.rescore())
        elif args.command == "preview":
            if args.max_items < 1:
                print("--max-items must be at least 1", file=sys.stderr)
                return 2
            params = json.loads(args.params_json)
            if not isinstance(params, dict):
                raise ValueError("params_json must decode to an object")
            _json([asdict(item) for item in hunter.preview(args.marketplace, params, args.max_items)])
        return 0
    except (ValueError, KeyError, LookupError, RuntimeError, TransientMarketplaceError, PermanentMarketplaceError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        hunter.close()


def _json(value: Any) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
