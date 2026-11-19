"""指标引擎单测：中文计数、分位数、集中度、标题同质化。"""

from __future__ import annotations

import pytest

from growing import metrics
from growing.copy import assemble, check_title, utf16_len


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1.2万", 12000),
        ("10w+", 100000),
        ("1,024", 1024),
        ("999", 999),
        ("2.3万", 23000),
        ("", 0),
        (None, 0),
        ("1亿", 100000000),
        ("3.5k", 3500),
    ],
)
def test_parse_count(raw, expected):
    assert metrics.parse_count(raw).value == expected


def test_parse_count_lower_bound():
    assert metrics.parse_count("10万+").is_lower_bound is True
    assert metrics.parse_count("10万").is_lower_bound is False


def test_engage_weights_prefer_collect():
    a = metrics.eng_score(100, 0, 0, 0)
    b = metrics.eng_score(0, 100, 0, 0)
    assert b > a


def test_percentile():
    values = [1, 2, 3, 4, 5]
    assert metrics.percentile(values, 0.5) == 3
    assert metrics.percentile(values, 0.0) == 1
    assert metrics.percentile(values, 1.0) == 5
    assert metrics.percentile([], 0.9) == 0


def test_hhi_monopoly():
    assert metrics.hhi([10]) == pytest.approx(1.0)
    assert metrics.hhi([1, 1, 1, 1]) == pytest.approx(0.25)
    assert metrics.hhi([]) == 0


def test_title_homogeneity_detects_pattern():
    same = [
        "宝宝辅食清单推荐",
        "宝宝辅食清单合集",
        "宝宝辅食清单大全",
        "宝宝辅食清单整理",
    ]
    diff = [
        "宝宝第一次吃蛋黄吐了",
        "断奶第四天我差点放弃",
        "8平米儿童房的收纳方案",
        "绘本读到第10本",
    ]
    assert metrics.title_homogeneity(same) > metrics.title_homogeneity(diff)


def test_detect_hook_patterns():
    titles = ["5个辅食误区", "别再乱买婴儿车", "怎么选婴儿床", "300元搞定穿搭"]
    patterns = {p["pattern"] for p in metrics.detect_hook_patterns(titles)}
    assert "数字清单" in patterns
    assert "避坑否定" in patterns


def test_summarize_notes_empty():
    stats = metrics.summarize_notes([], viral_floor=500)
    assert stats["count"] == 0
    assert stats["viral_rate"] == 0.0


def test_utf16_title_limit():
    assert utf16_len("宝宝辅食") == 4
    assert utf16_len("🍼") == 2
    long_title = "这是一条非常非常长的标题超过了二十个字符的上限需要压缩"
    result = check_title(long_title)
    assert result.ok is False
    assert any("超过上限" in issue for issue in result.issues)


def test_assemble_blocks_long_title():
    result = assemble("一二三四五六七八九十一二三四五六七八九十一", "正文", ["母婴"])
    assert result["ok"] is False
    assert result["content"].endswith("#母婴")
