"""Raw upload storage. Keys start with the tenant (specification section 7):
``<tenant_id>/<source>/<document_id>/<version>/<safe filename>``. The SHA-256 of the
bytes is stored in PostgreSQL and checked on every read. Buckets are never public;
files are read only by the backend.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID


class StorageError(Exception):
    pass


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes) -> None: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...


def object_key(tenant_id: UUID, source: str, document_id: UUID, version: int, filename: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._") or "file"
    return f"{tenant_id}/{source}/{document_id}/{version}/{safe[:120]}"


def read_verified(store: ObjectStore, key: str, sha256: str) -> bytes:
    data = store.get(key)
    if hashlib.sha256(data).hexdigest() != sha256:
        raise StorageError("stored bytes do not match their recorded SHA-256")
    return data


class FileSystemStore:
    """Local directory store for tests and single-process development."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _path(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if self._root not in path.parents:
            raise StorageError("invalid key")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    def get(self, key: str) -> bytes:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError as exc:
            raise StorageError("object not found") from exc

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Store:
    """S3-compatible store (SeaweedFS in Compose) through boto3."""

    def __init__(self, endpoint_url: str, bucket: str, access_key: str, secret_key: str) -> None:
        import boto3  # noqa: PLC0415 - optional at import time for tests
        from botocore.config import Config  # noqa: PLC0415

        self._bucket = bucket
        self._client: Any = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name="us-east-1",
            config=Config(
                connect_timeout=3,
                read_timeout=15,
                retries={"max_attempts": 2},
                s3={"addressing_style": "path"},
            ),
        )
        self._ensured = False

    def _ensure_bucket(self) -> None:
        if self._ensured:
            return
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            self._client.create_bucket(Bucket=self._bucket)
        self._ensured = True

    def put(self, key: str, data: bytes) -> None:
        try:
            self._ensure_bucket()
            self._client.put_object(Bucket=self._bucket, Key=key, Body=data)
        except Exception as exc:
            raise StorageError(type(exc).__name__) from exc

    def get(self, key: str) -> bytes:
        try:
            self._ensure_bucket()
            body = self._client.get_object(Bucket=self._bucket, Key=key)["Body"]
            return bytes(body.read())
        except Exception as exc:
            raise StorageError(type(exc).__name__) from exc

    def delete(self, key: str) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise StorageError(type(exc).__name__) from exc
