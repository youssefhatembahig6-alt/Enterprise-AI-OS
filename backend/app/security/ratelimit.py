"""Login admission control backed by Redis (fixed window per key).

Each attempt atomically *reserves* one unit against both its identity key
(SHA-256 of tenant + email) and its client key (SHA-256 of the client address)
before any password hashing happens. A Lua script checks both limits and
increments both counters in one step, so concurrent requests cannot all observe
the same remaining allowance. A reservation is a provisional failure:

* verified failure  -> kept (it counts toward the limit)
* success           -> identity counter cleared, client unit refunded
* error / busy      -> both units refunded

Keys are digests, so Redis never stores emails or client addresses in clear. If
Redis is unavailable the login path fails closed (503).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError


class RateLimiterUnavailable(Exception):
    pass


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


_RESERVE = """
local id = tonumber(redis.call('GET', KEYS[1]) or '0')
local client = tonumber(redis.call('GET', KEYS[2]) or '0')
local function wait(key)
  local ttl = redis.call('TTL', key)
  if ttl < 1 then return 1 end
  return ttl
end
if id >= tonumber(ARGV[1]) then return wait(KEYS[1]) end
if client >= tonumber(ARGV[2]) then return wait(KEYS[2]) end
redis.call('INCR', KEYS[1])
redis.call('EXPIRE', KEYS[1], ARGV[3], 'NX')
redis.call('INCR', KEYS[2])
redis.call('EXPIRE', KEYS[2], ARGV[3], 'NX')
return 0
"""

_REFUND = """
for _, key in ipairs(KEYS) do
  local value = tonumber(redis.call('GET', key) or '0')
  if value > 0 then redis.call('DECR', key) end
end
return 0
"""


@dataclass(frozen=True, slots=True)
class Reservation:
    identity_key: str
    client_key: str


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
        self._reserve = redis.register_script(_RESERVE)
        self._refund = redis.register_script(_REFUND)

    def _keys(self, tenant_slug: str, email: str, client: str) -> Reservation:
        return Reservation(
            identity_key=f"{self._prefix}:login:id:{_digest(tenant_slug, email.lower())}",
            client_key=f"{self._prefix}:login:client:{_digest(client)}",
        )

    def reserve(self, tenant_slug: str, email: str, client: str) -> Reservation | int:
        """Atomically admit one attempt. Returns a Reservation, or seconds to wait."""

        keys = self._keys(tenant_slug, email, client)
        try:
            wait = int(
                self._reserve(
                    keys=[keys.identity_key, keys.client_key],
                    args=[self._max_identity, self._max_client, self._window],
                )
            )
        except RedisError as exc:
            raise RateLimiterUnavailable from exc
        return keys if wait == 0 else wait

    def settle_success(self, reservation: Reservation) -> None:
        try:
            self._redis.delete(reservation.identity_key)
            self._refund(keys=[reservation.client_key])
        except RedisError as exc:
            raise RateLimiterUnavailable from exc

    def release(self, reservation: Reservation) -> None:
        """Refund an attempt that ended without a credential verdict."""

        try:
            self._refund(keys=[reservation.identity_key, reservation.client_key])
        except RedisError as exc:
            raise RateLimiterUnavailable from exc
