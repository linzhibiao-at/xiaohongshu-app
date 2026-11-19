"""Note traffic: sync snapshots from the connector and compute analytics."""

import json
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Note, NoteMetric, XhsAccount
from app.redis_client import get_redis
from app.xhs.base import get_connector

CACHE_TTL = 300
METRIC_FIELDS = ("impressions", "views", "likes", "collects", "comments", "shares", "new_followers")


def _cache_key(account_id: int) -> str:
    return f"xhs:analytics:{account_id}"


async def sync_account(db: AsyncSession, account: XhsAccount) -> int:
    """Pull notes + current numbers, store one metric snapshot per note."""
    connector = get_connector(account.connector, account.credential)
    snapshots = await connector.list_notes(f"{account.id}:{account.niche or account.nickname}")
    existing = {
        n.xhs_note_id: n
        for n in (await db.scalars(select(Note).where(Note.account_id == account.id))).all()
    }
    for snap in snapshots:
        note = existing.get(snap.xhs_note_id)
        if note is None:
            note = Note(account_id=account.id, xhs_note_id=snap.xhs_note_id, title=snap.title)
            db.add(note)
        note.title, note.note_type, note.tags, note.published_at = (
            snap.title, snap.note_type, snap.tags, snap.published_at,
        )
        await db.flush()
        db.add(NoteMetric(note_id=note.id, captured_at=datetime.now(),
                          **{f: getattr(snap, f) for f in METRIC_FIELDS}))
    await db.commit()
    await get_redis().delete(_cache_key(account.id))
    return len(snapshots)


def _rate(num: float, den: float) -> float:
    return round(num / den, 4) if den else 0.0


def _engagement(m: dict) -> int:
    return m["likes"] + m["collects"] + m["comments"] + m["shares"]


async def account_report(db: AsyncSession, account_id: int) -> dict:
    r = get_redis()
    if cached := await r.get(_cache_key(account_id)):
        return json.loads(cached)

    notes = (await db.scalars(select(Note).where(Note.account_id == account_id))).all()
    note_by_id = {n.id: n for n in notes}
    metrics = []
    if notes:
        metrics = (
            await db.scalars(
                select(NoteMetric).where(NoteMetric.note_id.in_(note_by_id)).order_by(NoteMetric.captured_at)
            )
        ).all()

    latest: dict[int, dict] = {}  # note id -> latest numbers
    daily: dict[str, dict[int, dict]] = defaultdict(dict)  # day -> note id -> last numbers that day
    for m in metrics:
        row = {f: getattr(m, f) for f in METRIC_FIELDS}
        latest[m.note_id] = row
        daily[m.captured_at.strftime("%Y-%m-%d")][m.note_id] = row

    totals = {f: sum(row[f] for row in latest.values()) for f in METRIC_FIELDS}

    note_rows = []
    by_hour: dict[int, list[float]] = defaultdict(list)
    by_tag: dict[str, list[float]] = defaultdict(list)
    by_type: dict[str, list[float]] = defaultdict(list)
    for nid, row in latest.items():
        n = note_by_id[nid]
        er = _rate(_engagement(row), row["views"])
        note_rows.append({
            "id": n.id, "title": n.title, "note_type": n.note_type, "tags": n.tags,
            "published_at": n.published_at.isoformat() if n.published_at else None,
            **row, "ctr": _rate(row["views"], row["impressions"]), "engagement_rate": er,
        })
        if n.published_at:
            by_hour[n.published_at.hour].append(er)
        for tag in n.tags or []:
            by_tag[tag].append(er)
        by_type[n.note_type].append(er)
    note_rows.sort(key=lambda x: x["views"], reverse=True)

    def avg_table(groups: dict, label: str) -> list[dict]:
        rows = [{label: k, "notes": len(v), "avg_engagement_rate": round(sum(v) / len(v), 4)}
                for k, v in groups.items()]
        return sorted(rows, key=lambda x: x["avg_engagement_rate"], reverse=True)

    trend = [
        {"date": day, **{f: sum(row[f] for row in rows.values()) for f in METRIC_FIELDS}}
        for day, rows in sorted(daily.items())
    ]

    report = {
        "account_id": account_id,
        "note_count": len(latest),
        "totals": totals,
        "ctr": _rate(totals["views"], totals["impressions"]),
        "engagement_rate": _rate(_engagement(totals), totals["views"]),
        "follow_rate": _rate(totals["new_followers"], totals["views"]),
        "notes": note_rows,
        "trend": trend,
        "by_publish_hour": avg_table(by_hour, "hour"),
        "by_tag": avg_table(by_tag, "tag"),
        "by_type": avg_table(by_type, "note_type"),
    }
    await r.set(_cache_key(account_id), json.dumps(report, ensure_ascii=False), ex=CACHE_TTL)
    return report
