"""Routes a sync request to the service for the source's connector kind (M5).

Drive and GitHub are pull connectors with fenced sync runs. A local folder is pushed by
the desktop app, so it has nothing to pull: a sync request for it is ``skipped``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Engine, text

from app.connectors.fencing import SyncOutcome
from app.db.engine import tenant_transaction


class ConnectorSyncRouter:
    def __init__(self, engine: Engine, services: dict[str, Any]) -> None:
        self._engine = engine
        self._services = services

    def kind(self, tenant_id: UUID, source_id: UUID) -> str | None:
        with tenant_transaction(self._engine, tenant_id) as conn:
            value = conn.execute(
                text("SELECT kind FROM connector_sources WHERE id = :s"), {"s": source_id}
            ).scalar_one_or_none()
        return str(value) if value else None

    def sync(self, tenant_id: UUID, source_id: UUID, **kw: Any) -> SyncOutcome:
        kind = self.kind(tenant_id, source_id)
        service = self._services.get(kind or "")
        if service is None:
            return SyncOutcome("skipped", run_id=kw.get("run_id"), error_code="not_pull_connector")
        outcome: SyncOutcome = service.sync(tenant_id, source_id, **kw)
        return outcome
