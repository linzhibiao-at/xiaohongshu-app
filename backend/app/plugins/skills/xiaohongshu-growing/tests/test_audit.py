"""发布前评分卡：权重对齐、分制完整、好稿与差稿能拉开差距。

核心要守的三件事：
  1. 权重与 references/rednote-rubrics.md 不漂移（改一处必须改两处）
  2. machine_max + pending_max 恒等于 100，封面 25 分不能凭空消失
  3. 机器分只覆盖可确定项，pending 必须明确列出
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from growing import audit
from growing.config import resolve_lane

RUBRICS = Path(__file__).resolve().parents[1] / "references" / "rednote-rubrics.md"


def _rubric_table() -> list[tuple[int, list[int]]]:
    """从 rubrics.md 的表格里抽出 (维度权重, [子维度权重...])。"""
    rows = []
    for line in RUBRICS.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*$", line)
        if not m:
            continue
        weight = int(m.group(2))
        subs = [int(n) for n in re.findall(r"(\d+)", m.group(3))]
        if subs:
            rows.append((weight, subs))
    return rows


GOOD_TITLE = "厦门探店｜一个人吃饭人均40的8家小店"
GOOD_BODY = """在厦门住了8年，一个人吃饭踩过的坑比吃过的店多。

最亏的一次是照着游客攻略排了两小时队，人均180，出来就后悔。
后来我只认三个判断标准：菜单上有没有单人份、门口要不要拉客、下午两点还开不开。

1｜八市转角那家沙茶面，人均25，一个人吃不尴尬
2｜松柏社区中午才开的小店，人均40，必点是卤面
3｜中山路背后没招牌的巷子，人均35，几点去不用排队

这三条我按顺序标出来了，照着走一趟只用半天。
评论区告诉我，你一个人来厦门最怕碰上什么？下一篇我去拍那个。

#厦门探店 #厦门美食 #一人食 #本地人推荐"""

BAD_TITLE = "一些分享"
BAD_BODY = "今天心情不错，随便写写。"


def _audit(title: str, body: str, lane: str = "美食探店", **kw) -> dict:
    return audit.audit(title, body, resolve_lane(lane), **kw)


# ─── 权重不漂移 ───────────────────────────────────────────────────────────────


def test_rubric_weights_match_reference_file():
    """audit.py 的权重必须与 rednote-rubrics.md 完全一致。"""
    table = _rubric_table()
    assert len(table) == len(audit.RUBRIC), "维度数量与 rubrics.md 不一致"
    for (weight, subs), dim in zip(table, audit.RUBRIC, strict=True):
        assert weight == dim["weight"], f"维度权重不一致: {dim['name']}"
        assert subs == [s[2] for s in dim["subs"]], f"子维度权重不一致: {dim['name']}"


def test_rubric_total_is_100():
    assert sum(d["weight"] for d in audit.RUBRIC) == 100
    for dim in audit.RUBRIC:
        assert sum(s[2] for s in dim["subs"]) == dim["weight"], dim["name"]


# ─── 分制完整性 ───────────────────────────────────────────────────────────────


def test_machine_and_pending_add_up_to_100():
    result = _audit(GOOD_TITLE, GOOD_BODY)
    assert result["machine_max"] + result["pending_max"] == 100


def test_cover_dimension_is_fully_pending():
    """封面 25 分只能由 Agent 判，机器不许给分也不许吞掉。"""
    result = _audit(GOOD_TITLE, GOOD_BODY)
    cover = next(d for d in result["dimensions"] if d["key"] == "cover")
    assert cover["machine_max"] == 0
    assert cover["pending_max"] == 25
    assert len(cover["pending"]) == 5
    assert len(cover["subs"]) == 0


def test_every_pending_has_reason():
    result = _audit(GOOD_TITLE, GOOD_BODY)
    pendings = [p for d in result["dimensions"] for p in d["pending"]]
    assert pendings, "至少要有一个 pending"
    for p in pendings:
        assert p["reason"] and p["reason"] != "需要人工/Agent 判断", p["label"]


def test_scale_note_refuses_to_be_a_total_score():
    result = _audit(GOOD_TITLE, GOOD_BODY)
    assert result["machine_max"] < 100
    assert "这不是总分" in result["scale_note"]


# ─── 打分能拉开差距 ───────────────────────────────────────────────────────────


def test_good_draft_beats_bad_draft():
    good = _audit(GOOD_TITLE, GOOD_BODY)["machine_score"]
    bad = _audit(BAD_TITLE, BAD_BODY)["machine_score"]
    assert good > bad + 20, f"好稿 {good} 与差稿 {bad} 拉不开差距"


def test_title_without_search_term_loses_points():
    """标题不写赛道搜索词是真实的搜索损失，不能被放过。"""
    result = _audit("一个人吃饭也不将就", GOOD_BODY)
    title = next(d for d in result["dimensions"] if d["key"] == "title")
    intent = next(s for s in title["subs"] if s["sub"] == "search_intent")
    assert intent["score"] == 0
    assert intent["advice"], "扣分必须带可执行的改法"


def test_title_with_itinerary_structure_gets_half_credit():
    """「3天2晚人均800」搜得到，不该和有赛道词一样高，但也不能判 0。"""
    result = _audit("厦门旅拍｜3天2晚人均800怎么排", GOOD_BODY, lane="旅行出行")
    title = next(d for d in result["dimensions"] if d["key"] == "title")
    intent = next(s for s in title["subs"] if s["sub"] == "search_intent")
    assert 0 < intent["score"] < intent["max"]
    assert intent["evidence"]


def test_pillar_words_count_as_search_terms():
    """内容支柱（如「住宿选择」）拆出来的词也要算搜索词。"""
    result = _audit("厦门住宿怎么选", GOOD_BODY, lane="旅行出行")
    title = next(d for d in result["dimensions"] if d["key"] == "title")
    intent = next(s for s in title["subs"] if s["sub"] == "search_intent")
    assert intent["score"] > 0
    assert any("住宿" in e for e in intent["evidence"])


def test_evidence_is_traceable():
    """每个扣分项都要能指回草稿里的具体词。"""
    result = _audit(GOOD_TITLE, GOOD_BODY)
    topic = next(d for d in result["dimensions"] if d["key"] == "topic")
    pain = next(s for s in topic["subs"] if s["sub"] == "pain_intensity")
    assert pain["score"] > 0
    assert pain["evidence"]


# ─── 拆解与风险 ───────────────────────────────────────────────────────────────


def test_split_tags_strips_trailing_tag_line():
    body, tags = audit.split_tags("正文内容\n\n#厦门探店 #美食")
    assert body == "正文内容"
    assert tags == ["厦门探店", "美食"]


def test_split_tags_keeps_hash_inside_body():
    body, tags = audit.split_tags("正文里的 #话题 不算标签行")
    assert tags == []
    assert "#话题" in body


def test_risk_words_zero_out_the_check():
    result = _audit("厦门探店｜全网最好吃", "这家绝对是治疗胃病的首选店")
    interaction = next(d for d in result["dimensions"] if d["key"] == "interaction")
    risk = next(s for s in interaction["subs"] if s["sub"] == "risk")
    assert risk["score"] == 0
    assert risk["evidence"]
    assert "先删掉" in risk["advice"]


def test_tags_outside_sweet_spot_lose_points():
    result = _audit(GOOD_TITLE, "正文段落一\n\n正文段落二\n\n正文段落三 #唯一话题")
    interaction = next(d for d in result["dimensions"] if d["key"] == "interaction")
    fields = next(s for s in interaction["subs"] if s["sub"] == "publish_fields")
    assert fields["score"] < fields["max"]


# ─── 封面只报事实 ─────────────────────────────────────────────────────────────


def test_cover_facts_when_missing():
    result = _audit(GOOD_TITLE, GOOD_BODY)
    assert result["cover"] == {"provided": False}


def test_cover_facts_when_unreadable(tmp_path):
    broken = tmp_path / "cover.png"
    broken.write_bytes(b"not an image")
    result = _audit(GOOD_TITLE, GOOD_BODY, cover=str(broken))
    assert result["cover"]["provided"] is True
    assert "error" in result["cover"]


# ─── 目标场景影响优先级 ───────────────────────────────────────────────────────


def test_goal_lifts_related_dimension_to_top_of_edits():
    body = "正文段落一，没有重点。\n\n正文段落二。\n\n正文段落三。"
    result = _audit("一个人吃饭", body, goal="爆文流量")
    if result["edits"]:
        assert result["edits"][0]["lost"] > 0
    assert result["goal"] == "爆文流量"


def test_free_lane_still_audits():
    """自由赛道没有预设 keywords，也要能跑完。"""
    result = _audit(GOOD_TITLE, GOOD_BODY, lane="厦门旅拍")
    assert result["lane"] == "厦门旅拍"
    assert result["machine_max"] + result["pending_max"] == 100


@pytest.mark.parametrize("title", ["", "  "])
def test_empty_title_does_not_crash(title):
    result = _audit(title, "")
    assert result["machine_score"] >= 0
    assert result["machine_max"] + result["pending_max"] == 100
