import os
import tempfile

# Must be set before the app (and its engine) is imported.
_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.update(
    DATABASE_URL=f"sqlite+aiosqlite:///{_db}",
    LLM_API_KEY="",
    E2B_API_KEY="",
    XHS_CONNECTOR="mock",
    INTERACTION_DAILY_LIMIT="3",
    INTERACTION_MIN_INTERVAL_SECONDS="0",
    ADMIN_USERNAME="admin",
    ADMIN_PASSWORD="admin-pass",
)

import fakeredis.aioredis  # noqa: E402
import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.main import app, ensure_admin  # noqa: E402
from app.redis_client import set_redis  # noqa: E402


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup():
    get_settings.cache_clear()
    set_redis(fakeredis.aioredis.FakeRedis(decode_responses=True))
    await init_db()
    await ensure_admin()


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def db():
    async with SessionLocal() as session:
        yield session


_n = 0


@pytest_asyncio.fixture
async def create_user(client):
    """Admin-provisioned account; returns (user id, auth headers)."""

    async def _create(username: str, password: str = "secret123", role: str = "user"):
        adm = await client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass"})
        assert adm.status_code == 200, adm.text
        r = await client.post(
            "/api/admin/users",
            headers={"Authorization": f"Bearer {adm.json()['token']}"},
            json={"username": username, "password": password, "role": role},
        )
        assert r.status_code == 200, r.text
        login = await client.post("/api/auth/login", json={"username": username, "password": password})
        assert login.status_code == 200, login.text
        return r.json()["id"], {"Authorization": f"Bearer {login.json()['token']}"}

    return _create


@pytest_asyncio.fixture
async def auth(create_user):
    """A fresh admin-provisioned user; returns auth headers."""
    global _n
    _n += 1
    _, headers = await create_user(f"user{_n}")
    return headers


@pytest_asyncio.fixture
async def admin(client):
    r = await client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}
