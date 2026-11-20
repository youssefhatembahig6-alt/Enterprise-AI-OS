"""Developer/operator commands: migrate, seed-dev, export-openapi.

Usage: python -m app.cli <command> [options]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, pool

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _owner_url() -> str:
    url = os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        sys.exit("MIGRATION_DATABASE_URL (privileged migration role) is required")
    return url


def cmd_migrate(_: argparse.Namespace) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "app/db/migrations"))
    config.attributes["url"] = _owner_url()
    command.upgrade(config, "head")
    print("migrations: at head")


def cmd_seed_dev(args: argparse.Namespace) -> None:
    from app.fixtures import load_foundation
    from app.seed import SeedRefused, seed_foundation, write_credentials

    environment = os.environ.get("EAIOS_ENV", "development")
    engine = create_engine(_owner_url(), poolclass=pool.NullPool)
    try:
        result = seed_foundation(
            engine,
            load_foundation(),
            environment=environment,
            reset_passwords=args.reset_passwords,
        )
    except SeedRefused as exc:
        sys.exit(f"refused: {exc}")
    finally:
        engine.dispose()
    written = write_credentials(Path(args.credentials_file), result.new_credentials)
    print("seed: inserted rows", json.dumps(result.created, sort_keys=True))
    if written:
        print(
            f"seed: wrote {written} new development password(s) to {args.credentials_file}"
            " (owner-only permissions; passwords are not printed)"
        )
    else:
        print("seed: no new users; existing passwords unchanged")


def cmd_export_openapi(args: argparse.Namespace) -> None:
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        DATABASE_URL="postgresql+psycopg://openapi@localhost/none",
        REDIS_URL="redis://localhost:6379/0",
        QDRANT_URL="http://localhost:6333",
        S3_ENDPOINT_URL="http://localhost:8333",
        JWT_SECRET="openapi-export-only-not-a-real-secret-000",  # noqa: S106
    )
    schema = create_app(settings).openapi()
    Path(args.output).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n")
    print(f"openapi: wrote {args.output}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="alembic upgrade head (owner role)").set_defaults(
        func=cmd_migrate
    )
    seed = sub.add_parser("seed-dev", help="idempotent development fixture seed (owner role)")
    seed.add_argument("--credentials-file", default=str(BACKEND_ROOT / "var/dev-credentials.json"))
    seed.add_argument(
        "--reset-passwords",
        action="store_true",
        help="regenerate passwords for fixture users (development only)",
    )
    seed.set_defaults(func=cmd_seed_dev)
    export = sub.add_parser("export-openapi", help="write the OpenAPI schema")
    export.add_argument("--output", default=str(BACKEND_ROOT.parent / "docs/openapi.json"))
    export.set_defaults(func=cmd_export_openapi)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
