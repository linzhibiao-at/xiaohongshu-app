"""L1 层：抖音热榜直连与小红书站内需求词。

这两个通道都不依赖 sdk 运行时：
  抖音走公开接口，用 monkeypatch 顶掉网络；
  小红书走站内 CLI，用 monkeypatch 顶掉 hot_suggest。
用例只验证解析、绑定、口径标注和降级语义，不发真实请求。
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from growing import hotspot, store


class _FakeResponse:
    """模拟 urlopen 返回的上下文管理器。"""

    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _fake_douyin(monkeypatch, words: list[tuple[str, float]]) -> None:
    payload = {
        "word_list": [{"word": w, "hot_value": h, "label": 0} for w, h in words]
    }
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload))


def _fake_xhs(monkeypatch, suggestions: list[str], tags: list[str] | None = None) -> None:
    payload = {
        "keyword": "厦门旅拍",
        "suggestions": [
            {"text": s, "type": "top_note", "search_type": "notes"} for s in suggestions
        ],
        "tags": [{"name": t, "origin_text": f"厦门旅拍{t}"} for t in (tags or [])],
    }
    monkeypatch.setattr(hotspot.xhs, "hot_suggest", lambda kw: payload)


# ─── 抖音热榜 ────────────────────────────────────────────────────────────────


def test_pull_douyin_binds_matched_words_to_lane(monkeypatch):
    _fake_douyin(monkeypatch, [("厦门旅拍攻略", 9800000), ("某明星塌房", 8700000)])
    result = hotspot.pull_douyin("厦门旅拍")

    assert result["source_level"] == "L1"
    assert result["fetched"] == 2
    assert result["matched_count"] == 1
    assert result["matched"][0]["keyword"] == "厦门旅拍攻略"
    assert result["matched"][0]["lane"] == "厦门旅拍"
    # 没命中的词进热榜池，不绑定赛道，避免污染赛道的错位判断
    assert [p["keyword"] for p in result["pool_top"]] == ["某明星塌房"]
    assert result["pool_top"][0]["lane"] == ""


def test_pull_douyin_without_match_keeps_pool_and_explains(monkeypatch):
    """命中 0 条是细分赛道的常态，提示里必须说清它不代表赛道不行。"""
    _fake_douyin(monkeypatch, [("某明星塌房", 8700000)])
    result = hotspot.pull_douyin("厦门旅拍")

    assert result["matched_count"] == 0
    assert result["pool_top"]
    assert "不代表赛道不行" in result["note"]
    assert "hotspot hotwords" in result["note"]


def test_pull_douyin_without_lane_puts_everything_in_pool(monkeypatch):
    _fake_douyin(monkeypatch, [("热点甲", 100.0), ("热点乙", 90.0)])
    result = hotspot.pull_douyin()

    assert result["lane"] == ""
    assert result["matched_count"] == 0
    assert len(result["pool_top"]) == 2


def test_pull_douyin_persists_with_heat_basis(monkeypatch):
    _fake_douyin(monkeypatch, [("厦门旅拍攻略", 9800000)])
    hotspot.pull_douyin("厦门旅拍")

    with store.connect() as conn:
        rows = store.list_hotspot(conn, lane="厦门旅拍")
    assert len(rows) == 1
    assert rows[0]["source_level"] == "L1"
    assert rows[0]["meta"]["heat_basis"] == "hot_value"
    assert rows[0]["heat_value"] == 9800000.0


# ─── 小红书站内需求词 ─────────────────────────────────────────────────────────


def test_pull_xhs_insights_stores_rank_basis(monkeypatch):
    _fake_xhs(monkeypatch, ["厦门旅拍摄影师", "厦门旅拍妆造"], tags=["鼓浪屿"])
    result = hotspot.pull_xhs_insights("厦门旅拍")

    assert result["source_level"] == "L1"
    assert result["pulled"] == 2
    first = result["signals"][0]
    assert first["rank"] == 1
    # 联想词没有热度值，必须明确标注口径是位次，不能冒充热度
    assert first["heat_value"] == 0.0
    assert first["meta"]["heat_basis"] == "search_rank"
    # 话题只作选题灵感，不入库
    assert result["topics"][0]["name"] == "鼓浪屿"

    with store.connect() as conn:
        rows = store.list_hotspot(conn, lane="厦门旅拍")
    assert len(rows) == 2
    assert all(r["platform"] == "xhs" for r in rows)


def test_normalize_separates_heat_basis():
    """同一平台下不同口径不能混算，否则互动分会把位次挤成 0。"""
    rows = [
        {
            "platform": "xhs",
            "keyword": "互动样本",
            "heat_value": 500.0,
            "rank": 0,
            "meta": {"heat_basis": "engagement_median"},
        },
        {
            "platform": "xhs",
            "keyword": "搜索建议",
            "heat_value": 0.0,
            "rank": 1,
            "meta": {"heat_basis": "search_rank"},
        },
    ]
    out = {r["keyword"]: r for r in hotspot._normalize(rows)}

    # 互动口径里只有一条样本，按单样本规则给中性值，不给满分
    assert out["互动样本"]["heat_norm"] == 50.0
    # 搜索位次口径里 rank=1 折算满分
    assert out["搜索建议"]["heat_norm"] == 100.0


def test_gaps_without_external_offers_station_demand(monkeypatch):
    """没有外部热点时不能返回空壳，站内需求词仍然要摆出来。"""
    _fake_xhs(monkeypatch, ["厦门旅拍摄影师", "厦门旅拍妆造"])
    hotspot.pull_xhs_insights("厦门旅拍")

    result = hotspot.gaps("厦门旅拍")

    assert result["available"] is False
    assert [r["keyword"] for r in result["station_demand"]] == [
        "厦门旅拍摄影师",
        "厦门旅拍妆造",
    ]
    assert "无法判断外溢机会" in result["verdict"]["conclusion"]


def test_gaps_pool_only_contains_unbound_douyin(monkeypatch):
    """热榜池只收没绑定赛道的词，绑定过的不能重复出现在池里。"""
    _fake_douyin(monkeypatch, [("厦门旅拍攻略", 9800000), ("某明星塌房", 8700000)])
    hotspot.pull_douyin("厦门旅拍")

    result = hotspot.gaps("厦门旅拍")
    # 这条赛道有外部热点（命中一条），所以走的是主路径而不是无外部分支
    assert result["available"] is True
    assert result["count"] >= 1


def test_pull_xhs_insights_requires_lane():
    with pytest.raises(ValueError):
        hotspot.pull_xhs_insights("")
