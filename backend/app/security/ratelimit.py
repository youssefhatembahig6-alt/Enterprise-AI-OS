"""Login admission control backed by Redis (fixed window per key).

Each limiter key (SHA-256 of tenant + email for the identity, SHA-256 of the
client address for the client) is a Redis hash that lives for one window:

* ``win``       random id of this window, set when the hash is created
* ``failed``    settled credential failures in this window
* ``r:<id>``    one field per reservation still in progress

The usage of a key is ``failed`` plus the number of pending reservation fields.
Every attempt atomically reserves one field on both keys *before* any password
hashing; a Lua script checks both limits and adds both fields in one step, so
concurrent requests cannot share the same remaining allowance. A
:class:`Reservation` remembers its id and the ``win`` of each key it was
admitted into, and every settlement is a no-op unless that exact field still
exists in that same window. Settlement therefore applies at most once and never
touches a later window or another attempt's reservation:

* verified failure -> the field becomes one ``failed`` count
* success          -> the field is removed; on the identity key earlier settled
                      failures are cleared, but other pending reservations stay
* error / busy     -> the field is removed (refund)

If a window expires before settlement, the reservation disappears with it and a
late settlement does nothing. Keys are digests, so Redis never stores emails or
client addresses in clear. If Redis is unavailable, login fails closed (503).
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from redis import Redis
from redis.exceptions import RedisError


class RateLimiterUnavailable(Exception):
    pass


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


# Key layout version. v2 keys are hashes; the earlier v1 layout used plain string
# counters under ``login:id:`` / ``login:client:``. Versioning keeps leftover v1
# keys (which expire on their own) from colliding with v2 hashes after an upgrade.
KEY_VERSION = "v2"

# Fields in every window hash besides the reservation fields.
_FIXED_FIELDS = 2

# KEYS: identity, client. ARGV: max_identity, max_client, window_seconds,
# reservation field, new window id for identity, new window id for client.
# Returns {0, identity_win, client_win} when admitted, or {retry_after} when refused.
_RESERVE = """
local limits = {tonumber(ARGV[1]), tonumber(ARGV[2])}
for i = 1, 2 do
  local key = KEYS[i]
  if redis.call('EXISTS', key) == 1 then
    local used = tonumber(redis.call('HGET', key, 'failed')) + redis.call('HLEN', key) - 2
    if used >= limits[i] then
      local ttl = redis.call('TTL', key)
      if ttl < 1 then ttl = 1 end
      return {ttl}
    end
  end
end
local wins = {}
for i = 1, 2 do
  local key = KEYS[i]
  if redis.call('EXISTS', key) == 0 then
    redis.call('HSET', key, 'win', ARGV[4 + i], 'failed', 0)
    redis.call('EXPIRE', key, ARGV[3])
  end
  redis.call('HSET', key, ARGV[4], 1)
  wins[i] = redis.call('HGET', key, 'win')
end
return {0, wins[1], wins[2]}
"""

# Settle one reservation field on one key, only inside its original window.
# KEYS: key. ARGV: window id, reservation field, mode ("fail" | "refund" | "success").
# Returns 1 if the field existed and was settled, else 0.
_SETTLE = """
if redis.call('HGET', KEYS[1], 'win') ~= ARGV[1] then return 0 end
if redis.call('HDEL', KEYS[1], ARGV[2]) == 0 then return 0 end
if ARGV[3] == 'fail' then
  redis.call('HINCRBY', KEYS[1], 'failed', 1)
elseif ARGV[3] == 'success' then
  redis.call('HSET', KEYS[1], 'failed', 0)
end
return 1
"""


@dataclass(frozen=True, slots=True)
class Reservation:
    identity_key: str
    client_key: str
    field: str
    identity_window: str
    client_window: str


@dataclass(frozen=True, slots=True)
class Usage:
    identity: int
    client: int


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
        self._settle = redis.register_script(_SETTLE)

    def _keys(self, tenant_slug: str, email: str, client: str) -> tuple[str, str]:
        return (
            f"{self._prefix}:login:{KEY_VERSION}:id:{_digest(tenant_slug, email.lower())}",
            f"{self._prefix}:login:{KEY_VERSION}:client:{_digest(client)}",
        )

    def reserve(self, tenant_slug: str, email: str, client: str) -> Reservation | int:
        """Atomically admit one attempt. Returns a Reservation, or seconds to wait."""

        identity_key, client_key = self._keys(tenant_slug, email, client)
        field = f"r:{secrets.token_hex(16)}"
        try:
            result = self._reserve(
                keys=[identity_key, client_key],
                args=[
                    self._max_identity,
                    self._max_client,
                    self._window,
                    field,
                    secrets.token_hex(8),
                    secrets.token_hex(8),
                ],
            )
        except RedisError as exc:
            raise RateLimiterUnavailable from exc
        if int(result[0]) != 0:
            return int(result[0])
        return Reservation(
            identity_key=identity_key,
            client_key=client_key,
            field=field,
            identity_window=_text(result[1]),
            client_window=_text(result[2]),
        )

    def _apply(self, reservation: Reservation, identity_mode: str, client_mode: str) -> None:
        try:
            pipe = self._redis.pipeline(transaction=True)
            self._settle(
                keys=[reservation.identity_key],
                args=[reservation.identity_window, reservation.field, identity_mode],
                client=pipe,
            )
            self._settle(
                keys=[reservation.client_key],
                args=[reservation.client_window, reservation.field, client_mode],
                client=pipe,
            )
            pipe.execute()  # type: ignore[no-untyped-call]
        except RedisError as exc:
            raise RateLimiterUnavailable from exc

    def settle_failure(self, reservation: Reservation) -> None:
        """A verified credential failure: counts toward both limits in its window."""

        self._apply(reservation, "fail", "fail")

    def settle_success(self, reservation: Reservation) -> None:
        """Clear earlier settled identity failures; keep other attempts' reservations."""

        self._apply(reservation, "success", "refund")

    def release(self, reservation: Reservation) -> None:
        """Refund an attempt that ended without a credential verdict (at most once)."""

        self._apply(reservation, "refund", "refund")

    def usage(self, tenant_slug: str, email: str, client: str) -> Usage:
        """Current window usage (settled failures + pending reservations) per key."""

        counts = []
        try:
            for key in self._keys(tenant_slug, email, client):
                failed = self._redis.hget(key, "failed")
                counts.append(
                    0
                    if failed is None
                    else int(failed) + int(self._redis.hlen(key)) - _FIXED_FIELDS  # type: ignore[arg-type]
                )
        except RedisError as exc:
            raise RateLimiterUnavailable from exc
        return Usage(identity=counts[0], client=counts[1])


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
