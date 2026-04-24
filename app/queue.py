import os

from redis import Redis
from rq import Queue

_redis: Redis | None = None
_queue: Queue | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(os.getenv("REDIS_URL", "redis://localhost:6379"))
    return _redis


def get_queue() -> Queue:
    global _queue
    if _queue is None:
        _queue = Queue(connection=get_redis())
    return _queue


def set_sync_status(uuid: str, message: str, current: int = 0, total: int = 0) -> None:
    redis = get_redis()
    key = f"sync_status:{uuid}"
    redis.hset(key, mapping={"message": message, "current": current, "total": total})
    redis.expire(key, 3600)


def get_sync_status(uuid: str) -> dict | None:
    raw = get_redis().hgetall(f"sync_status:{uuid}")
    if not raw:
        return None
    return {
        "message": raw.get(b"message", b"").decode(),
        "current": int(raw.get(b"current", b"0") or 0),
        "total": int(raw.get(b"total", b"0") or 0),
    }
