import redis.asyncio as aioredis

from app.config import get_settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


def set_redis(client: aioredis.Redis) -> None:
    """Swap the client (used by tests with fakeredis)."""
    global _client
    _client = client
