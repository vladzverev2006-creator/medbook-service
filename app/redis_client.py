"""Redis access: cache-aside helpers (AP) and distributed slot locks (CP).

Cache helpers never raise on Redis failures: they log a warning and degrade to
"cache miss", so the catalog keeps working from PostgreSQL.
Lock helpers `acquire_slot_lock` / `get_slot_lock_owner` DO raise `RedisError`:
booking must not proceed without a working lock (handled as 503 in app.main).
"""

import json
import logging
from datetime import date
from typing import Any

import redis
from redis.exceptions import RedisError

from app.config import settings

logger = logging.getLogger(__name__)

redis_client: redis.Redis = redis.Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_timeout=1.0,
    socket_connect_timeout=1.0,
    health_check_interval=30,
)

# ---------------------------------------------------------------- cache keys


def doctors_cache_key(
    clinic_id: int | None, specialization_id: int | None, city: str | None
) -> str:
    return (
        f"cache:doctors:clinic={clinic_id if clinic_id is not None else 'any'}"
        f":spec={specialization_id if specialization_id is not None else 'any'}"
        f":city={city if city else 'any'}"
    )


def doctors_cache_pattern() -> str:
    """Matches every cached doctor list across all filter combinations."""
    return "cache:doctors:*"


def slots_cache_key(doctor_id: int, day: date | None) -> str:
    return f"cache:slots:{doctor_id}:{day.isoformat() if day else 'all'}"


def slots_cache_pattern(doctor_id: int) -> str:
    """Matches every cached slot list (all dates) of a doctor."""
    return f"cache:slots:{doctor_id}:*"


# ---------------------------------------------------------------- cache-aside


def get_cache(key: str) -> Any | None:
    try:
        raw = redis_client.get(key)
    except RedisError as exc:
        logger.warning("Redis unavailable on cache read (%s), falling back to DB: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        logger.warning("Corrupted cache entry %s, ignoring", key)
        return None


def set_cache(key: str, value: Any, ttl: int) -> None:
    try:
        redis_client.set(key, json.dumps(value, ensure_ascii=False), ex=ttl)
    except RedisError as exc:
        logger.warning("Redis unavailable on cache write (%s): %s", key, exc)


def invalidate_cache(pattern: str) -> None:
    """Delete a key or all keys matching a glob pattern (e.g. `cache:slots:5:*`)."""
    try:
        if any(ch in pattern for ch in "*?["):
            batch: list[str] = []
            for key in redis_client.scan_iter(match=pattern, count=100):
                batch.append(key)
                if len(batch) >= 100:
                    redis_client.delete(*batch)
                    batch.clear()
            if batch:
                redis_client.delete(*batch)
        else:
            redis_client.delete(pattern)
    except RedisError as exc:
        # Stale entries will expire by TTL (<= SLOTS_CACHE_TTL / DOCTORS_CACHE_TTL).
        logger.warning("Redis unavailable on cache invalidation (%s): %s", pattern, exc)


# ---------------------------------------------------------------- slot locks

_RELEASE_IF_OWNER = redis_client.register_script(
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)


def _lock_key(slot_id: int) -> str:
    return f"lock:slot:{slot_id}"


def acquire_slot_lock(slot_id: int, user_id: int, ttl: int = 600) -> bool:
    """Atomic `SET lock:slot:{slot_id} {user_id} NX EX ttl`. Raises RedisError if Redis is down."""
    return bool(redis_client.set(_lock_key(slot_id), str(user_id), nx=True, ex=ttl))


def get_slot_lock_owner(slot_id: int) -> int | None:
    """User id holding the lock, or None if it expired. Raises RedisError if Redis is down."""
    value = redis_client.get(_lock_key(slot_id))
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def get_slot_lock_ttl(slot_id: int) -> int | None:
    """Remaining TTL in seconds for `lock:slot:{slot_id}`, or None if key does not exist."""
    try:
        ttl = redis_client.ttl(_lock_key(slot_id))
    except RedisError as exc:
        logger.warning("Redis unavailable on TTL check for slot %s: %s", slot_id, exc)
        return None
    if ttl is None or ttl <= 0:
        return None
    return int(ttl)


def release_slot_lock(slot_id: int, owner_id: int | None = None) -> None:
    """Best-effort lock release (lock has a TTL anyway).

    If `owner_id` is given, the key is deleted only when it still belongs to that
    user, so we never drop a lock that has meanwhile been taken by someone else.
    """
    try:
        if owner_id is None:
            redis_client.delete(_lock_key(slot_id))
        else:
            _RELEASE_IF_OWNER(keys=[_lock_key(slot_id)], args=[str(owner_id)])
    except RedisError as exc:
        logger.warning("Failed to release lock for slot %s: %s", slot_id, exc)
