#!/usr/bin/env python3
"""小红书起号 agent 的统一 CLI。

输出统一为 JSON（ensure_ascii=False），由 Agent 负责组织成人话。
退出码：0 成功 / 1 未登录 / 2 参数或执行错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 允许直接执行 scripts/growing_cli.py
sys.path.insert(0, str(Path(__file__).resolve().parent))

from growing import (
    audit,
    benchmark,
    calendar,
    collect,
    copy,
    hotspot,
    review,
    store,
    topic,
    track,
    xhs,
)
from growing.config import LANES, XHS_CLI, resolve_lane, xhs_python


def _out(data: object, code: int = 0) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    sys.exit(code)


def _read_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _read_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def _parse_fits(raw: str) -> dict[str, float]:
    """解析 --fit 母婴育儿=80,美食探店=60。"""
    if not raw:
        return {}
    fits: dict[str, float] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"--fit 格式应为 赛道=分数，收到：{chunk}")
        name, value = chunk.split("=", 1)
        fits[name.strip()] = float(value)
    return fits


def _parse_weights(raw: str) -> dict[str, float]:
    """解析 --weights demand=0.3,external=0.1，只覆盖传入的维度。"""
    if not raw:
        return {}
    weights: dict[str, float] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"--weights 格式应为 维度=权重，收到：{chunk}")
        name, value = chunk.split("=", 1)
        weights[name.strip()] = float(value)
    return weights


# ─── doctor / profile ────────────────────────────────────────────────────────


def cmd_doctor(args: argparse.Namespace) -> None:
    logged_in, error = xhs.check_login()
    with store.connect() as conn:
        self_id = store.kv_get(conn, "self_user_id", "")
        note_count = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        author_count = conn.execute("SELECT COUNT(*) c FROM authors").fetchone()["c"]
    _out(
        {
            "xhs_cli": str(XHS_CLI),
            "xhs_cli_exists": Path(XHS_CLI).exists(),
            "xhs_python": xhs_python(),
            "logged_in": logged_in,
            "login_error": error,
            "self_registered": bool(self_id),
            "self_user_id": self_id,
            "local_notes": note_count,
            "local_authors": author_count,
            "lanes": list(LANES),
        }
    )


def cmd_profile_set(args: argparse.Namespace) -> None:
    _out(collect.collect_self_profile(args.url))


def cmd_collect_lane(args: argparse.Namespace) -> None:
    cfg = collect.CollectConfig(
        keywords=args.keywords or [],
        per_keyword=args.per_keyword,
        detail_top=args.detail_top,
        with_fresh=not args.no_fresh,
        with_authors=args.with_authors,
        author_top=args.author_top,
    )
    _out(collect.collect_lane(args.lane, cfg))


def cmd_collect_search(args: argparse.Namespace) -> None:
    with store.connect() as conn:
        notes = collect.collect_search(
            conn,
            collect.seed_lane_lookup(collect.resolve_lane(args.lane).key),
            args.keyword,
            sort_by=args.sort_by,
            publish_time=args.publish_time,
            note_type=args.note_type,
            limit=args.limit,
        )
    _out({"count": len(notes), "notes": notes})


def cmd_collect_detail(args: argparse.Namespace) -> None:
    with store.connect() as conn:
        note = store.get_note(conn, args.feed_id) or {
            "feed_id": args.feed_id,
            "xsec_token": args.xsec_token,
            "lane": args.lane or "",
        }
        detail = collect.collect_detail(
            conn, note, keyword=note.get("keyword", ""), load_comments=args.comments
        )
    detail.pop("_comments", None)
    _out(detail)


def cmd_collect_author(args: argparse.Namespace) -> None:
    with store.connect() as conn:
        author = collect.collect_author(
            conn, args.user_id, args.xsec_token or "", lane=args.lane or ""
        )
    _out(author)


def cmd_collect_refresh(args: argparse.Namespace) -> None:
    _out(collect.refresh_self_stats())


# ─── track ───────────────────────────────────────────────────────────────────


def cmd_track_lane(args: argparse.Namespace) -> None:
    fits = _parse_fits(args.fit)
    with store.connect() as conn:
        lane = resolve_lane(args.lane)
        _out(
            track.score_lane(
                conn,
                lane.key,
                fit=fits.get(lane.key, 60.0),
                weights=_parse_weights(args.weights),
            )
        )


def cmd_track_compare(args: argparse.Namespace) -> None:
    lanes = [x.strip() for x in args.lanes.split(",")] if args.lanes else None
    _out(
        track.compare_lanes(
            fits=_parse_fits(args.fit),
            weights=_parse_weights(args.weights),
            lanes=lanes,
        )
    )


def cmd_track_keywords(args: argparse.Namespace) -> None:
    with store.connect() as conn:
        _out({"lane": args.lane, "keywords": track.keyword_heat(conn, args.lane)})


# ─── benchmark ───────────────────────────────────────────────────────────────


def cmd_benchmark_pick(args: argparse.Namespace) -> None:
    _out(benchmark.pick_benchmarks(args.lane, per_tier=args.per_tier))


def cmd_benchmark_author(args: argparse.Namespace) -> None:
    _out(benchmark.analyze_author(args.user_id, args.lane or ""))


def cmd_benchmark_gap(args: argparse.Namespace) -> None:
    _out(benchmark.gap_against_self(args.user_id, args.lane or ""))


# ─── topic / copy ────────────────────────────────────────────────────────────


def cmd_topic_pool(args: argparse.Namespace) -> None:
    exclude = _read_json(args.exclude_file) if args.exclude_file else None
    _out(topic.build_pool(args.lane, per_pillar=args.per_pillar, exclude_titles=exclude))


def cmd_copy_check(args: argparse.Namespace) -> None:
    title = _read_text(args.title_file) if args.title_file else args.title or ""
    body = _read_text(args.body_file) if args.body_file else args.body or ""
    result = copy.assemble(title, body, args.tags or [], topic_id=args.topic_id or "")
    _out(result, 0 if result["ok"] else 2)


def cmd_copy_brief(args: argparse.Namespace) -> None:
    lane = resolve_lane(args.lane)
    record = {
        "pillar": args.pillar,
        "template": args.template,
    }
    if args.topic_file:
        record.update(_read_json(args.topic_file))
    _out(copy.build_brief(record, {
        "audience": lane.audience,
        "asset_logic": lane.asset_logic,
    }))


def cmd_copy_audit(args: argparse.Namespace) -> None:
    """发布前评分：机器判可确定项，封面与对标留给 Agent。"""
    draft = Path(args.draft_dir) if args.draft_dir else None
    if draft and not draft.is_dir():
        _out({"error": f"草稿目录不存在：{draft}"}, code=2)

    def _pick(file_name: str, inline: str) -> str:
        if inline:
            return inline
        if draft and (draft / file_name).is_file():
            return _read_text(str(draft / file_name))
        return ""

    title = _pick("title.txt", args.title)
    body = _pick("body.txt", args.body)
    if not title.strip():
        _out({"error": "没有标题：传 --title，或用 --draft-dir 指向含 title.txt 的目录"}, code=2)

    _out(audit.audit(
        title,
        body,
        resolve_lane(args.lane),
        cover=args.cover or "",
        goal=args.goal or "",
    ))


# ─── plan ────────────────────────────────────────────────────────────────────


def cmd_plan_build(args: argparse.Namespace) -> None:
    topics = _read_json(args.topics_file) if args.topics_file else None
    plan = calendar.build_plan(
        args.lane, start=args.start, days=args.days, topics=topics, name=args.name or ""
    )
    exported = ""
    if args.save:
        plan["id"] = calendar.save(plan)
        if args.export == "html":
            exported = str(calendar.export_report(plan))
        elif args.export:
            exported = str(calendar.export(plan, args.export))
    _out({"plan": plan, "saved_id": plan.get("id", 0), "exported": exported})


def cmd_plan_show(args: argparse.Namespace) -> None:
    with store.connect() as conn:
        plan = store.get_plan(conn, args.plan_id)
    if not plan:
        raise ValueError(f"排期 {args.plan_id} 不存在")
    payload = plan.get("payload") or plan
    _out({"plan": plan, "markdown": calendar.render_markdown(payload)})


def cmd_plan_next(args: argparse.Namespace) -> None:
    _out({"items": calendar.next_items(args.plan_id, limit=args.limit)})


def cmd_plan_mark(args: argparse.Namespace) -> None:
    with store.connect() as conn:
        store.mark_plan_item(conn, args.item_id, args.status, args.feed_id or "")
    _out({"item_id": args.item_id, "status": args.status})


def cmd_plan_progress(args: argparse.Namespace) -> None:
    _out(calendar.progress(args.plan_id))


def cmd_plan_shift(args: argparse.Namespace) -> None:
    with store.connect() as conn:
        plan = store.get_plan(conn, args.plan_id)
        if not plan:
            raise ValueError(f"排期 {args.plan_id} 不存在")
        shifted = calendar.shift_calendar(plan["payload"] or plan, args.from_day, args.delta)
        new_id = calendar.save(shifted)
    _out({"new_plan_id": new_id, "plan": shifted})


# ─── review ──────────────────────────────────────────────────────────────────


def cmd_review_note(args: argparse.Namespace) -> None:
    _out(review.note_review(args.feed_id, args.lane or ""))


def cmd_review_weekly(args: argparse.Namespace) -> None:
    _out(review.weekly_review(args.plan_id))


# ─── hotspot ─────────────────────────────────────────────────────────────────


def cmd_hotspot_pull(args: argparse.Namespace) -> None:
    _out(
        hotspot.pull_xhs(
            args.lane,
            keywords=args.keywords or None,
            per_keyword=args.per_keyword,
            sort_by=args.sort_by,
        )
    )


def cmd_hotspot_hotwords(args: argparse.Namespace) -> None:
    # _out 内部会 sys.exit，所以两个分支可以先后写而不需要 else
    if args.platform == "douyin":
        _out(hotspot.pull_douyin(args.lane or "", limit=args.limit))
    if not args.lane:
        _out({"error": "小红书站内热词必须指定 --lane，否则无法判定需求归属"}, code=2)
    _out(
        hotspot.pull_xhs_insights(
            args.lane,
            keywords=args.keywords or None,
            per_keyword=args.limit,
        )
    )


def cmd_hotspot_ingest(args: argparse.Namespace) -> None:
    _out(
        hotspot.ingest(
            args.file,
            args.platform,
            lane_name=args.lane or "",
            source_level=args.source_level,
            captured_at=args.captured_at or "",
        )
    )


def cmd_hotspot_trend(args: argparse.Namespace) -> None:
    _out(hotspot.trend(args.lane or "", days=args.days, platform=args.platform or ""))


def cmd_hotspot_gaps(args: argparse.Namespace) -> None:
    _out(hotspot.gaps(args.lane, days=args.days, limit=args.limit))


# ─── 参数解析 ────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="growing", description="小红书起号 agent 数据层")
    sub = parser.add_subparsers(dest="group", required=True)

    sub.add_parser("doctor", help="检查底层 CLI、登录状态与本地数据量")

    p = sub.add_parser("profile-set", help="登记自己的小红书主页链接")
    p.add_argument("--url", required=True)

    c = sub.add_parser("collect", help="采集数据").add_subparsers(dest="action", required=True)
    p = c.add_parser("lane", help="按赛道批量采集")
    p.add_argument("--lane", required=True)
    p.add_argument("--keywords", nargs="*")
    p.add_argument("--per-keyword", type=int, default=12)
    p.add_argument("--detail-top", type=int, default=6)
    p.add_argument("--no-fresh", action="store_true", help="不采集一周内最新样本")
    p.add_argument("--with-authors", action="store_true", help="额外采集头部作者粉丝数")
    p.add_argument("--author-top", type=int, default=5)
    p = c.add_parser("search", help="采集单个关键词")
    p.add_argument("--lane", required=True)
    p.add_argument("--keyword", required=True)
    p.add_argument("--sort-by", default="最多点赞")
    p.add_argument("--publish-time", default="")
    p.add_argument("--note-type", default="")
    p.add_argument("--limit", type=int, default=12)
    p = c.add_parser("detail", help="采集笔记详情")
    p.add_argument("--feed-id", required=True)
    p.add_argument("--xsec-token", default="")
    p.add_argument("--lane", default="")
    p.add_argument("--comments", action="store_true")
    p = c.add_parser("author", help="采集作者主页")
    p.add_argument("--user-id", required=True)
    p.add_argument("--xsec-token", default="")
    p.add_argument("--lane", default="")
    c.add_parser("refresh", help="刷新自己账号的互动快照")

    t = sub.add_parser("track", help="赛道判断").add_subparsers(dest="action", required=True)
    p = t.add_parser("lane", help="单赛道打分")
    p.add_argument("--lane", required=True)
    p.add_argument("--fit", default="", help="匹配度，如 母婴育儿=80,家居生活=60")
    p.add_argument(
        "--weights",
        default="",
        help="覆盖评分权重，如 demand=0.25,external=0.1（external 默认 0 不进总分）",
    )
    p = t.add_parser("compare", help="多赛道对比排序")
    p.add_argument("--fit", default="")
    p.add_argument("--lanes", default="", help="逗号分隔，默认四大赛道")
    p.add_argument("--weights", default="")
    p = t.add_parser("keywords", help="赛道内关键词热度")
    p.add_argument("--lane", required=True)

    b = sub.add_parser("benchmark", help="对标拆解").add_subparsers(dest="action", required=True)
    p = b.add_parser("pick", help="按粉丝分层挑对标账号")
    p.add_argument("--lane", required=True)
    p.add_argument("--per-tier", type=int, default=1)
    p = b.add_parser("author", help="拆解单个账号")
    p.add_argument("--user-id", required=True)
    p.add_argument("--lane", default="")
    p = b.add_parser("gap", help="自己与对标的差距")
    p.add_argument("--user-id", required=True)
    p.add_argument("--lane", default="")

    tp = sub.add_parser("topic", help="选题").add_subparsers(dest="action", required=True)
    p = tp.add_parser("pool", help="生成选题池")
    p.add_argument("--lane", required=True)
    p.add_argument("--per-pillar", type=int, default=3)
    p.add_argument("--exclude-file", default="")

    cp = sub.add_parser("copy", help="文案校验与骨架").add_subparsers(dest="action", required=True)
    p = cp.add_parser("check", help="校验标题/正文/话题")
    p.add_argument("--title-file", default="")
    p.add_argument("--body-file", default="")
    p.add_argument("--title", default="")
    p.add_argument("--body", default="")
    p.add_argument("--tags", nargs="*")
    p.add_argument("--topic-id", default="")
    p = cp.add_parser("brief", help="生成写作骨架")
    p.add_argument("--lane", required=True)
    p.add_argument("--pillar", required=True)
    p.add_argument("--template", default="")
    p.add_argument("--topic-file", default="")
    p = cp.add_parser(
        "audit", help="发布前评分：机器判可确定项，封面与对标标 pending"
    )
    p.add_argument("--lane", required=True)
    p.add_argument("--draft-dir", default="", help="含 title.txt / body.txt 的目录")
    p.add_argument("--title", default="")
    p.add_argument("--body", default="")
    p.add_argument("--cover", default="", help="封面图路径，只取客观尺寸信息")
    p.add_argument("--goal", default="", choices=["", "IP增长", "产品转化", "爆文流量"])

    pl = sub.add_parser("plan", help="30 天排期").add_subparsers(dest="action", required=True)
    p = pl.add_parser("build", help="生成排期")
    p.add_argument("--lane", required=True)
    p.add_argument("--start", default="")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--topics-file", default="")
    p.add_argument("--name", default="")
    p.add_argument("--save", action="store_true")
    p.add_argument("--export", default="", choices=["", "md", "csv", "html"])
    p = pl.add_parser("show", help="查看排期")
    p.add_argument("--plan-id", type=int, required=True)
    p = pl.add_parser("next", help="接下来要发的几条")
    p.add_argument("--plan-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=3)
    p = pl.add_parser("mark", help="标记某条状态")
    p.add_argument("--item-id", type=int, required=True)
    p.add_argument(
        "--status",
        required=True,
        choices=["planned", "writing", "published", "skipped"],
    )
    p.add_argument("--feed-id", default="")
    p = pl.add_parser("progress", help="排期完成度")
    p.add_argument("--plan-id", type=int, required=True)
    p = pl.add_parser("shift", help="整体顺延")
    p.add_argument("--plan-id", type=int, required=True)
    p.add_argument("--from-day", type=int, required=True)
    p.add_argument("--delta", type=int, required=True)

    r = sub.add_parser("review", help="数据复盘").add_subparsers(dest="action", required=True)
    p = r.add_parser("note", help="单篇复盘")
    p.add_argument("--feed-id", required=True)
    p.add_argument("--lane", default="")
    p = r.add_parser("weekly", help="阶段复盘")
    p.add_argument("--plan-id", type=int, default=None)

    h = sub.add_parser("hotspot", help="跨平台热点").add_subparsers(dest="action", required=True)
    p = h.add_parser("pull", help="用站内 CLI 采集关键词热度（来源等级 L2）")
    p.add_argument("--lane", required=True)
    p.add_argument("--keywords", nargs="*", help="不传则用赛道种子词前 3 个")
    p.add_argument("--per-keyword", type=int, default=12)
    p.add_argument("--sort-by", default="最多点赞")
    p = h.add_parser(
        "hotwords", help="拉平台热词（抖音全国热榜 / 小红书站内搜索需求，L1）"
    )
    p.add_argument("--platform", required=True, choices=["douyin", "xhs"])
    p.add_argument("--lane", default="", help="绑定赛道；抖音不传则只存全网热点池")
    p.add_argument("--keywords", nargs="*", help="小红书：覆盖赛道种子词")
    p.add_argument("--limit", type=int, default=25, help="取前 N 条")
    p = h.add_parser("ingest", help="导入网页/搜索整理的热点 JSON（默认 L3）")
    p.add_argument("--platform", required=True, choices=["douyin", "xhs"])
    p.add_argument("--file", required=True, help="JSON 文件路径")
    p.add_argument("--lane", default="")
    p.add_argument("--source-level", default="L3", choices=["L1", "L2", "L3"])
    p.add_argument("--captured-at", default="", help="采集时间，缺省用当前时间")
    p = h.add_parser("trend", help="热点趋势与持续性")
    p.add_argument("--lane", default="")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--platform", default="", choices=["", "douyin", "xhs"])
    p = h.add_parser("gaps", help="跨平台错位：外部热但站内没接住")
    p.add_argument("--lane", required=True)
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--limit", type=int, default=12)

    return parser


HANDLERS = {
    ("doctor", None): cmd_doctor,
    ("profile-set", None): cmd_profile_set,
    ("collect", "lane"): cmd_collect_lane,
    ("collect", "search"): cmd_collect_search,
    ("collect", "detail"): cmd_collect_detail,
    ("collect", "author"): cmd_collect_author,
    ("collect", "refresh"): cmd_collect_refresh,
    ("track", "lane"): cmd_track_lane,
    ("track", "compare"): cmd_track_compare,
    ("track", "keywords"): cmd_track_keywords,
    ("benchmark", "pick"): cmd_benchmark_pick,
    ("benchmark", "author"): cmd_benchmark_author,
    ("benchmark", "gap"): cmd_benchmark_gap,
    ("topic", "pool"): cmd_topic_pool,
    ("copy", "check"): cmd_copy_check,
    ("copy", "brief"): cmd_copy_brief,
    ("copy", "audit"): cmd_copy_audit,
    ("plan", "build"): cmd_plan_build,
    ("plan", "show"): cmd_plan_show,
    ("plan", "next"): cmd_plan_next,
    ("plan", "mark"): cmd_plan_mark,
    ("plan", "progress"): cmd_plan_progress,
    ("plan", "shift"): cmd_plan_shift,
    ("review", "note"): cmd_review_note,
    ("review", "weekly"): cmd_review_weekly,
    ("hotspot", "pull"): cmd_hotspot_pull,
    ("hotspot", "hotwords"): cmd_hotspot_hotwords,
    ("hotspot", "ingest"): cmd_hotspot_ingest,
    ("hotspot", "trend"): cmd_hotspot_trend,
    ("hotspot", "gaps"): cmd_hotspot_gaps,
}


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    key = (args.group, getattr(args, "action", None))
    handler = HANDLERS.get(key)
    if not handler:
        parser.error(f"未实现的命令：{key}")
    try:
        handler(args)
    except xhs.NotLoggedInError as exc:
        _out({"error": "未登录", "detail": str(exc), "next": "先执行 xhs-auth 登录流程"}, 1)
    except xhs.XhsError as exc:
        _out({"error": "小红书数据获取失败", "detail": str(exc)}, 2)
    except (ValueError, FileNotFoundError) as exc:
        _out({"error": str(exc)}, 2)
    except KeyboardInterrupt:
        _out({"error": "已中断"}, 2)


if __name__ == "__main__":
    main()
