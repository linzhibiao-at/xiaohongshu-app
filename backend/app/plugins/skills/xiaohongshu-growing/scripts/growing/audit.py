"""发布前评分：把 pre-publish-analyzer 的评分卡接到草稿上。

分工：
- 机器只判能从文本确定的子维度（赛道词命中、结构、长度、风险词）
- 封面主观质量、对标差异化、趋势契合必须由 Agent 补判，这里标 pending

权重与 `references/rednote-rubrics.md` 保持一致，改这里必须同步改那份文件，
tests/test_audit.py 会交叉校验。

不输出 100 分总分。机器判分只覆盖能确定的那部分，
冒充总分会把「封面没看、对标没比」这件事藏起来。
"""

from __future__ import annotations

import re

from . import copy as copy_mod
from .config import Lane

# ─── 评分卡结构 ───────────────────────────────────────────────────────────────

RUBRIC: tuple[dict, ...] = (
    {
        "key": "cover",
        "name": "封面首屏吸引力",
        "weight": 25,
        "subs": (
            ("promise_clarity", "承诺清晰", 7),
            ("thumbnail", "缩略图可读", 6),
            ("contrast", "视觉对比与主体聚焦", 5),
            ("curiosity", "好奇/冲突", 4),
            ("native", "平台原生感", 3),
        ),
    },
    {
        "key": "title",
        "name": "标题与搜索关键词",
        "weight": 20,
        "subs": (
            ("search_intent", "搜索意图关键词", 6),
            ("click_hook", "点击钩子", 5),
            ("specificity", "具体性", 4),
            ("audience_fit", "人群匹配", 3),
            ("cover_match", "标题与封面一致", 2),
        ),
    },
    {
        "key": "topic",
        "name": "选题痛点与差异化",
        "weight": 20,
        "subs": (
            ("pain_intensity", "痛点强度", 6),
            ("audience_precision", "目标人群精准", 4),
            ("new_angle", "对比同类的新角度", 4),
            ("trend_fit", "趋势/搜索契合", 3),
            ("ip_relevance", "创作者/IP 关联", 3),
        ),
    },
    {
        "key": "structure",
        "name": "卡片叙事与收藏价值",
        "weight": 20,
        "subs": (
            ("first_hook", "首卡钩子", 4),
            ("card_logic", "卡间/段间逻辑", 4),
            ("rhythm", "扫读节奏", 4),
            ("save_value", "清单/模板/行动价值", 5),
            ("ending", "结尾记忆点", 3),
        ),
    },
    {
        "key": "interaction",
        "name": "互动设计与发布就绪",
        "weight": 15,
        "subs": (
            ("comment", "评论触发", 4),
            ("save_share", "收藏分享触发", 4),
            ("cta", "CTA/关注理由", 3),
            ("publish_fields", "发布字段与话题", 2),
            ("risk", "风险与过度承诺", 2),
        ),
    },
)

# 需要 Agent 补判的子维度，以及为什么机器判不了
PENDING_REASONS: dict[tuple[str, str], str] = {
    ("cover", "promise_clarity"): "要看封面的信息层级：一句话能不能读懂这篇给什么",
    ("cover", "thumbnail"): "要在信息流缩略图尺寸下看字是否还认得出",
    ("cover", "contrast"): "要看主体与背景的明暗对比、视觉落点",
    ("cover", "curiosity"): "要判断封面是否制造了信息缺口",
    ("cover", "native"): "要对照同赛道当前主流封面的风格",
    ("title", "cover_match"): "标题承诺与封面画面是否对得上，需要同时看图",
    ("topic", "new_angle"): "要与同类笔记的实际内容比较，本地无对标语料",
    ("topic", "trend_fit"): "要与当前热点池/搜索需求比对，本次未拉取",
}

# 三个目标场景下优先看的子维度（rubrics 的「推荐优先级」）
GOAL_PRIORITY: dict[str, tuple[str, ...]] = {
    "IP增长": ("ip_relevance", "cta", "ending"),
    "产品转化": ("pain_intensity", "save_value", "comment"),
    "爆文流量": ("search_intent", "click_hook", "first_hook"),
}

# ─── 词表与模式 ───────────────────────────────────────────────────────────────

_HOOK_QUESTION = ("怎么", "为什么", "多少", "哪家", "哪些", "要不要", "吗", "？", "?")
_HOOK_CONTRAST = (
    "别再", "竟然", "居然", "原来", "才知道", "后悔", "不看", "错的",
    "踩坑", "翻车", "避坑", "反而", "其实",
)
_HOOK_RESULT = ("花了", "省了", "一共", "清单", "攻略", "亲测", "实测", "整理")

PAIN_WORDS = (
    "踩坑", "踩雷", "后悔", "翻车", "太贵", "被坑", "白花钱", "智商税",
    "不会选", "难吃", "难用", "排队", "落差", "掉头", "别去", "浪费",
)

AUDIENCE_WORDS = (
    "一个人", "新手", "小白", "宝妈", "上班族", "学生", "情侣", "亲子",
    "本地人", "游客", "老人", "独居", "租房", "通勤", "打工人", "第一次",
)

SAVE_WORDS = ("收藏", "码住", "存下", "清单", "保存", "转给", "抄作业", "照着")
FOLLOW_WORDS = ("关注", "这个号", "接下来", "下一篇", "会一直", "长期更新", "我每周")

# 可复制的动作词：读者能照着做，才有收藏价值
ACTION_WORDS = (
    "标出", "按顺序", "给三样", "直接", "照着", "记录", "先问", "看这", "只点",
    "人均", "必点", "几点去", "怎么走", "判断标准",
)

_NUMBERED = re.compile(r"^\s*(?:\d+\s*[｜|、.）)]|[①②③④⑤⑥⑦⑧⑨⑩])", re.M)
_TAG_LINE = re.compile(r"^\s*(?:#[^\s#]+\s*)+$")
_UNIT_NUM = re.compile(
    r"\d+\s*(?:元|块|家|天|分钟|小时|年|个月|个|次|km|公里|㎡|平|折|w|万)"
)
# 身份信息：“在厦门住了8年”这种句子才是 IP 背书，单一个“我”不算
_IDENTITY = re.compile(
    r"[^，。\n]{0,10}\d+\s*年[^，。\n]{0,6}"
    r"|我(?:在|住|做|吃|拍|带|跑|干)[^，。\n]{0,12}"
    r"|作为[^，。\n]{0,12}"
)
# 结尾的邀请信号：不只是疑问句，指向评论区也算
_END_INVITE = (*_HOOK_QUESTION, "你", "你们", "告诉我", "评论区", "下一篇")


# ─── 拆解输入 ─────────────────────────────────────────────────────────────────


def split_tags(body: str) -> tuple[str, list[str]]:
    """把正文末尾的 `#标签 #标签` 行剥出来，返回 (正文, 标签)。"""
    lines = (body or "").rstrip().split("\n")
    tags: list[str] = []
    while lines and _TAG_LINE.match(lines[-1]):
        tags = re.findall(r"#([^\s#]+)", lines[-1]) + tags
        lines.pop()
    return "\n".join(lines).rstrip(), tags


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def _lane_terms(lane: Lane) -> list[str]:
    """赛道词表：名称 + 采集关键词 + 内容支柱拆词。

    pillars 是「住宿选择」「拍照机位」这类 4 字复合词，整词在标题里几乎不会
    字面出现，所以按 2+2 拆开：「住宿选择」-> 「住宿」「选择」。
    """
    terms = [lane.name, *lane.keywords]
    for pillar in lane.pillars:
        for part in re.split(r"[/、]", pillar):
            if len(part) <= 2:
                terms.append(part)
            else:
                terms.extend((part[:2], part[-2:]))
    return [t for t in terms if t]


def _lane_audience_terms(lane: Lane) -> list[str]:
    parts = re.split(r"[、,，/\s和与]+", lane.audience or "")
    return [p for p in parts if len(p) >= 2 and p not in ("人群", "用户")]


# ─── 打分项 ───────────────────────────────────────────────────────────────────


def _item(dim: str, sub: str, label: str, score: float, full: int,
          evidence: list[str] | None = None, advice: str = "") -> dict:
    return {
        "dim": dim,
        "sub": sub,
        "label": label,
        "pending": False,
        "score": round(max(0.0, min(float(full), score)), 1),
        "max": full,
        "evidence": evidence or [],
        "advice": advice,
    }


def _pending(dim: str, sub: str, label: str, full: int) -> dict:
    return {
        "dim": dim,
        "sub": sub,
        "label": label,
        "pending": True,
        "max": full,
        "reason": PENDING_REASONS.get((dim, sub), "需要人工/Agent 判断"),
    }


def _score_cover() -> list[dict]:
    """封面 25 分全部交给 Agent：机器只看图，看不出信息层级。"""
    return [
        _pending("cover", sub, label, full)
        for sub, label, full in RUBRIC[0]["subs"]
    ]


def _score_title(title: str, lane: Lane, corpus: str) -> list[dict]:
    text = (title or "").strip()
    low = text.lower()
    items: list[dict] = []

    # 搜索意图：标题里有没有这个赛道的搜索词
    lane_terms = _lane_terms(lane)
    hits = [t for t in lane_terms if t.lower() in low]
    if hits:
        score = 3.0 if len(hits) == 1 else 6.0
        ev: list[str] = hits
    else:
        # 没有赛道词，但带行程/预算这类可搜索的限定结构，也算半个搜索型标题
        # （「厦门旅拍｜3天2晚人均800」搜得到，只是没命中预设词表）
        structured = bool(re.search(r"\d+\s*(?:天|日|晚)", text)) or bool(_UNIT_NUM.search(text))
        score = 3.0 if structured else 0.0
        ev = ["有行程/预算结构，但没命中赛道词"] if structured else []
    items.append(_item(
        "title", "search_intent", "搜索意图关键词", score, 6, ev,
        "" if score >= 6 else "标题缺赛道搜索词，用户搜的时候找不到你；"
                             f"从这些词里挑一个放进标题：{'、'.join(lane_terms[:4])}",
    ))

    # 点击钩子：数字 / 提问 / 反差 / 结果，四类里命中越多越好
    kinds = []
    if re.search(r"\d", text):
        kinds.append("数字")
    if any(w in text for w in _HOOK_QUESTION):
        kinds.append("提问")
    if any(w in text for w in _HOOK_CONTRAST):
        kinds.append("反差")
    if any(w in text for w in _HOOK_RESULT):
        kinds.append("结果")
    items.append(_item(
        "title", "click_hook", "点击钩子", len(kinds) * 1.8, 5, kinds,
        "" if len(kinds) >= 3 else "标题只有陈述，没有让人想点的理由；"
                                   "加一个数字或一句反差（如「别再按游客攻略吃」）",
    ))

    # 具体性：有带单位的数字才算具体
    unit_hits = _UNIT_NUM.findall(text)
    if unit_hits:
        spec, ev = 4.0, unit_hits
    elif re.search(r"\d", text):
        spec, ev = 2.5, ["有数字但无单位"]
    else:
        spec, ev = 0.0, []
    items.append(_item(
        "title", "specificity", "具体性", spec, 4, ev,
        "" if spec >= 4 else "标题没有可核对的数字；加人均、价位、天数、店数任意一个",
    ))

    # 人群匹配：标题里能不能看出写给谁
    aud = [w for w in AUDIENCE_WORDS if w in text]
    aud += [w for w in _lane_audience_terms(lane) if w in text and w not in aud]
    items.append(_item(
        "title", "audience_fit", "人群匹配", 3.0 if aud else 0.0, 3, aud,
        "" if aud else "标题看不出写给谁；用「一个人/新手/本地人」这类词锚定人群",
    ))

    items.append(_pending("title", "cover_match", "标题与封面一致", 2))
    return items


def _score_topic(title: str, body: str, lane: Lane) -> list[dict]:
    corpus = f"{title}\n{body}"
    items: list[dict] = []

    # 痛点强度：正文有没有把代价说清楚
    pain = [w for w in PAIN_WORDS if w in corpus]
    n = len(pain)
    score = 0.0 if n == 0 else (3.0 if n == 1 else (4.5 if n <= 3 else 6.0))
    items.append(_item(
        "topic", "pain_intensity", "痛点强度", score, 6, pain,
        "" if score >= 4.5 else "痛点写得太轻；把「踩过什么坑、花了多少冤枉钱」写进开头",
    ))

    # 人群精准：正文提到了具体的人
    aud = [w for w in AUDIENCE_WORDS if w in corpus]
    aud += [w for w in _lane_audience_terms(lane) if w in corpus and w not in aud]
    items.append(_item(
        "topic", "audience_precision", "目标人群精准", min(4.0, len(aud) * 1.5), 4, aud,
        "" if len(aud) >= 2 else "正文没有具体人群，读起来像写给所有人",
    ))

    items.append(_pending("topic", "new_angle", "对比同类的新角度", 4))
    items.append(_pending("topic", "trend_fit", "趋势/搜索契合", 3))

    # IP 关联：有没有创作者身份落在文字里
    ident = _IDENTITY.findall(corpus)
    if ident:
        ip, ev = 3.0, [i.strip() for i in ident[:3]]
    elif "我" in corpus:
        ip, ev = 1.5, ["出现第一人称，但没有身份信息"]
    else:
        ip, ev = 0.0, []
    items.append(_item(
        "topic", "ip_relevance", "创作者/IP 关联", ip, 3, ev,
        "" if ip >= 3 else "看不出你是谁、凭什么由你来讲；补一句身份（住了几年、做过什么）",
    ))
    return items


def _score_structure(body: str) -> list[dict]:
    paras = _paragraphs(body)
    items: list[dict] = []
    p0 = paras[0] if paras else ""

    # 首卡钩子：第一段能不能一句话立住
    hook = 0.0
    ev: list[str] = []
    if re.search(r"\d", p0):
        hook += 2.0
        ev.append("首段有数字")
    if any(w in p0 for w in PAIN_WORDS + _HOOK_CONTRAST):
        hook += 1.0
        ev.append("首段有痛点或反差")
    if p0 and len(p0) <= 80:
        hook += 1.0
        ev.append("首段短，不铺垫")
    items.append(_item(
        "structure", "first_hook", "首卡钩子", hook, 4, ev,
        "" if hook >= 3 else "首段太长或太虚；压到 80 字内，直接给结论或代价",
    ))

    # 段间逻辑：分段 + 编号结构
    numbered = _NUMBERED.findall(body or "")
    logic = (2.0 if len(paras) >= 3 else (1.0 if len(paras) >= 2 else 0.0))
    logic += 2.0 if numbered else 0.0
    items.append(_item(
        "structure", "card_logic", "卡间/段间逻辑", logic, 4,
        [f"{len(paras)} 段"] + ([f"{len(numbered)} 处编号"] if numbered else []),
        "" if logic >= 4 else "段落之间没有递进关系；用 1｜2｜3 把结构显性化",
    ))

    # 扫读节奏：段短、段多、有小标题
    rhythm = 0.0
    rev: list[str] = []
    if paras:
        avg = sum(len(p) for p in paras) / len(paras)
        if avg <= 120:
            rhythm += 2.0
            rev.append(f"平均段长 {avg:.0f} 字")
        if len(paras) >= 4:
            rhythm += 1.0
            rev.append(f"{len(paras)} 段，有节奏")
        if numbered:
            rhythm += 1.0
            rev.append("有小标题")
    items.append(_item(
        "structure", "rhythm", "扫读节奏", rhythm, 4, rev,
        "" if rhythm >= 3 else "段落偏长，手机上要滑很久才见到下一个信息点",
    ))

    # 收藏价值：清单 + 数字 + 可复制动作
    save = 0.0
    sv: list[str] = []
    if numbered:
        save += 2.0
        sv.append("有编号清单")
    num_count = len(_UNIT_NUM.findall(body or ""))
    if num_count >= 3:
        save += 2.0
        sv.append(f"{num_count} 处带单位数字")
    elif num_count:
        save += 1.0
        sv.append(f"{num_count} 处带单位数字")
    actions = [w for w in ACTION_WORDS if w in (body or "")]
    if actions:
        save += 1.0
        sv.append("有可复制动作：" + "、".join(actions[:3]))
    items.append(_item(
        "structure", "save_value", "清单/模板/行动价值", save, 5, sv,
        "" if save >= 4 else "读完没有能存下来的东西；给一个照做就行的清单或判断标准",
    ))

    # 结尾记忆点
    end = paras[-1] if paras else ""
    ending = 0.0
    ev = []
    if any(w in end for w in ("只做", "就一件事", "记住", "一句话", "总结", "边界")):
        ending += 2.0
        ev.append("结尾有概括句")
    if any(w in end for w in _END_INVITE):
        ending += 1.0
        ev.append("结尾有邀请")
    if end and len(end) <= 100:
        ending += 1.0
        ev.append("结尾短")
    items.append(_item(
        "structure", "ending", "结尾记忆点", ending, 3, ev,
        "" if ending >= 2 else "结尾没有收住；用一句话说清这个号长期给你什么",
    ))
    return items


def _score_interaction(body: str, tags: list[str]) -> list[dict]:
    text = body or ""
    items: list[dict] = []

    comment = 0.0
    ev: list[str] = []
    if any(w in text for w in _HOOK_QUESTION):
        comment += 2.0
        ev.append("有提问")
    if "你" in text:
        comment += 1.0
        ev.append("对读者说话")
    if "评论区" in text:
        comment += 1.0
        ev.append("明确指向评论区")
    items.append(_item(
        "interaction", "comment", "评论触发", comment, 4, ev,
        "" if comment >= 3 else "没有让人想开口的问题；结尾问一个一句话能答的具体问题",
    ))

    saves = [w for w in SAVE_WORDS if w in text]
    items.append(_item(
        "interaction", "save_share", "收藏分享触发", min(4.0, len(saves) * 2.0), 4, saves,
        "" if saves else "没有给收藏或转发的理由；点明「下次用得上」的场景",
    ))

    follow = [w for w in FOLLOW_WORDS if w in text]
    items.append(_item(
        "interaction", "cta", "CTA/关注理由", 3.0 if follow else 0.0, 3, follow,
        "" if follow else "没有关注理由；说清关注之后读者能持续拿到什么",
    ))

    n = len(tags)
    if 3 <= n <= 8:
        field, fev = 2.0, [f"{n} 个话题"]
    elif 1 <= n <= 10:
        field, fev = 1.0, [f"{n} 个话题，不在 3-8 的甜区"]
    else:
        field, fev = 0.0, [f"{n} 个话题"]
    items.append(_item(
        "interaction", "publish_fields", "发布字段与话题", field, 2, fev,
        "" if field >= 2 else "话题数量不在 3-8；核心词 1-2 个 + 长尾 3-4 个最稳",
    ))

    risk = 0.0
    rev = []
    for word in copy_mod.ABSOLUTE_WORDS + copy_mod.EFFICACY_WORDS:
        if word in text:
            rev.append(word)
    for word in copy_mod.DIVERSION_WORDS:
        if word.lower() in text.lower():
            rev.append(word)
    if not rev:
        risk = 2.0
    items.append(_item(
        "interaction", "risk", "风险与过度承诺", risk, 2, rev,
        "" if not rev else "先删掉这些词再发：" + "、".join(rev[:4]),
    ))
    return items


# ─── 封面事实 ─────────────────────────────────────────────────────────────────


def _cover_facts(path: str) -> dict:
    """只报封面客观事实，不判封面好坏。"""
    if not path:
        return {"provided": False}
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
    except Exception as exc:
        return {"provided": True, "path": path, "error": str(exc)[:80]}
    ratio = round(w / h, 3) if h else 0.0
    return {
        "provided": True,
        "path": path,
        "size": [w, h],
        "ratio": ratio,
        "is_portrait_3_4": abs(ratio - 0.75) <= 0.03,
    }


# ─── 汇总 ─────────────────────────────────────────────────────────────────────


def audit(title: str, body: str, lane: Lane, cover: str = "",
          goal: str = "") -> dict:
    """对一篇草稿跑评分卡，返回机器可判分与待补判项。"""
    body_only, tags = split_tags(body)
    all_items = (
        _score_cover()
        + _score_title(title, lane, body_only)
        + _score_topic(title, body_only, lane)
        + _score_structure(body_only)
        + _score_interaction(body_only, tags)
    )
    items = [i for i in all_items if not i["pending"]]
    pending = [i for i in all_items if i["pending"]]

    machine_score = sum(i["score"] for i in items)
    machine_max = sum(i["max"] for i in items)
    pending_max = sum(p["max"] for p in pending)

    # 按维度聚合，便于和 rubrics 的五维表对照
    dims = []
    for dim in RUBRIC:
        got = sum(i["score"] for i in items if i["dim"] == dim["key"])
        cap = sum(i["max"] for i in items if i["dim"] == dim["key"])
        lost = sum(p["max"] for p in pending if p["dim"] == dim["key"])
        dims.append({
            "key": dim["key"],
            "name": dim["name"],
            "weight": dim["weight"],
            "machine_score": round(got, 1),
            "machine_max": cap,
            "pending_max": lost,
            "subs": [i for i in items if i["dim"] == dim["key"]],
            "pending": [p for p in pending if p["dim"] == dim["key"]],
        })

    # 优先修改清单：按丢分多少排，目标场景下的关键项整体提前
    boost = GOAL_PRIORITY.get(goal, ())
    lost_items = sorted(
        (i for i in items if i["advice"]),
        key=lambda i: (i["sub"] in boost, i["max"] - i["score"]),
        reverse=True,
    )
    edits = [
        {
            "rank": idx + 1,
            "where": i["label"],
            "lost": round(i["max"] - i["score"], 1),
            "advice": i["advice"],
        }
        for idx, i in enumerate(lost_items[:5])
    ]

    body_check = copy_mod.check_body(body_only, tags)
    return {
        "lane": lane.name,
        "goal": goal or "",
        "machine_score": round(machine_score, 1),
        "machine_max": machine_max,
        "pending_max": pending_max,
        "scale_note": (
            f"机器判分只覆盖 {machine_max}/100 的可确定部分，"
            f"另有 {pending_max} 分必须由 Agent 看图、比同类后补判。这不是总分。"
        ),
        "dimensions": dims,
        "cover": _cover_facts(cover),
        "edits": edits,
        "risk": {"blocking": body_check.issues, "warnings": body_check.warnings},
        "tags": tags,
    }
