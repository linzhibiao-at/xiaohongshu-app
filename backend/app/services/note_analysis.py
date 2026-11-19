"""One-note analysis for the public "paste a note link" flow.

Nothing is stored and no LLM call is made: the connector reads the note's
numbers and this module turns them into a rule-based read-out, so the endpoint
can stay open to visitors who have not logged in yet.
"""

import re

from app.xhs.base import NoteSnapshot

# Rough public benchmarks used only to phrase the read-out.
_BENCH = {"ctr": 0.08, "engagement_rate": 0.05, "collect_rate": 0.02, "comment_rate": 0.01}

_DETAIL_RE = re.compile(r"(?:explore|discovery/item|note)/([0-9a-zA-Z]{6,})")
_RAW_ID_RE = re.compile(r"^[0-9a-zA-Z]{20,}$")


def extract_note_ref(url: str) -> str:
    """Pull the note id out of a xiaohongshu note link (or accept a bare id)."""
    text = (url or "").strip()
    if not text:
        raise ValueError("请输入小红书笔记链接")
    if "xhslink.com" in text:
        raise ValueError("暂不支持分享短链，请在小红书里复制含 /explore/ 的完整笔记链接")
    match = _DETAIL_RE.search(text)
    if match:
        return match.group(1)
    if _RAW_ID_RE.match(text):
        return text
    raise ValueError("没认出笔记 ID，请粘贴形如 https://www.xiaohongshu.com/explore/xxxx 的链接")


def _rate(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def analyze(note: NoteSnapshot) -> dict:
    """Turn one note's numbers into rates, a 0-100 score and plain-language advice."""
    views = note.views
    rates = {
        "ctr": _rate(views, note.impressions),
        "engagement_rate": _rate(note.likes + note.collects + note.comments, views),
        "collect_rate": _rate(note.collects, views),
        "comment_rate": _rate(note.comments, views),
    }

    ctr_ratio = min(1.0, rates["ctr"] / _BENCH["ctr"])
    eng_ratio = min(1.0, rates["engagement_rate"] / _BENCH["engagement_rate"])
    collect_ratio = min(1.0, rates["collect_rate"] / _BENCH["collect_rate"])
    comment_ratio = min(1.0, rates["comment_rate"] / _BENCH["comment_rate"])
    score = round(100 * (0.35 * ctr_ratio + 0.30 * eng_ratio + 0.20 * collect_ratio + 0.15 * comment_ratio))

    suggestions: list[str] = []
    if rates["ctr"] < _BENCH["ctr"] * 0.6:
        suggestions.append(
            f"点击率 {_pct(rates['ctr'])}，低于常见水平 {_pct(_BENCH['ctr'])}："
            "封面要一眼给出结果或对比，标题补上数字和具体对象，让人有点进来的理由。"
        )
    elif rates["ctr"] >= _BENCH["ctr"]:
        suggestions.append(
            f"点击率 {_pct(rates['ctr'])} 高于常见水平：这套封面 + 标题的钩子可以沉淀成模板，"
            "换选题继续复用。"
        )

    if rates["collect_rate"] < _BENCH["collect_rate"] * 0.6:
        suggestions.append(
            f"收藏率只有 {_pct(rates['collect_rate'])}：内容偏观点、缺少可复用的东西。"
            "加步骤清单、参数表或模板，读者才有「先存下来」的动机。"
        )

    if rates["comment_rate"] < _BENCH["comment_rate"] * 0.6:
        suggestions.append(
            f"评论率只有 {_pct(rates['comment_rate'])}：结尾没有抛问题。"
            "最后一句改成提问、征集经验或「评论区扣 1 领清单」，把读者往评论里带。"
        )

    if rates["engagement_rate"] >= _BENCH["engagement_rate"]:
        suggestions.append(
            f"互动率 {_pct(rates['engagement_rate'])} 跑赢基准：这个选题方向值得做成系列，"
            "下一篇沿用同一结构、换具体案例。"
        )

    if not suggestions:
        suggestions.append("各项指标接近常见水平：先固定封面和标题的写法，再连续发布 3-5 篇观察变化。")

    if score >= 75:
        verdict = "点击和互动都跑赢了常见水平，属于可以复用的爆款结构。"
    elif score >= 55:
        verdict = "整体中上，补齐下面的短板后数据还有明显空间。"
    elif score >= 35:
        verdict = "数据一般，「点进来」和「愿意互动」两头都需要再优化。"
    else:
        verdict = "数据偏弱，建议先把封面和标题的问题解决，再谈互动和涨粉。"

    return {
        "note": {
            "id": note.xhs_note_id,
            "title": note.title,
            "note_type": note.note_type,
            "tags": note.tags,
            "published_at": note.published_at,
        },
        "metrics": {
            "impressions": note.impressions,
            "views": views,
            "likes": note.likes,
            "collects": note.collects,
            "comments": note.comments,
            "shares": note.shares,
            "new_followers": note.new_followers,
        },
        "rates": rates,
        "score": score,
        "verdict": verdict,
        "suggestions": suggestions,
    }
