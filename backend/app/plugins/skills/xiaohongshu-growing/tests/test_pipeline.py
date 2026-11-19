"""端到端离线链路：采集 → 赛道判断 → 对标 → 选题 → 排期 → 复盘。"""

from __future__ import annotations

import json

import pytest

import fake
from growing import benchmark, calendar, collect, copy, review, store, topic, track, xhs
from growing_cli import build_parser


@pytest.fixture
def patched(monkeypatch):
    """把底层 CLI 调用换成本地假数据，并去掉节流等待。"""
    monkeypatch.setattr(xhs, "search", lambda keyword, **kw: fake.all_feeds())
    monkeypatch.setattr(
        xhs, "feed_detail", lambda fid, token="", **kw: fake.feed_detail(fid)
    )
    monkeypatch.setattr(xhs, "user_profile", lambda uid, token="": fake.user_profile(uid))
    monkeypatch.setattr(collect.time, "sleep", lambda seconds: None)
    return monkeypatch


@pytest.fixture
def seeded(patched):
    """跑一次完整的赛道采集。"""
    cfg = collect.CollectConfig(
        keywords=["宝宝辅食", "母婴好物", "绘本推荐"],
        per_keyword=20,
        detail_top=10,
        with_authors=True,
        author_top=5,
    )
    return collect.collect_lane("母婴育儿", cfg)


def test_collect_lane_writes_data(seeded):
    assert seeded["lane"] == "母婴育儿"
    assert seeded["details"] == 10
    with store.connect() as conn:
        count = conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"]
        authors = conn.execute("SELECT COUNT(*) c FROM authors").fetchone()["c"]
        searches = conn.execute("SELECT COUNT(*) c FROM searches").fetchone()["c"]
    assert count >= 20
    assert authors >= 1
    assert searches >= 3


def test_collect_lane_retries_on_error(patched):
    calls = {"n": 0}

    def flaky(keyword, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise xhs.XhsError("模拟风控")
        return fake.all_feeds()

    patched.setattr(xhs, "search", flaky)
    cfg = collect.CollectConfig(keywords=["宝宝辅食", "母婴好物"], detail_top=0, with_fresh=False)
    stats = collect.collect_lane("母婴育儿", cfg)
    assert stats["failed"]
    assert stats["searched"] == 1


def test_track_compare_produces_ranking(seeded):
    result = track.compare_lanes(fits={"母婴育儿": 85})
    assert len(result["ranking"]) == 4
    assert result["recommendation"]["primary"]
    assert result["caveats"]
    mother = next(r for r in result["ranking"] if r["lane"] == "母婴育儿")
    assert mother["sample_size"] > 0
    assert 0 <= mother["score"] <= 100
    assert mother["dimensions"]["fit"] == 85


def test_track_keyword_heat(seeded):
    with store.connect() as conn:
        rows = track.keyword_heat(conn, "母婴育儿")
    assert rows
    assert {"keyword", "median_liked", "title_homogeneity"} <= set(rows[0])


def test_benchmark_pick_and_analyze(seeded):
    picked = benchmark.pick_benchmarks("母婴育儿", per_tier=1)
    assert picked["benchmarks"]
    uid = picked["benchmarks"][0]["user_id"]
    detail = benchmark.analyze_author(uid, "母婴育儿")
    assert detail["summary"]["count"] > 0
    assert detail["copyable"]
    assert detail["not_copyable"]


def test_topic_pool_has_no_duplicates(seeded):
    pool = topic.build_pool("母婴育儿")
    assert pool["topics"]
    titles = [t["title"] for t in pool["topics"]]
    assert len(titles) == len(set(titles))
    first = pool["topics"][0]
    assert {"pillar", "template", "competition", "collect_intent"} <= set(first)


def test_calendar_build_and_save(seeded):
    plan = calendar.build_plan("母婴育儿", start="2026-11-11", days=30)
    assert plan["summary"]["total_posts"] == 17
    assert plan["items"][0]["pillar"] == "人设建立"
    assert plan["items"][0]["date"] == "2026-11-11"
    assert plan["end_date"] == "2026-12-10"
    # 没有"同一天发两条"这类硬问题
    hard_issues = [c for c in plan["checks"] if "同天发布" in c or "人设篇" in c]
    assert not hard_issues

    plan_id = calendar.save(plan)
    assert plan_id > 0
    with store.connect() as conn:
        saved = store.get_plan(conn, plan_id)
    assert len(saved["items"]) == plan["summary"]["total_posts"]
    assert calendar.progress(plan_id)["published"] == 0
    assert len(calendar.next_items(plan_id, limit=3)) == 3


def test_calendar_export(seeded, tmp_path):
    plan = calendar.build_plan("母婴育儿", start="2026-11-11", days=30)
    md = calendar.export(plan, "md")
    csv_path = calendar.export(plan, "csv")
    assert md.exists() and md.read_text(encoding="utf-8").startswith("# ")
    assert csv_path.exists()
    lines = csv_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == plan["summary"]["total_posts"] + 1


def test_calendar_export_html_report(seeded):
    """方案 HTML 落工程根 reports/，不是 data/exports/。"""
    plan = calendar.build_plan("母婴育儿", start="2026-11-11", days=30)
    path = calendar.export_report(plan)
    assert path.parent.name == "reports"
    assert path.name == f"plan-母婴育儿-{plan['start_date']}.html"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("<!DOCTYPE html>")
    assert "母婴育儿" in text
    # 标题天数跟 --days 一致，不是写死的 30
    assert f"{plan['days']} 天排期" in text
    # 表头 1 行 + 每天 1 行
    assert text.count("<tr>") == plan["summary"]["total_posts"] + 1
    assert "待写" in text

    # 不同状态要有各自的样式类，否则圆点颜色区分不出来
    plan["items"][0]["status"] = "published"
    plan["items"][1]["status"] = "writing"
    plan["items"][2]["status"] = "skipped"
    mixed = calendar.render_html(plan)
    for cls, label in (("published", "已发"), ("writing", "在写"), ("skipped", "跳过")):
        assert f'class="st {cls}"' in mixed
        assert label in mixed


def test_calendar_shift(seeded):
    plan = calendar.build_plan("母婴育儿", start="2026-11-11", days=30)
    before = {i["day_index"]: i["date"] for i in plan["items"]}
    shifted = calendar.shift_calendar(plan, from_day=8, delta_days=2)
    assert shifted["items"][0]["date"] == "2026-11-11"
    after = {i["day_index"]: i["date"] for i in shifted["items"]}
    # D8 之前不动，D8 及之后整体后移 2 天，且日期与天序号同步
    for day, date in before.items():
        if day < 8:
            assert after[day] == date
        else:
            assert day + 2 in after
            assert after[day + 2] > date


def test_copy_brief_and_check(seeded):
    lane = {"audience": "新手妈妈", "asset_logic": "清单收藏高"}
    brief = copy.build_brief({"pillar": "喂养辅食", "template": "清单体"}, lane)
    assert brief["title_limit"] == 20
    assert brief["must_include"]

    ok = copy.assemble(
        "5个辅食误区｜我踩过的",
        "第一段\n\n第二段\n\n第三段 评论区说说",
        ["辅食", "育儿", "母婴"],
    )
    assert ok["ok"] is True
    bad = copy.assemble("最好的辅食", "短", ["辅食"])
    assert any("绝对化" in w for w in bad["title_check"]["warnings"])


def test_review_note(seeded):
    result = review.note_review("feed001", "母婴育儿")
    assert result["baseline"]["count"] > 0
    assert result["diagnosis"]
    assert 0 <= result["percentile_in_lane"] <= 1
    assert result["growth"]["available"] is False


def test_review_weekly_without_profile(seeded):
    result = review.weekly_review()
    assert "message" in result


def test_self_profile_and_review(patched):
    patched.setattr(
        xhs, "user_profile", lambda uid, token="": fake.self_profile()
    )
    result = collect.collect_self_profile(
        "https://www.xiaohongshu.com/user/profile/u_self?xsec_token=abc&xsec_source=pc_note"
    )
    assert result["user_id"] == "u_self"
    assert result["fans"] == 12

    weekly = review.weekly_review()
    assert weekly["account"]["user_id"] == "u_self"
    assert weekly["verdict"]


def test_store_note_upsert_keeps_detail(seeded):
    with store.connect() as conn:
        before = store.get_note(conn, "feed001")
        assert before["body"]  # 详情已补全
        note = dict(before)
        note["liked"] = 99999
        note["body"] = ""
        store.upsert_note(conn, note)
        after = store.get_note(conn, "feed001")
    assert after["liked"] == 99999
    assert after["body"] == before["body"]


def test_cli_parser_accepts_core_commands():
    parser = build_parser()
    args = parser.parse_args(["track", "compare", "--fit", "母婴育儿=80,家居生活=55"])
    assert args.group == "track"
    assert args.fit.startswith("母婴")
    args = parser.parse_args(["plan", "build", "--lane", "母婴", "--save", "--export", "md"])
    assert args.save is True
    args = parser.parse_args(["copy", "check", "--title-file", "/tmp/t.txt", "--tags", "a", "b"])
    assert args.tags == ["a", "b"]


def test_collect_detail_merges_snapshot(seeded):
    with store.connect() as conn:
        note = store.get_note(conn, "feed005")
        detail = collect.collect_detail(conn, note)
        snaps = conn.execute(
            "SELECT COUNT(*) c FROM note_snapshots WHERE feed_id=?", ("feed005",)
        ).fetchone()["c"]
    assert detail["body"]
    assert snaps >= 1


def test_json_serializable(seeded):
    """所有对外输出必须能直接 json.dumps。"""
    result = track.compare_lanes(fits={"母婴育儿": 80})
    json.dumps(result, ensure_ascii=False, default=str)
    pool = topic.build_pool("母婴育儿")
    json.dumps(pool, ensure_ascii=False, default=str)
    plan = calendar.build_plan("母婴育儿", start="2026-11-11")
    json.dumps(plan, ensure_ascii=False, default=str)
