from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.api import admin_api, user_api
from app.config import get_settings
from app.db import SessionLocal, init_db
from app.models import User
from app.security import hash_password
from app.services.skills import cleanup_cache_dir


async def ensure_admin() -> None:
    s = get_settings()
    async with SessionLocal() as db:
        if await db.scalar(select(User).where(User.username == s.admin_username)) is None:
            db.add(User(username=s.admin_username, password_hash=hash_password(s.admin_password), role="admin"))
            await db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await ensure_admin()
    cleanup_cache_dir()  # drop skill files extracted by earlier processes
    yield


app = FastAPI(title="小红书运营平台", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",")],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(user_api.router)
app.include_router(admin_api.router)


@app.get("/healthz")
async def healthz():
    return {"ok": True}
