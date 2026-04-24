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
