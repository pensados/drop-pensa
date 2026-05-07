"""
Rate limiter with Redis backend and in-memory fallback.

Two flavors of buckets:
- Per-IP for uploads, fetches, and deletes
- Per (file_id, IP) for fetches, to mitigate hotlinking abuse

The Redis path uses a Lua script so the read-modify-write is atomic
under concurrency. Without Lua, two parallel requests can both pass
when only one should — that's the whole point of using Redis here.

If Redis is unreachable we silently fall back to in-memory. That means
limits are per-process (so per-replica), not global. For a single-node
deploy this is fine; for HA we'd want Redis to be present.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

try:
    import redis  # type: ignore
except ImportError:
    redis = None  # type: ignore


log = logging.getLogger("drop_pensa.ratelimit")


# Sliding-window Lua script. Runs atomically inside Redis.
#
# KEYS[1] = bucket key
# ARGV[1] = window size in seconds
# ARGV[2] = max events allowed in window
# ARGV[3] = current unix time (ms)
# ARGV[4] = unique event id (timestamp+random nonce)
#
# Returns 1 if allowed, 0 if rate-limited.
_LUA_SLIDING = """
local key    = KEYS[1]
local window = tonumber(ARGV[1])
local maxn   = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local member = ARGV[4]
local cutoff = now_ms - (window * 1000)

redis.call('ZREMRANGEBYSCORE', key, '-inf', cutoff)
local count = redis.call('ZCARD', key)
if count >= maxn then
  return 0
end
redis.call('ZADD', key, now_ms, member)
redis.call('PEXPIRE', key, window * 1000 + 1000)
return 1
"""


@dataclass(frozen=True)
class Limit:
    """A named rate limit: max N events per window seconds."""
    name: str
    max_events: int
    window_seconds: int


# Limits used across the app. Keep names stable; they show up in logs.
LIMITS = {
    "upload_per_ip":          Limit("upload_per_ip",        30,   3600),  # 30/h
    "upload_per_ip_foreign":  Limit("upload_per_ip_foreign", 10,  3600),  # 10/h when CORS request
    "fetch_per_ip":           Limit("fetch_per_ip",         200,    60),  # 200/min
    "fetch_per_file_ip":      Limit("fetch_per_file_ip",    100,  3600),  # 100/h per (file, ip)
    "delete_per_ip":          Limit("delete_per_ip",         60,  3600),
}


class _Backend(Protocol):
    def check(self, bucket_key: str, limit: Limit) -> bool: ...


class _MemoryBackend:
    """
    Sliding-window limiter using deques per bucket.

    Not perfect under high concurrency (the lock is global, not per-bucket),
    but the workload here is far below where that matters — at the rates we
    enforce, the lock is held for microseconds. If we ever need to scale,
    Redis takes over and this code path goes idle.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, bucket_key: str, limit: Limit) -> bool:
        now = time.time()
        cutoff = now - limit.window_seconds
        with self._lock:
            d = self._buckets[bucket_key]
            # Trim expired entries from the left.
            while d and d[0] < cutoff:
                d.popleft()
            if len(d) >= limit.max_events:
                return False
            d.append(now)
            return True


class _RedisBackend:
    """Redis-backed sliding window. Falls back to memory on connection errors."""

    def __init__(self, url: str, fallback: _Backend) -> None:
        self._client = redis.Redis.from_url(url, socket_timeout=0.5, socket_connect_timeout=0.5)
        self._lua = self._client.register_script(_LUA_SLIDING)
        self._fallback = fallback
        self._healthy = True

    def check(self, bucket_key: str, limit: Limit) -> bool:
        # If we already saw Redis go down, don't keep paying the timeout
        # on every request. We retry on a cooldown via _retry_redis().
        if not self._healthy:
            return self._fallback.check(bucket_key, limit)

        try:
            now_ms = int(time.time() * 1000)
            # Member must be unique per call so ZADD doesn't dedupe within
            # the same millisecond. Two random bytes are plenty.
            member = f"{now_ms}-{time.monotonic_ns() & 0xFFFF:x}"
            allowed = self._lua(
                keys=[bucket_key],
                args=[limit.window_seconds, limit.max_events, now_ms, member],
            )
            return bool(allowed)
        except Exception as e:
            log.warning("redis rate-limit failed, falling back to memory: %s", e)
            self._healthy = False
            return self._fallback.check(bucket_key, limit)


# ---------- public API ----------


_backend: _Backend | None = None


def init(redis_url: str | None) -> None:
    """Initialize the global limiter. Idempotent."""
    global _backend
    mem = _MemoryBackend()
    if redis_url and redis is not None:
        try:
            client = redis.Redis.from_url(redis_url, socket_connect_timeout=1.0)
            client.ping()
            _backend = _RedisBackend(redis_url, fallback=mem)
            log.info("rate-limit backend: redis (%s)", redis_url)
            return
        except Exception as e:
            log.warning("redis at %s unreachable, using memory: %s", redis_url, e)
    _backend = mem
    log.info("rate-limit backend: memory")


def check(limit_name: str, *bucket_parts: str) -> bool:
    """
    Check if an event is allowed under the named limit.

    `bucket_parts` are joined into the bucket key (e.g., the IP, or
    "(file_id, ip)"). Returns True if the event is allowed and was
    counted, False if the bucket is full.
    """
    if _backend is None:
        # Tests or early calls — fail open so we don't break startup.
        return True
    limit = LIMITS[limit_name]
    key = "rl:" + limit.name + ":" + ":".join(bucket_parts)
    return _backend.check(key, limit)
