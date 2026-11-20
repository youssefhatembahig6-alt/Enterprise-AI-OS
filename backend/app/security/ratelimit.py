"""Login failure rate limiting backed by Redis (fixed window per key).

Keys are SHA-256 digests, so Redis never stores emails or client addresses in clear.
If Redis is unavailable the login path fails closed (503) rather than allowing
unlimited guessing.
"""

from __future__ import annotations

import hashlib

from redis import Redis
from redis.exceptions import RedisError


class RateLimiterUnavailable(Exception):
    pass


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class LoginRateLimiter:
    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str,
        max_per_identity: int,
        max_per_client: int,
        window_seconds: int,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._max_identity = max_per_identity
        self._max_client = max_per_client
        self._window = window_seconds

    def _keys(self, tenant_slug: str, email: str, client: str) -> tuple[str, str]:
        return (
            f"{self._prefix}:login:id:{_digest(tenant_slug, email.lower())}",
            f"{self._prefix}:login:client:{_digest(client)}",
        )

    def retry_after(self, tenant_slug: str, email: str, client: str) -> int | None:
        """Seconds until another attempt is allowed, or None if allowed now."""

        id_key, client_key = self._keys(tenant_slug, email, client)
        try:
            pipe = self._redis.pipeline()
            pipe.get(id_key)
            pipe.ttl(id_key)
            pipe.get(client_key)
            pipe.ttl(client_key)
            id_count, id_ttl, client_count, client_ttl = pipe.execute()  # type: ignore[no-untyped-call]
        except RedisError as exc:
            raise RateLimiterUnavailable from exc
        if id_count is not None and int(id_count) >= self._max_identity:
            return max(int(id_ttl), 1)
        if client_count is not None and int(client_count) >= self._max_client:
            return max(int(client_ttl), 1)
        return None

    def record_failure(self, tenant_slug: str, email: str, client: str) -> None:
        try:
            pipe = self._redis.pipeline()
            for key in self._keys(tenant_slug, email, client):
                pipe.incr(key)
                pipe.expire(key, self._window, nx=True)
            pipe.execute()  # type: ignore[no-untyped-call]
        except RedisError as exc:
            raise RateLimiterUnavailable from exc

    def reset_identity(self, tenant_slug: str, email: str, client: str) -> None:
        try:
            self._redis.delete(self._keys(tenant_slug, email, client)[0])
        except RedisError as exc:
            raise RateLimiterUnavailable from exc
