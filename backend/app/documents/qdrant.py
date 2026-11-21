"""Minimal Qdrant REST client (httpx) for chunk vectors.

Qdrant stores only vectors and the minimum indexing payload: ``tenant_id``,
``document_id``, ``version`` and ``generation``. Titles and text stay in PostgreSQL.

Every search carries the authorization filter built by
``app.documents.access.search_filter``: ``tenant_id`` match **and** ``has_id`` over the
caller's currently eligible point ids (ADR 0104). The same filter is attached to every
branch: the dense query, the sparse query, each prefetch and the fused query. Each
outbound request body is passed to ``on_request`` so tests can assert on what was
actually sent (T9), not on what came back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("eaios.qdrant")
DENSE = "dense"
SPARSE = "sparse"


class QdrantError(Exception):
    pass


class CollectionMissing(QdrantError):
    """The collection does not exist (nothing indexed with this embedding space yet)."""


@dataclass(frozen=True, slots=True)
class SparseVector:
    indices: list[int]
    values: list[float]

    def as_json(self) -> dict[str, Any]:
        return {"indices": self.indices, "values": self.values}


@dataclass(frozen=True, slots=True)
class Hit:
    point_id: str
    score: float
    payload: dict[str, Any]


class QdrantClient:
    def __init__(
        self,
        url: str,
        *,
        timeout: float = 10.0,
        on_request: Callable[[str, str, dict[str, Any] | None], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(base_url=url.rstrip("/"), timeout=timeout, transport=transport)
        self.on_request = on_request

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        if self.on_request is not None:
            self.on_request(method, path, body)
        try:
            response = self._http.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise QdrantError(type(exc).__name__) from exc
        if response.status_code == 404 and method == "GET":
            return None
        if response.status_code == 404 and "doesn't exist" in response.text:
            raise CollectionMissing(path)
        if response.status_code >= 400:
            raise QdrantError(f"{method} {path} -> {response.status_code}: {response.text[:200]}")
        return response.json().get("result")

    # -- collections ---------------------------------------------------------------

    def ensure_collection(self, name: str, dense_size: int) -> None:
        existing = self._call("GET", f"/collections/{name}")
        if existing is None:
            self._call(
                "PUT",
                f"/collections/{name}",
                {
                    "vectors": {DENSE: {"size": dense_size, "distance": "Cosine"}},
                    "sparse_vectors": {SPARSE: {}},
                },
            )
            for field in ("tenant_id", "document_id"):
                self._call(
                    "PUT",
                    f"/collections/{name}/index?wait=true",
                    {"field_name": field, "field_schema": "keyword"},
                )
            return
        size = existing["config"]["params"]["vectors"][DENSE]["size"]
        if size != dense_size:
            raise QdrantError(f"collection {name} has dense size {size}, expected {dense_size}")

    def delete_collection(self, name: str) -> None:
        self._call("DELETE", f"/collections/{name}")

    # -- points --------------------------------------------------------------------

    def upsert(self, name: str, points: list[dict[str, Any]]) -> None:
        if points:
            self._call("PUT", f"/collections/{name}/points?wait=true", {"points": points})

    def count(self, name: str, flt: dict[str, Any]) -> int:
        try:
            result = self._call(
                "POST", f"/collections/{name}/points/count", {"filter": flt, "exact": True}
            )
        except CollectionMissing:
            return 0
        return int(result["count"])

    def delete_points(self, name: str, flt: dict[str, Any]) -> None:
        try:
            self._call("POST", f"/collections/{name}/points/delete?wait=true", {"filter": flt})
        except CollectionMissing:
            return

    # -- search --------------------------------------------------------------------

    def query_dense(
        self, name: str, dense: list[float], flt: dict[str, Any], limit: int
    ) -> list[Hit]:
        body = {
            "query": dense,
            "using": DENSE,
            "filter": flt,
            "limit": limit,
            "with_payload": True,
        }
        return self._search(name, body)

    def _search(self, name: str, body: dict[str, Any]) -> list[Hit]:
        """A search against a missing collection finds nothing (and is logged): the
        caller then returns the ordinary no-sources response."""

        try:
            return self._hits(self._call("POST", f"/collections/{name}/points/query", body))
        except CollectionMissing:
            logger.warning("qdrant_collection_missing", extra={"collection": name})
            return []

    def query_hybrid(
        self,
        name: str,
        dense: list[float],
        sparse: SparseVector,
        flt: dict[str, Any],
        limit: int,
        prefetch_limit: int,
    ) -> list[Hit]:
        """Dense and sparse prefetches fused with Reciprocal Rank Fusion. The filter is
        applied to each prefetch *and* to the fusion stage."""

        body = {
            "prefetch": [
                {"query": dense, "using": DENSE, "filter": flt, "limit": prefetch_limit},
                {
                    "query": sparse.as_json(),
                    "using": SPARSE,
                    "filter": flt,
                    "limit": prefetch_limit,
                },
            ],
            "query": {"fusion": "rrf"},
            "filter": flt,
            "limit": limit,
            "with_payload": True,
        }
        return self._search(name, body)

    @staticmethod
    def _hits(result: Any) -> list[Hit]:
        points = result.get("points", []) if isinstance(result, dict) else result or []
        return [
            Hit(
                point_id=str(p["id"]),
                score=float(p.get("score", 0.0)),
                payload=p.get("payload") or {},
            )
            for p in points
        ]
