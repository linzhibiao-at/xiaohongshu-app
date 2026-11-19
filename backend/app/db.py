from collections.abc import AsyncIterator

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import get_settings


class Base(DeclarativeBase):
    pass


_settings = get_settings()
engine = create_async_engine(_settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    from app import models  # noqa: F401  (register tables)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _sync_columns(conn)
        await _upgrade_mysql_columns(conn)


# create_all 不会给已存在的表补列，这里补齐后加的字段（表: 列名 -> 列定义）。
_LATE_COLUMNS: dict[str, dict[str, str]] = {
    "xhs_accounts": {"xhs_id": "VARCHAR(64) NOT NULL DEFAULT ''"},
}


async def _sync_columns(conn: AsyncConnection) -> None:
    def _apply(sync_conn) -> None:
        inspector = inspect(sync_conn)
        tables = set(inspector.get_table_names())
        for table, columns in _LATE_COLUMNS.items():
            if table not in tables:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))

    await conn.run_sync(_apply)


# create_all 同样不会修改已存在列的类型。上传的技能文件可达 5MB，而 MySQL 的
# TEXT/BLOB 只有 64KB，且 utf8mb4_unicode_ci 会把 A.md 与 a.md 当成同一路径。
# (表, 列, 期望 DATA_TYPE, 期望 COLLATION 或 None, MODIFY 子句)
_MYSQL_COLUMN_UPGRADES: list[tuple[str, str, str, str | None, str]] = [
    ("skills", "instructions", "mediumtext", None, "MEDIUMTEXT NOT NULL"),
    ("skill_files", "content", "mediumblob", None, "MEDIUMBLOB NOT NULL"),
    ("skill_files", "path", "varchar", "utf8mb4_bin", "VARCHAR(512) COLLATE utf8mb4_bin NOT NULL"),
]


async def _upgrade_mysql_columns(conn: AsyncConnection) -> None:
    if conn.dialect.name != "mysql":
        return

    def _apply(sync_conn) -> None:
        wanted_tables = {t for t, *_ in _MYSQL_COLUMN_UPGRADES}
        rows = sync_conn.execute(
            text(
                "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, COLLATION_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN :tables"
            ).bindparams(bindparam("tables", expanding=True)),
            {"tables": sorted(wanted_tables)},
        ).all()
        current = {(t, c): (dt, coll) for t, c, dt, coll in rows}
        for table, column, want_type, want_collation, ddl in _MYSQL_COLUMN_UPGRADES:
            found = current.get((table, column))
            if found is None:
                continue
            data_type, collation = found
            if data_type == want_type and (want_collation is None or collation == want_collation):
                continue
            sync_conn.execute(text(f"ALTER TABLE {table} MODIFY COLUMN {column} {ddl}"))

    await conn.run_sync(_apply)
