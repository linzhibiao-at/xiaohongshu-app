"""跨平台热点层：导入 / 趋势 / 错位判断，以及对赛道打分的影响。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from growing import calendar, hotspot, store, track
from growing.config import resolve_lane
from growing_cli import build_parser


def _write(tmp_path, items, name="hot.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _day(offset: int = 0) -> str:
    return (datetime.now() - timedelta(days=offset)).isoformat(timespec="seconds")


@pytest.fixture
def travel_notes():
    """往库里塞 25 条旅行样本，让赛道打分有数据可用。"""
    with store.connect() as conn:
        for i in range(25):
            store.upsert_note(
                conn,
                {
                    "feed_id": f"t{i:03d}",
                    "title": f"厦门citywalk第{i}天路线",
                    "keyword": "旅行攻略",
                    "lane": "旅行出行",
                    "author_id": f"u{i % 5}",
                    "author_name": f"作者{i % 5}",
                    "liked": 300 + i * 40,
                    "collected": 200 + i * 30,
                    "comment": 20 + i,
                    "shared": 10 + i,
                },
            )
    return conn


# ─── ingest ──────────────────────────────────────────────────────────────────


def test_ingest_parses_units_and_skips_bad_rows(tmp_path):
    path = _write(
        tmp_path,
        [
            {"keyword": "厦门旅拍", "heat": "980万", "rank": 3, "url": "https://x/1"},
            {"word": "鼓浪屿", "hot": 12.5, "rank": 8},
            {"no_keyword": True},
        ],
    )
    result = hotspot.ingest(path, "douyin", lane_name="旅行出行")
    assert result["accepted"] == 2
    assert result["skipped"] == 1
    with store.connect() as conn:
        rows = store.list_hotspot(conn, lane="旅行出行")
    by_kw = {r["keyword"]: r for r in rows}
    assert by_kw["厦门旅拍"]["heat_value"] == 9_800_000
    assert by_kw["鼓浪屿"]["heat_value"] == 12.5
    assert by_kw["厦门旅拍"]["source_level"] == "L3"
    assert result["strength"].startswith("只能作方向提示")


def test_ingest_rejects_unknown_platform(tmp_path):
    path = _write(tmp_path, [{"keyword": "a", "heat": 1}])
    with pytest.raises(ValueError):
        hotspot.ingest(path, "kuaishou")


def test_ingest_same_day_overwrites(tmp_path):
    hotspot.ingest(
        _write(tmp_path, [{"keyword": "厦门旅拍", "heat": 100}], "a.json"),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(0),
    )
    hotspot.ingest(
        _write(tmp_path, [{"keyword": "厦门旅拍", "heat": 900}], "b.json"),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(0),
    )
    with store.connect() as conn:
        rows = store.list_hotspot(conn, platform="douyin")
    assert len(rows) == 1
    assert rows[0]["heat_value"] == 900


# ─── trend ──────────────────────────────────────────────────────────────────


def test_trend_baseline_then_delta(tmp_path):
    hotspot.ingest(
        _write(tmp_path, [{"keyword": "厦门旅拍", "heat": 100}], "a.json"),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(2),
    )
    hotspot.ingest(
        _write(tmp_path, [{"keyword": "厦门旅拍", "heat": 900}], "b.json"),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(0),
    )
    result = hotspot.trend("旅行出行", days=30)
    assert result["count"] == 1
    item = result["items"][0]
    assert item["appearances"] == 2
    assert item["trend"] == "上升"
    assert item["sustained"] is False
    assert any("至少隔天再采" in c for c in result["caveats"]) is False


def test_trend_single_capture_is_baseline(tmp_path):
    hotspot.ingest(
        _write(tmp_path, [{"keyword": "厦门旅拍", "heat": 100}], "a.json"),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(0),
    )
    result = hotspot.trend("旅行出行", days=30)
    item = result["items"][0]
    assert item["delta"] == 0
    assert "基线" in item["trend"]
    assert any("至少隔天再采" in c for c in result["caveats"])


# ─── gaps ───────────────────────────────────────────────────────────────────


def test_gaps_flags_spill_when_xhs_has_no_supply(travel_notes, tmp_path):
    hotspot.ingest(
        _write(tmp_path, [{"keyword": "厦门旅拍", "heat": 9800000, "rank": 2}]),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(0),
    )
    result = hotspot.gaps("旅行出行", days=30)
    assert result["available"] is True
    assert result["opportunities"]
    top = result["opportunities"][0]
    assert top["keyword"] == "厦门旅拍"
    assert top["xhs_supply"] == 0
    assert "站内仅 0 条" in top["verdict"]
    assert "外溢缝隙" in result["verdict"]["conclusion"]


def test_gaps_without_external_data_is_explicit():
    result = hotspot.gaps("旅行出行", days=30)
    assert result["available"] is False
    # 没有外部热点是常态，不能用“无法判断”堵住后续动作
    assert "不代表赛道没有机会" in result["message"]
    assert "无法判断" in result["verdict"]["conclusion"]
    assert result["opportunities"] == []
    assert result["station_demand"] == []
    assert result["pool"] == []


def test_gaps_without_xhs_samples_refuses_to_call_opportunity(tmp_path):
    """站内一条都没采时，不能把 supply=0 当成“需求没被接住”。"""
    hotspot.ingest(
        _write(tmp_path, [{"keyword": "厦门旅拍", "heat": 9800000, "rank": 1}]),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(0),
    )
    result = hotspot.gaps("旅行出行", days=30)
    assert result["xhs_sampled"] is False
    assert result["opportunities"] == []
    assert result["unverified"]
    assert "无法判断" in result["verdict"]["conclusion"]
    assert "collect lane" in result["verdict"]["next"]
    assert any("尚无任何站内样本" in c for c in result["caveats"])


def test_gaps_marks_native_when_xhs_only(travel_notes, tmp_path):
    hotspot.ingest(
        _write(tmp_path, [{"keyword": "旅行攻略", "heat": 10, "rank": 30}]),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(0),
    )
    result = hotspot.gaps("旅行出行", days=30)
    native = [r for r in result["native"] if r["keyword"] == "旅行攻略"]
    assert native
    assert native[0]["xhs_supply"] == 25


# ─── 与赛道打分的关系 ─────────────────────────────────────────────────────────


def test_external_heat_not_in_total_by_default(travel_notes, tmp_path):
    with store.connect() as conn:
        before = track.score_lane(conn, "旅行出行", fit=60)
    assert before["external_heat"]["available"] is False

    hotspot.ingest(
        _write(tmp_path, [{"keyword": "厦门旅拍", "heat": 9800000, "rank": 1}]),
        "douyin",
        lane_name="旅行出行",
        captured_at=_day(0),
    )
    with store.connect() as conn:
        after = track.score_lane(conn, "旅行出行", fit=60)
        weighted = track.score_lane(conn, "旅行出行", fit=60, weights={"external": 0.2})

    assert after["external_heat"]["available"] is True
    assert after["external_in_total"] is False
    assert after["score"] == before["score"]
    assert weighted["external_in_total"] is True


def test_external_weight_ignored_when_no_data(travel_notes):
    """显式开了 external 权重但没采外部数据时，不能拿 0 分去惩罚。"""
    with store.connect() as conn:
        plain = track.score_lane(conn, "旅行出行", fit=60)
        asked = track.score_lane(conn, "旅行出行", fit=60, weights={"external": 0.2})
    assert asked["external_in_total"] is False
    assert asked["score"] == plain["score"]


# ─── 自由赛道 ────────────────────────────────────────────────────────────────


def test_custom_lane_fallback():
    lane = resolve_lane("厦门旅拍")
    assert lane.is_custom is True
    assert lane.key == "厦门旅拍"
    assert lane.keywords == ("厦门旅拍",)
    assert len(lane.pillars) >= 4
    # 四大赛道与别名仍走预设
    assert resolve_lane("母婴").key == "母婴育儿"
    assert resolve_lane("母婴育儿").is_custom is False
    with pytest.raises(ValueError):
        resolve_lane("   ")


def test_custom_lane_runs_through_scoring_and_plan():
    with store.connect() as conn:
        result = track.score_lane(conn, "厦门旅拍", fit=70)
    assert result["lane_is_custom"] is True
    assert result["data_status"] == "empty"
    assert "external_heat" in result

    # 没样本时不得凭空生成排期
    with pytest.raises(ValueError, match="选题池为空"):
        calendar.build_plan("厦门旅拍", start="2026-11-11", days=30)

    topics = [
        {"pillar": "入门指南", "title": "厦门旅拍新手怎么开始", "template": "清单体"}
    ] * 3
    plan = calendar.build_plan("厦门旅拍", start="2026-11-11", days=30, topics=topics)
    assert plan["lane"] == "厦门旅拍"
    assert plan["summary"]["total_posts"] == 17


# ─── CLI ────────────────────────────────────────────────────────────────────


def test_cli_parser_accepts_hotspot_commands():
    parser = build_parser()
    args = parser.parse_args(
        ["hotspot", "ingest", "--platform", "douyin", "--file", "/tmp/h.json"]
    )
    assert (args.group, args.action) == ("hotspot", "ingest")
    assert args.source_level == "L3"

    args = parser.parse_args(["hotspot", "gaps", "--lane", "厦门旅拍", "--days", "7"])
    assert args.lane == "厦门旅拍"
    assert args.days == 7

    args = parser.parse_args(["track", "compare", "--weights", "external=0.1"])
    assert args.weights == "external=0.1"


def test_cli_parser_accepts_hotwords():
    parser = build_parser()
    args = parser.parse_args(
        ["hotspot", "hotwords", "--platform", "douyin", "--lane", "厦门旅拍", "--limit", "8"]
    )
    assert (args.group, args.action) == ("hotspot", "hotwords")
    assert args.platform == "douyin"
    assert args.limit == 8

    # 微信和小红书以外的平台名要在解析阶段就被挡掉
    with pytest.raises(SystemExit):
        parser.parse_args(["hotspot", "hotwords", "--platform", "weibo"])
