"""Alembic environment. Runs only with the privileged migration (owner) role."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

config = context.config


def _url() -> str:
    url = config.attributes.get("url") or os.environ.get("MIGRATION_DATABASE_URL")
    if not url:
        raise RuntimeError("MIGRATION_DATABASE_URL is required to run migrations")
    return str(url)


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise RuntimeError("Offline SQL generation is not supported for this project")
run_migrations_online()
