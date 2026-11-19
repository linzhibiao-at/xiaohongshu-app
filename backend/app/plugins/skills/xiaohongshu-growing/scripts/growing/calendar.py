"""冷启动排期：把选题变成有节奏、可执行的发布计划。

排期逻辑不是平均分配，而是按四个阶段的验证目标安排密度：
立人设 → 测选题 → 放大 → 沉淀。
"""

from __future__ import annotations

import csv
import html
from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from . import store
from .config import PHASES, PUBLISH_SLOTS, Phase, export_dir, report_dir, resolve_lane
from .topic import pick_for_calendar


def _phase_of(day_index: int) -> Phase:
    for phase in PHASES:
        if phase.days[0] <= day_index <= phase.days[1]:
            return phase
    return PHASES[-1]


def _spread_days(start: int, end: int, count: int) -> list[int]:
    """在 [start, end] 内均匀放置 count 天，首尾都用到。"""
    if count <= 0:
        return []
    span = end - start + 1
    if count == 1:
        return [start + span // 2]
    return [start + round(i * (span - 1) / (count - 1)) for i in range(count)]


def _slots(lane_key: str, index: int) -> str:
    slots = PUBLISH_SLOTS.get(lane_key, ("12:00",))
    return slots[index % len(slots)]


def build_plan(
    lane_name: str,
    start: str | None = None,
    days: int = 30,
    topics: list[dict] | None = None,
    name: str = "",
) -> dict:
    """生成排期。topics 由 Agent 提供时优先使用，否则从选题池自动挑选。"""
    lane = resolve_lane(lane_name)
    start_date = datetime.strptime(start, "%Y-%m-%d").date() if start else date.today()
    chosen = topics or pick_for_calendar(lane.key, count=20)
    if not chosen:
        raise ValueError("选题池为空，请先执行 topic pool --lane <赛道> 采集数据")

    # 首篇固定为人设篇：起号第一条必须回答"你是谁、为谁服务"
    persona = {
        "pillar": "人设建立",
        "template": "身份体",
        "title": f"我是谁｜{lane.audience.split('，')[0]}的{lane.name}记录",
        "collect_intent": "mid",
        "note_type": "图文",
        "hook": "自我介绍 + 我能提供什么 + 你会看到什么内容",
        "tags": [lane.name, "新人报道", f"{lane.name}分享"],
    }
    pool = list(chosen)
    items: list[dict] = []
    counter = 0
    for phase in PHASES:
        count = phase.posts_per_week
        if phase.days[1] - phase.days[0] >= 7:
            count += 1
        end_day = min(phase.days[1], days)
        if end_day < phase.days[0]:
            continue
        day_list = _spread_days(phase.days[0], end_day, count)
        for day_index in day_list:
            if day_index > days:
                continue
            topic = {**persona} if counter == 0 else (
                pool.pop(0) if pool else _filler(lane.key, counter)
            )
            note_date = start_date + timedelta(days=day_index - 1)
            note_type = topic.get("note_type") or ("图文" if counter % 5 else "视频")
            items.append(
                {
                    "day_index": day_index,
                    "date": note_date.isoformat(),
                    "weekday": "一二三四五六日"[note_date.weekday()],
                    "phase": phase.name,
                    "phase_goal": phase.goal,
                    "pillar": topic.get("pillar", ""),
                    "template": topic.get("template", ""),
                    "title": topic.get("title", ""),
                    "hook": topic.get("hook") or _default_hook(topic),
                    "note_type": note_type,
                    "publish_at": _slots(lane.key, counter),
                    "tags": topic.get("tags", []),
                    "topic_id": topic.get("id", ""),
                    "collect_intent": topic.get("collect_intent", "mid"),
                    "status": "planned",
                }
            )
            counter += 1

    plan = {
        "name": name or f"{lane.name} {days} 天起号排期",
        "lane": lane.key,
        "start_date": start_date.isoformat(),
        "end_date": (start_date + timedelta(days=days - 1)).isoformat(),
        "days": days,
        "items": items,
    }
    plan["checks"] = check_plan(plan)
    plan["summary"] = _summary(plan)
    return plan


def _filler(lane_key: str, index: int) -> dict:
    lane = resolve_lane(lane_key)
    pillar = lane.pillars[index % len(lane.pillars)]
    return {
        "pillar": pillar,
        "template": "流程体",
        "title": f"{pillar}的完整做法，照着做就行",
        "hook": "按步骤给流程，结尾补常见失败点",
        "collect_intent": "high",
        "tags": [lane.name, pillar],
    }


def _default_hook(topic: dict) -> str:
    return f"{topic.get('template', '')}结构：开头给结论，中间给依据，结尾给可执行动作"


def _summary(plan: dict) -> dict:
    items = plan["items"]
    by_phase: dict[str, int] = {}
    by_pillar: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for item in items:
        by_phase[item["phase"]] = by_phase.get(item["phase"], 0) + 1
        by_pillar[item["pillar"]] = by_pillar.get(item["pillar"], 0) + 1
        by_type[item["note_type"]] = by_type.get(item["note_type"], 0) + 1
    return {
        "total_posts": len(items),
        "by_phase": by_phase,
        "by_pillar": by_pillar,
        "by_type": by_type,
    }


def check_plan(plan: dict) -> list[str]:
    """排期自检：密度、支柱连发、阶段目标缺失。"""
    issues: list[str] = []
    items = sorted(plan["items"], key=lambda x: x["day_index"])
    for prev, cur in pairwise(items):
        if cur["day_index"] - prev["day_index"] < 1:
            issues.append(f"第 {prev['day_index']} 天与第 {cur['day_index']} 天同天发布")
        if cur["pillar"] == prev["pillar"] and cur["day_index"] - prev["day_index"] <= 2:
            issues.append(
                f"第 {prev['day_index']}-{cur['day_index']} 天连续同一支柱"
                f"「{cur['pillar']}」，建议错开"
            )
    if not any(i["phase"] == "立人设" for i in items):
        issues.append("缺少人设篇，冷启动第一条必须交代身份")
    first_week = [i for i in items if i["day_index"] <= 7]
    if len(first_week) < 3:
        issues.append(f"首周只有 {len(first_week)} 篇，低于 3 篇不利于账号打标签")
    return issues


def save(plan: dict) -> int:
    with store.connect() as conn:
        return store.save_plan(conn, plan)


def render_markdown(plan: dict) -> str:
    lines = [
        f"# {plan['name']}",
        "",
        f"- 赛道：{plan['lane']}",
        f"- 周期：{plan['start_date']} ~ {plan['end_date']}（{plan['days']} 天）",
        f"- 总篇数：{plan['summary']['total_posts']}",
        f"- 形式分布：{plan['summary']['by_type']}",
        "",
        "| 天 | 日期 | 阶段 | 支柱 | 标题 | 形式 | 时间 | 状态 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for item in plan["items"]:
        lines.append(
            f"| D{item['day_index']} | {item['date']} 周{item['weekday']} | {item['phase']} | "
            f"{item['pillar']} | {item['title']} | {item['note_type']} | {item['publish_at']} | "
            f"{item['status']} |"
        )
    if plan.get("checks"):
        lines += ["", "## 排期自检", ""]
        lines += [f"- {c}" for c in plan["checks"]]
    else:
        lines += ["", "## 排期自检", "", "- 通过"]
    return "\n".join(lines)


_STATUS_LABEL = {"planned": "待写", "writing": "在写", "published": "已发", "skipped": "跳过"}

_HTML_CSS = """
:root{--ink:#1a1a1a;--muted:#6b6b6b;--line:#e6e3de;--bg:#faf9f7;--head:#f4f2ee}
*{box-sizing:border-box}
body{margin:0;padding:48px 24px;background:var(--bg);color:var(--ink);
  font:15px/1.7 -apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto}
h1{margin:0 0 6px;font-size:26px;letter-spacing:-.01em}
p.sub{margin:4px 0;color:var(--muted);font-size:14px}
.stats{display:flex;flex-wrap:wrap;gap:12px;margin:26px 0 22px}
.stat{flex:1 1 150px;padding:16px 18px;background:#fff;border:1px solid var(--line);
  border-radius:10px}
.stat b{display:block;font-size:24px;line-height:1.2}
.stat span{color:var(--muted);font-size:13px}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);
  border-radius:10px;overflow:hidden;font-size:14px;margin-top:22px}
thead th{position:sticky;top:0;background:var(--head);text-align:left;font-weight:600;
  padding:12px 14px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:11px 14px;border-bottom:1px solid #f1efeb;vertical-align:top}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover{background:#fbfaf8}
.d{font-weight:600;white-space:nowrap}
.num{white-space:nowrap;font-variant-numeric:tabular-nums}
.wd{margin-left:4px;color:var(--muted);font-size:12px}
.title{font-weight:500}
.st{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;
  background:#f0eeea;color:#555;white-space:nowrap}
.st.published{background:#e3f2e7;color:#256b36}
.st.writing{background:#fdf0d9;color:#8a5a12}
.st.skipped{background:#f4e6e6;color:#8a3d3d}
.checks{margin-top:28px;padding:20px 24px;background:#fff;border:1px solid var(--line);
  border-radius:10px}
.checks h2{margin:0 0 10px;font-size:16px}
.checks ul{margin:0;padding-left:20px}
footer{margin-top:32px;color:var(--muted);font-size:12px}
@media(max-width:640px){body{padding:24px 14px}table{font-size:13px}}
"""


def render_html(plan: dict) -> str:
    """排期方案的 HTML：自包含单文件，可直接打开或转发。"""
    s = plan["summary"]
    days = plan.get("days") or (
        (date.fromisoformat(plan["end_date"]) - date.fromisoformat(plan["start_date"])).days + 1
    )
    title = f"{days} 天排期 · {plan['lane']}"

    def stat(value: object, label: str) -> str:
        return f'<div class="stat"><b>{value}</b><span>{label}</span></div>'

    cards = "".join(
        [
            stat(s["total_posts"], "总篇数"),
            stat(len(s["by_phase"]), "阶段"),
            stat(len(s["by_pillar"]), "内容支柱"),
            stat(len(s["by_type"]), "笔记类型"),
        ]
    )

    rows = [
        "<tr>"
        f'<td class="d">D{it["day_index"]}</td>'
        f'<td class="num">{it["date"]}<span class="wd">周{it["weekday"]}</span></td>'
        f"<td>{it['phase']}</td>"
        f"<td>{it['pillar']}</td>"
        f'<td class="title">{html.escape(str(it["title"]))}</td>'
        f"<td>{it['note_type']}</td>"
        f'<td class="num">{it["publish_at"]}</td>'
        f'<td><span class="st {it["status"]}">'
        f'{_STATUS_LABEL.get(it["status"], it["status"])}</span></td>'
        "</tr>"
        for it in plan["items"]
    ]

    checks = plan.get("checks") or []
    check_items = "".join(f"<li>{html.escape(str(c))}</li>" for c in checks) or "<li>通过</li>"
    phase_split = " · ".join(f"{k} {v} 篇" for k, v in s["by_phase"].items())
    pillar_split = " · ".join(f"{k} {v} 篇" for k, v in s["by_pillar"].items())

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<div class="wrap">
  <h1>{html.escape(title)}</h1>
  <p class="sub">{plan["start_date"]} → {plan["end_date"]} · {s["total_posts"]} 篇</p>
  <div class="stats">{cards}</div>
  <p class="sub">阶段：{phase_split}</p>
  <p class="sub">支柱：{pillar_split}</p>
  <table>
    <thead>
      <tr><th>天</th><th>日期</th><th>阶段</th><th>支柱</th><th>选题</th>
      <th>类型</th><th>发布时间</th><th>状态</th></tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
  <section class="checks">
    <h2>排期自检</h2>
    <ul>{check_items}</ul>
  </section>
  <footer>由 xiaohongshu-growing 生成 · {datetime.now():%Y-%m-%d %H:%M}</footer>
</div>
</body>
</html>
"""


def export_report(plan: dict) -> Path:
    """导出排期方案 HTML 到工程根 reports/。"""
    path = report_dir() / f"plan-{plan['lane']}-{plan['start_date']}.html"
    path.write_text(render_html(plan), encoding="utf-8")
    return path


def export(plan: dict, fmt: str = "md") -> Path:
    """导出排期到 data/exports/。"""
    stem = f"plan-{plan['lane']}-{plan['start_date']}"
    out_dir = export_dir()
    if fmt == "csv":
        path = out_dir / f"{stem}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(
                fh,
                fieldnames=[
                    "day_index", "date", "weekday", "phase", "pillar", "template",
                    "title", "hook", "note_type", "publish_at", "status",
                ],
                extrasaction="ignore",
            )
            writer.writeheader()
            writer.writerows(plan["items"])
        return path
    path = out_dir / f"{stem}.md"
    path.write_text(render_markdown(plan), encoding="utf-8")
    return path


def shift_calendar(plan: dict, from_day: int, delta_days: int) -> dict:
    """整体顺延：某些天没发出去时，把后面的计划往后挪。"""
    for item in plan["items"]:
        if item["day_index"] >= from_day:
            item["day_index"] += delta_days
            new_date = date.fromisoformat(item["date"]) + timedelta(days=delta_days)
            item["date"] = new_date.isoformat()
            item["weekday"] = "一二三四五六日"[new_date.weekday()]
    plan["end_date"] = max(i["date"] for i in plan["items"])
    plan["checks"] = check_plan(plan)
    return plan


def progress(plan_id: int) -> dict:
    """某份排期的执行进度。"""
    with store.connect() as conn:
        plan = store.get_plan(conn, plan_id)
    if not plan:
        raise ValueError(f"排期 {plan_id} 不存在")
    items = plan["items"]
    done = [i for i in items if i["status"] in ("published", "done")]
    return {
        "plan_id": plan_id,
        "total": len(items),
        "published": len(done),
        "rate": round(len(done) / len(items), 3) if items else 0.0,
        "pending": [i for i in items if i["status"] == "planned"][:5],
    }


def next_items(plan_id: int, limit: int = 3) -> list[dict]:
    """接下来要发的几条，让 Agent 可以直接进入写稿流程。"""
    with store.connect() as conn:
        plan = store.get_plan(conn, plan_id)
    if not plan:
        raise ValueError(f"排期 {plan_id} 不存在")
    pending = [i for i in plan["items"] if i["status"] == "planned"]
    pending.sort(key=lambda x: x["day_index"])
    return pending[:limit]
