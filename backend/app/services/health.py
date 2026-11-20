"""Liveness and readiness checks.

Readiness reports only component names and ``ok``/``unavailable``; error text
(which may contain hosts or credentials) is never returned.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx
from alembic.config import Config
from alembic.script import ScriptDirectory
from redis import Redis
from sqlalchemy import Engine, text

logger = logging.getLogger("eaios.health")

_BACKEND_ROOT = Path(__file__).resolve().parents[2]


def expected_schema_head() -> str | None:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_BACKEND_ROOT / "app/db/migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    ready: bool
    checks: dict[str, str]


class ReadinessChecker:
    def __init__(
        self,
        *,
        engine: Engine,
        redis: Redis,
        qdrant_url: str,
        s3_endpoint_url: str,
        timeout: float,
    ) -> None:
        self._engine = engine
        self._redis = redis
        self._qdrant_url = qdrant_url.rstrip("/")
        self._s3_url = s3_endpoint_url.rstrip("/")
        self._timeout = timeout
        self._schema_head = expected_schema_head()

    def _postgres(self) -> None:
        with self._engine.connect() as conn:
            version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if version != self._schema_head:
            raise RuntimeError("schema_not_at_head")

    def _redis_ping(self) -> None:
        if not self._redis.ping():
            raise RuntimeError("redis_ping_failed")

    def _qdrant(self) -> None:
        response = httpx.get(f"{self._qdrant_url}/readyz", timeout=self._timeout)
        response.raise_for_status()

    def _storage(self) -> None:
        # Any non-5xx answer (an unauthenticated S3 request yields 403) means serving.
        response = httpx.get(f"{self._s3_url}/", timeout=self._timeout)
        if response.status_code >= 500:
            raise RuntimeError("storage_error_status")

    def check(self) -> ReadinessReport:
        probes: dict[str, Callable[[], None]] = {
            "postgres": self._postgres,
            "redis": self._redis_ping,
            "qdrant": self._qdrant,
            "storage": self._storage,
        }
        results: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=len(probes)) as pool:
            futures = {name: pool.submit(probe) for name, probe in probes.items()}
            for name, future in futures.items():
                try:
                    future.result(timeout=self._timeout + 3)
                    results[name] = "ok"
                except Exception as exc:
                    logger.warning(
                        "readiness_check_failed",
                        extra={"component": name, "error_type": type(exc).__name__},
                    )
                    results[name] = "unavailable"
        return ReadinessReport(ready=all(v == "ok" for v in results.values()), checks=results)
