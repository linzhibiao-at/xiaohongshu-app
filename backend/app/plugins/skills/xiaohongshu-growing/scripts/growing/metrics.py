"""指标引擎：把小红书页面上的中文计数改造成可比较的数值，并提供赛道/笔记指标。

所有函数都是纯函数，方便单测。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import median

from .config import ENGAGE_WEIGHTS

_COUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([万亿wkKW])?")
_UNITS = {"万": 10_000, "w": 10_000, "W": 10_000, "k": 1_000, "K": 1_000, "亿": 100_000_000}


@dataclass(frozen=True)
class Count:
    """解析后的计数。is_lower_bound 为真表示原文带 + / + 以上，实际值只会更大。"""

    value: int = 0
    is_lower_bound: bool = False
    raw: str = ""

    def __int__(self) -> int:
        return self.value


def parse_count(raw: str | int | None) -> Count:
    """把 "1.2万" / "10w+" / "1,024" / "" 解析为整数。"""
    if raw is None:
        return Count(0, False, "")
    if isinstance(raw, int):
        return Count(raw, False, str(raw))
    text = str(raw).strip()
    if not text:
        return Count(0, False, "")
    lower_bound = "+" in text or "以上" in text
    match = _COUNT_RE.search(text.replace(",", ""))
    if not match:
        return Count(0, lower_bound, text)
    number = float(match.group(1))
    unit = match.group(2)
    if unit:
        number *= _UNITS.get(unit, 1)
    return Count(round(number), lower_bound, text)


def eng_score(liked: int, collected: int, comment: int, shared: int) -> float:
    """加权互动分。收藏/评论/分享比点赞更能代表内容价值。"""
    w = ENGAGE_WEIGHTS
    return (
        liked * w["liked"]
        + collected * w["collected"]
        + comment * w["comment"]
        + shared * w["shared"]
    )


def percentile(values: list[float], q: float) -> float:
    """线性插值分位数，q 取 0-1。空列表返回 0。"""
    if not values:
        return 0.0
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    pos = q * (len(data) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(data[lo])
    return float(data[lo] + (data[hi] - data[lo]) * (pos - lo))


def safe_div(a: float, b: float) -> float:
    return float(a) / float(b) if b else 0.0


def hhi(counts: list[int]) -> float:
    """赫芬达尔指数，衡量结果是否被少数作者垄断。0=极度分散，1=完全垄断。"""
    total = sum(counts)
    if total <= 0:
        return 0.0
    return sum((c / total) ** 2 for c in counts)


_TITLE_STOP = set("的了是我你他她它和与及在有这那就都也很吧呢啊吗一个怎么什么如何")


def title_shingles(title: str, n: int = 2) -> set[str]:
    """中文标题的字符 n-gram 集合，用于粗粒度同质化计算。"""
    clean = re.sub(r"[^\u4e00-\u9fa5a-zA-Z0-9]", "", title or "")
    clean = "".join(ch for ch in clean if ch not in _TITLE_STOP)
    if len(clean) < n:
        return {clean} if clean else set()
    return {clean[i : i + n] for i in range(len(clean) - n + 1)}


def title_homogeneity(titles: list[str]) -> float:
    """标题同质化程度 0-1：越高说明大家都在用同一套词，越难差异化。"""
    grams = [title_shingles(t) for t in titles if t]
    grams = [g for g in grams if g]
    if len(grams) < 3:
        return 0.0
    df: Counter[str] = Counter()
    for g in grams:
        df.update(g)
    # 出现在 >= 30% 标题中的 n-gram 视为"套路词"
    threshold = max(3, int(len(grams) * 0.3))
    common = {k for k, v in df.items() if v >= threshold}
    if not common:
        return 0.0
    scores = [len(g & common) / len(g) for g in grams]
    return round(min(1.0, sum(scores) / len(scores)), 3)


def detect_hook_patterns(titles: list[str]) -> list[dict]:
    """识别爆款标题常用的钩子模式，并统计命中率。只做统计，不做编造。"""
    patterns: list[tuple[str, str]] = [
        ("数字清单", r"\d+\s*[个种条款家道步招]|[一二三四五六七八九十]+\s*[个种条款家道步招]"),
        ("避坑否定", r"别|不要|避坑|踩雷|后悔|千万别|劝退"),
        ("疑问求解", r"怎么|如何|为什么|能不能|要不要|是不是"),
        ("身份锁定", r"新手|宝妈|租客|打工|学生|月薪|预算|贫民"),
        ("结果承诺", r"搞定|一口气|全攻略|一次说清|清单|汇总|指南|保姆级"),
        ("对比反差", r"vs|对比|差别|前后|竟然|没想到|以为"),
        ("地域限定", r"北京|上海|广州|深圳|杭州|成都|重庆|西安|武汉|南京|苏州"),
        ("时间限定", r"周末|五一|国庆|夏天|春天|秋冬|换季|30天|一周"),
    ]
    total = max(1, len(titles))
    out: list[dict] = []
    for name, pattern in patterns:
        regex = re.compile(pattern)
        hit = sum(1 for t in titles if regex.search(t or ""))
        if hit:
            out.append(
                {
                    "pattern": name,
                    "hits": hit,
                    "share": round(hit / total, 3),
                    "examples": [t for t in titles if regex.search(t or "")][:3],
                }
            )
    out.sort(key=lambda x: x["hits"], reverse=True)
    return out


@dataclass
class NoteMetrics:
    """单篇笔记的派生指标。"""

    feed_id: str
    title: str
    author_id: str
    author_name: str
    note_type: str
    liked: int
    collected: int
    comment: int
    shared: int
    publish_ts: int = 0
    tags: list[str] = field(default_factory=list)

    @property
    def engage(self) -> float:
        return eng_score(self.liked, self.collected, self.comment, self.shared)

    @property
    def collect_ratio(self) -> float:
        """收藏/点赞：越高说明内容越"有用"，适合做搜索长尾。"""
        return round(safe_div(self.collected, self.liked), 3)

    @property
    def comment_ratio(self) -> float:
        """评论/点赞：越高说明越有讨论欲，适合做人设。"""
        return round(safe_div(self.comment, self.liked), 3)

    @property
    def age_days(self) -> int:
        if not self.publish_ts:
            return 0
        import time

        return max(0, int((time.time() - self.publish_ts / 1000) // 86400))

    def to_row(self) -> dict:
        return {
            "feed_id": self.feed_id,
            "title": self.title,
            "author_id": self.author_id,
            "author_name": self.author_name,
            "note_type": self.note_type,
            "liked": self.liked,
            "collected": self.collected,
            "comment": self.comment,
            "shared": self.shared,
            "engage": round(self.engage, 1),
            "collect_ratio": self.collect_ratio,
            "comment_ratio": self.comment_ratio,
            "publish_ts": self.publish_ts,
            "age_days": self.age_days,
        }


def summarize_notes(notes: list[NoteMetrics], viral_floor: int = 500) -> dict:
    """对一组笔记汇总分布指标。viral_floor 是爆款的绝对下限。"""
    if not notes:
        return {
            "count": 0,
            "median_liked": 0,
            "p90_liked": 0,
            "median_engage": 0,
            "viral_threshold": viral_floor,
            "viral_count": 0,
            "viral_rate": 0.0,
            "median_collect_ratio": 0,
            "median_comment_ratio": 0,
            "author_count": 0,
            "author_hhi": 0.0,
            "top_author_share": 0.0,
            "title_homogeneity": 0.0,
            "fresh_ratio": 0.0,
            "video_ratio": 0.0,
            "hook_patterns": [],
        }
    liked = [n.liked for n in notes]
    engage = [n.engage for n in notes]
    threshold = max(viral_floor, int(percentile(liked, 0.90)))
    viral = [n for n in notes if n.liked >= threshold]
    author_counts = Counter(n.author_id or n.author_name for n in notes).most_common()
    total = len(notes)
    fresh = [n for n in notes if 0 < n.age_days <= 30]
    with_age = [n for n in notes if n.age_days > 0]
    return {
        "count": total,
        "median_liked": int(median(liked)),
        "p90_liked": int(percentile(liked, 0.90)),
        "median_engage": round(median(engage), 1),
        "viral_threshold": threshold,
        "viral_count": len(viral),
        "viral_rate": round(len(viral) / total, 3),
        "median_collect_ratio": round(median([n.collect_ratio for n in notes]), 3),
        "median_comment_ratio": round(median([n.comment_ratio for n in notes]), 3),
        "author_count": len(author_counts),
        "author_hhi": round(hhi([c for _, c in author_counts]), 3),
        "top_author_share": round(author_counts[0][1] / total, 3) if author_counts else 0.0,
        "title_homogeneity": title_homogeneity([n.title for n in notes]),
        "fresh_ratio": round(len(fresh) / len(with_age), 3) if with_age else 0.0,
        "video_ratio": round(len([n for n in notes if "视频" in n.note_type]) / total, 3),
        "hook_patterns": detect_hook_patterns([n.title for n in notes]),
    }
