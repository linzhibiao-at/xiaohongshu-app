"""本地快照库（SQLite）。

设计原则：
- 采集到的原始笔记/作者/搜索结果全部落库，报告可回溯到原始数据。
- 笔记的互动数据按天快照，才能做"发布 3 天 vs 7 天"的复盘对比。
- 不做 ORM，保持依赖为零。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    feed_id       TEXT PRIMARY KEY,
    xsec_token    TEXT DEFAULT '',
    title         TEXT DEFAULT '',
    desc          TEXT DEFAULT '',
    body          TEXT DEFAULT '',
    tags          TEXT DEFAULT '[]',
    note_type     TEXT DEFAULT '',
    author_id     TEXT DEFAULT '',
    author_name   TEXT DEFAULT '',
    liked         INTEGER DEFAULT 0,
    collected     INTEGER DEFAULT 0,
    comment       INTEGER DEFAULT 0,
    shared        INTEGER DEFAULT 0,
    liked_raw     TEXT DEFAULT '',
    collected_raw TEXT DEFAULT '',
    comment_raw   TEXT DEFAULT '',
    publish_ts    INTEGER DEFAULT 0,
    ip_location   TEXT DEFAULT '',
    cover         TEXT DEFAULT '',
    lane          TEXT DEFAULT '',
    keyword       TEXT DEFAULT '',
    is_self       INTEGER DEFAULT 0,
    first_seen_at TEXT DEFAULT '',
    last_seen_at  TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS note_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id     TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    day_index   INTEGER DEFAULT 0,
    liked       INTEGER DEFAULT 0,
    collected   INTEGER DEFAULT 0,
    comment     INTEGER DEFAULT 0,
    shared      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_snap_feed ON note_snapshots(feed_id);

CREATE TABLE IF NOT EXISTS authors (
    user_id      TEXT PRIMARY KEY,
    nickname     TEXT DEFAULT '',
    red_id       TEXT DEFAULT '',
    desc         TEXT DEFAULT '',
    fans         INTEGER DEFAULT 0,
    fans_raw     TEXT DEFAULT '',
    following    INTEGER DEFAULT 0,
    like_collected INTEGER DEFAULT 0,
    ip_location  TEXT DEFAULT '',
    lane         TEXT DEFAULT '',
    is_self      INTEGER DEFAULT 0,
    first_seen_at TEXT DEFAULT '',
    last_seen_at TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS searches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword      TEXT NOT NULL,
    lane         TEXT DEFAULT '',
    sort_by      TEXT DEFAULT '',
    publish_time TEXT DEFAULT '',
    note_type    TEXT DEFAULT '',
    result_count INTEGER DEFAULT 0,
    collected_at TEXT DEFAULT '',
    note_ids     TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    lane       TEXT DEFAULT '',
    start_date TEXT DEFAULT '',
    days       INTEGER DEFAULT 30,
    created_at TEXT DEFAULT '',
    payload    TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS plan_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     INTEGER NOT NULL,
    day_index   INTEGER DEFAULT 0,
    date        TEXT DEFAULT '',
    phase       TEXT DEFAULT '',
    pillar      TEXT DEFAULT '',
    title       TEXT DEFAULT '',
    note_type   TEXT DEFAULT '',
    publish_at  TEXT DEFAULT '',
    hook        TEXT DEFAULT '',
    tags        TEXT DEFAULT '[]',
    status      TEXT DEFAULT 'planned',
    feed_id     TEXT DEFAULT '',
    note        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_plan_items ON plan_items(plan_id);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS hotspot_signals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    platform    TEXT NOT NULL,
    lane        TEXT DEFAULT '',
    keyword     TEXT NOT NULL,
    raw_title   TEXT DEFAULT '',
    heat_value  REAL DEFAULT 0,
    heat_raw    TEXT DEFAULT '',
    rank        INTEGER DEFAULT 0,
    source_level TEXT DEFAULT '',
    source_url  TEXT DEFAULT '',
    captured_at TEXT DEFAULT '',
    captured_day TEXT DEFAULT '',
    meta        TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_hotspot_kw ON hotspot_signals(platform, keyword, captured_day);
CREATE INDEX IF NOT EXISTS idx_hotspot_lane ON hotspot_signals(lane, captured_day);
"""


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(path or db_path()))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


# ─── kv ──────────────────────────────────────────────────────────────────────


def kv_set(conn: sqlite3.Connection, key: str, value: Any) -> None:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    conn.execute(
        "INSERT INTO kv(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, payload),
    )


def kv_get(conn: sqlite3.Connection, key: str, default: Any = None) -> Any:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    raw = row["value"]
    if isinstance(default, (dict, list)):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default
    return raw


# ─── notes ───────────────────────────────────────────────────────────────────


def upsert_note(conn: sqlite3.Connection, note: dict) -> None:
    ts = now_iso()
    existing = conn.execute(
        "SELECT feed_id, liked FROM notes WHERE feed_id=?", (note["feed_id"],)
    ).fetchone()
    fields = {
        "feed_id": note["feed_id"],
        "xsec_token": note.get("xsec_token", ""),
        "title": note.get("title", ""),
        "desc": note.get("desc", ""),
        "body": note.get("body", ""),
        "tags": json.dumps(note.get("tags", []), ensure_ascii=False),
        "note_type": note.get("note_type", ""),
        "author_id": note.get("author_id", ""),
        "author_name": note.get("author_name", ""),
        "liked": int(note.get("liked", 0)),
        "collected": int(note.get("collected", 0)),
        "comment": int(note.get("comment", 0)),
        "shared": int(note.get("shared", 0)),
        "liked_raw": note.get("liked_raw", ""),
        "collected_raw": note.get("collected_raw", ""),
        "comment_raw": note.get("comment_raw", ""),
        "publish_ts": int(note.get("publish_ts", 0)),
        "ip_location": note.get("ip_location", ""),
        "cover": note.get("cover", ""),
        "lane": note.get("lane", ""),
        "keyword": note.get("keyword", ""),
        "is_self": int(note.get("is_self", 0)),
        "last_seen_at": ts,
    }
    if existing:
        # 缺字段时不要用空值覆盖已有内容
        updates = {k: v for k, v in fields.items() if v not in ("", 0) or k in ("feed_id",)}
        updates.pop("feed_id", None)
        assignments = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE notes SET {assignments} WHERE feed_id=?",
            (*updates.values(), fields["feed_id"]),
        )
    else:
        fields["first_seen_at"] = ts
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        conn.execute(f"INSERT INTO notes({cols}) VALUES({marks})", tuple(fields.values()))


def record_snapshot(conn: sqlite3.Connection, note_id: str, day_index: int = 0) -> None:
    row = conn.execute(
        "SELECT liked, collected, comment, shared FROM notes WHERE feed_id=?", (note_id,)
    ).fetchone()
    if not row:
        return
    conn.execute(
        "INSERT INTO note_snapshots"
        "(feed_id, captured_at, day_index, liked, collected, comment, shared)"
        " VALUES(?,?,?,?,?,?,?)",
        (note_id, now_iso(), day_index, row["liked"], row["collected"], row["comment"],
         row["shared"]),
    )


def get_note(conn: sqlite3.Connection, note_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM notes WHERE feed_id=?", (note_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_notes(
    conn: sqlite3.Connection,
    lane: str | None = None,
    keyword: str | None = None,
    is_self: bool | None = None,
    limit: int = 500,
) -> list[dict]:
    sql = "SELECT * FROM notes WHERE 1=1"
    args: list[Any] = []
    if lane:
        sql += " AND lane=?"
        args.append(lane)
    if keyword:
        sql += " AND keyword=?"
        args.append(keyword)
    if is_self is not None:
        sql += " AND is_self=?"
        args.append(1 if is_self else 0)
    sql += " ORDER BY (liked*1.0 + collected*1.5 + comment*2.0) DESC LIMIT ?"
    rows = conn.execute(sql, (*args, limit)).fetchall()
    return [_row_to_dict(r) for r in rows]


# ─── authors ─────────────────────────────────────────────────────────────────


def upsert_author(conn: sqlite3.Connection, author: dict) -> None:
    ts = now_iso()
    fields = {
        "user_id": author["user_id"],
        "nickname": author.get("nickname", ""),
        "red_id": author.get("red_id", ""),
        "desc": author.get("desc", ""),
        "fans": int(author.get("fans", 0)),
        "fans_raw": author.get("fans_raw", ""),
        "following": int(author.get("following", 0)),
        "like_collected": int(author.get("like_collected", 0)),
        "ip_location": author.get("ip_location", ""),
        "lane": author.get("lane", ""),
        "is_self": int(author.get("is_self", 0)),
        "last_seen_at": ts,
    }
    exists = conn.execute("SELECT 1 FROM authors WHERE user_id=?", (fields["user_id"],)).fetchone()
    if exists:
        updates = {k: v for k, v in fields.items() if v not in ("", 0)}
        updates.pop("user_id", None)
        assignments = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE authors SET {assignments} WHERE user_id=?",
            (*updates.values(), fields["user_id"]),
        )
    else:
        fields["first_seen_at"] = ts
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        conn.execute(f"INSERT INTO authors({cols}) VALUES({marks})", tuple(fields.values()))


def get_author(conn: sqlite3.Connection, user_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM authors WHERE user_id=?", (user_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_authors(conn: sqlite3.Connection, lane: str | None = None) -> list[dict]:
    sql = "SELECT * FROM authors"
    args: list[Any] = []
    if lane:
        sql += " WHERE lane=?"
        args.append(lane)
    sql += " ORDER BY fans DESC"
    return [_row_to_dict(r) for r in conn.execute(sql, args).fetchall()]


# ─── searches ────────────────────────────────────────────────────────────────


def record_search(conn: sqlite3.Connection, search: dict) -> None:
    conn.execute(
        "INSERT INTO searches(keyword, lane, sort_by, publish_time, note_type, result_count,"
        " collected_at, note_ids) VALUES(?,?,?,?,?,?,?,?)",
        (
            search["keyword"],
            search.get("lane", ""),
            search.get("sort_by", ""),
            search.get("publish_time", ""),
            search.get("note_type", ""),
            int(search.get("result_count", 0)),
            now_iso(),
            json.dumps(search.get("note_ids", []), ensure_ascii=False),
        ),
    )


# ─── hotspot ─────────────────────────────────────────────────────────────────


def upsert_hotspot(conn: sqlite3.Connection, signal: dict) -> None:
    """写入一条热点信号。

    同一 platform + keyword + 采集日 只保留一条，重复采集时用新值覆盖，
    这样 trend 里“同一天反复抓”不会把趋势曲线拉平。
    """
    captured_at = signal.get("captured_at") or now_iso()
    captured_day = captured_at[:10]
    fields = {
        "platform": signal["platform"],
        "lane": signal.get("lane", ""),
        "keyword": signal["keyword"],
        "raw_title": signal.get("raw_title", ""),
        "heat_value": float(signal.get("heat_value", 0) or 0),
        "heat_raw": signal.get("heat_raw", ""),
        "rank": int(signal.get("rank", 0) or 0),
        "source_level": signal.get("source_level", ""),
        "source_url": signal.get("source_url", ""),
        "meta": json.dumps(signal.get("meta", {}) or {}, ensure_ascii=False),
    }
    existing = conn.execute(
        "SELECT id FROM hotspot_signals WHERE platform=? AND keyword=? AND captured_day=?",
        (fields["platform"], fields["keyword"], captured_day),
    ).fetchone()
    if existing:
        assignments = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE hotspot_signals SET {assignments}, captured_at=? WHERE id=?",
            (*fields.values(), captured_at, existing["id"]),
        )
    else:
        fields["captured_at"] = captured_at
        fields["captured_day"] = captured_day
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        conn.execute(f"INSERT INTO hotspot_signals({cols}) VALUES({marks})", tuple(fields.values()))


def list_hotspot(
    conn: sqlite3.Connection,
    lane: str | None = None,
    platform: str | None = None,
    days: int = 0,
    limit: int = 200,
) -> list[dict]:
    """按赛道/平台/时间窗取热点信号，最近的在前。"""
    sql = "SELECT * FROM hotspot_signals WHERE 1=1"
    args: list[Any] = []
    if lane:
        sql += " AND lane=?"
        args.append(lane)
    if platform:
        sql += " AND platform=?"
        args.append(platform)
    if days > 0:
        sql += " AND captured_day >= date('now', ?)"
        args.append(f"-{int(days)} days")
    sql += " ORDER BY captured_at DESC, heat_value DESC LIMIT ?"
    rows = conn.execute(sql, (*args, limit)).fetchall()
    out = []
    for row in rows:
        item = _row_to_dict(row)
        if isinstance(item.get("meta"), str):
            try:
                item["meta"] = json.loads(item["meta"])
            except json.JSONDecodeError:
                item["meta"] = {}
        out.append(item)
    return out


# ─── plans ───────────────────────────────────────────────────────────────────


def save_plan(conn: sqlite3.Connection, plan: dict) -> int:
    cur = conn.execute(
        "INSERT INTO plans(name, lane, start_date, days, created_at, payload)"
        " VALUES(?,?,?,?,?,?)",
        (
            plan["name"],
            plan.get("lane", ""),
            plan.get("start_date", ""),
            int(plan.get("days", 30)),
            now_iso(),
            json.dumps(plan, ensure_ascii=False),
        ),
    )
    plan_id = int(cur.lastrowid or 0)
    for item in plan.get("items", []):
        conn.execute(
            "INSERT INTO plan_items(plan_id, day_index, date, phase, pillar, title, note_type,"
            " publish_at, hook, tags, status, feed_id, note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id,
                int(item.get("day_index", 0)),
                item.get("date", ""),
                item.get("phase", ""),
                item.get("pillar", ""),
                item.get("title", ""),
                item.get("note_type", ""),
                item.get("publish_at", ""),
                item.get("hook", ""),
                json.dumps(item.get("tags", []), ensure_ascii=False),
                item.get("status", "planned"),
                item.get("feed_id", ""),
                item.get("note", ""),
            ),
        )
    return plan_id


def get_plan(conn: sqlite3.Connection, plan_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    if not row:
        return None
    plan = _row_to_dict(row)
    items = conn.execute(
        "SELECT * FROM plan_items WHERE plan_id=? ORDER BY day_index", (plan_id,)
    ).fetchall()
    plan["items"] = [_row_to_dict(i) for i in items]
    return plan


def latest_plan(conn: sqlite3.Connection, lane: str | None = None) -> dict | None:
    sql = "SELECT id FROM plans"
    args: list[Any] = []
    if lane:
        sql += " WHERE lane=?"
        args.append(lane)
    sql += " ORDER BY id DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    return get_plan(conn, int(row["id"])) if row else None


def mark_plan_item(conn: sqlite3.Connection, item_id: int, status: str, feed_id: str = "") -> None:
    conn.execute(
        "UPDATE plan_items SET status=?, feed_id=COALESCE(NULLIF(?, ''), feed_id) WHERE id=?",
        (status, feed_id, item_id),
    )


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key in ("tags", "note_ids"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except json.JSONDecodeError:
                data[key] = []
    if "payload" in data and isinstance(data["payload"], str):
        try:
            data["payload"] = json.loads(data["payload"])
        except json.JSONDecodeError:
            data["payload"] = {}
    return data
