"""文案工具：不做"一键生成"，负责写作 brief、结构化校验与合规预检。

分工：
- Agent 负责写文案（有语义和情感判断）
- 本模块负责确定性校验（长度、结构、违禁词、话题数量）与写作骨架
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

TITLE_MAX = 20  # 小红书标题按 UTF-16 计长，emoji 占 2

# 广告绝对化用语（《广告法》第九条相关），建议规避
ABSOLUTE_WORDS = (
    "最好", "最佳", "最强", "最高级", "第一品牌", "国家级", "最佳品牌",
    "独一无二", "顶级", "首选", "100%", "绝对", "唯一", "史上最", "全网最",
)
# 医疗/功效宣称风险词，育儿与家居赛道尤其容易踩
EFFICACY_WORDS = (
    "治疗", "治愈", "药效", "消炎", "抗病毒", "根治", "无副作用",
    "祛疾", "疗效", "包治", "抗癌", "壮阳", "丰胸", "减肥神",
)
# 站外导流风险词，轻则限流重则处罚
DIVERSION_WORDS = (
    "加微信", "加我v", "加V", "vx", "VX", "薇信", "私信我发", "扫码加",
    "淘宝搜", "拼多多搜", "代购微信",
)

CTA_PATTERNS = (
    r"评论区", r"留言", r"收藏", r"码住", r"记得", r"告诉我", r"你们", r"蹲", r"求",
)


def utf16_len(text: str) -> int:
    """小红书标题长度按 UTF-16 编码单元计算。"""
    return len(text.encode("utf-16-le")) // 2


@dataclass
class CheckResult:
    ok: bool = True
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "issues": self.issues,
            "warnings": self.warnings,
            "metrics": self.metrics,
        }


def check_title(title: str) -> CheckResult:
    result = CheckResult()
    text = (title or "").strip()
    length = utf16_len(text)
    result.metrics = {"chars": len(text), "utf16": length, "max": TITLE_MAX}
    if not text:
        result.ok = False
        result.issues.append("标题为空")
        return result
    if length > TITLE_MAX:
        result.ok = False
        result.issues.append(f"标题 {length} 字符，超过上限 {TITLE_MAX}，需压缩")
    if "\n" in text:
        result.ok = False
        result.issues.append("标题内含换行")
    for word in ABSOLUTE_WORDS:
        if word in text:
            result.warnings.append(f"含绝对化用语「{word}」，有合规风险")
    for word in DIVERSION_WORDS:
        if word.lower() in text.lower():
            result.warnings.append(f"含导流词「{word}」，可能被限流")
    if not re.search(r"\d|[一二三四五六七八九十]", text):
        result.warnings.append("标题没有数字，清单类/结果类选题加上数字通常点击更高")
    if not any(word in text for word in ("｜", "|", "：", ":", "，", ",")):
        result.warnings.append("标题没有分隔符，信息层次不清晰（建议用｜切分卖点）")
    return result


def check_body(body: str, tags: list[str] | None = None) -> CheckResult:
    result = CheckResult()
    text = (body or "").strip()
    paragraphs = [p for p in re.split(r"\n\s*\n|\n", text) if p.strip()]
    tags = tags or []
    result.metrics = {
        "chars": len(text),
        "paragraphs": len(paragraphs),
        "tags": len(tags),
    }
    if len(text) < 60:
        result.warnings.append("正文偏短，缺少支撑信息，收藏动机不足")
    if len(text) > 1000:
        result.ok = False
        result.issues.append(f"正文 {len(text)} 字，超过 1000 字上限")
    if len(paragraphs) < 3:
        result.warnings.append("段落过少，建议拆成 3 段以上（钩子 / 主体 / 结论）")
    if not any(re.search(p, text) for p in CTA_PATTERNS):
        result.warnings.append("没有互动钩子，评论率可能偏低")
    if len(tags) < 3:
        result.warnings.append("话题标签少于 3 个，搜索曝光会打折")
    if len(tags) > 8:
        result.warnings.append("话题标签超过 8 个，容易显得堆砌")
    for word in ABSOLUTE_WORDS + EFFICACY_WORDS:
        if word in text:
            result.warnings.append(f"含风险词「{word}」，发布前请确认合规")
    for word in DIVERSION_WORDS:
        if word.lower() in text.lower():
            result.warnings.append(f"含导流词「{word}」，可能被限流")
    return result


def check_tags(tags: list[str]) -> CheckResult:
    result = CheckResult()
    cleaned = [t.strip().lstrip("#") for t in tags if t and t.strip()]
    result.metrics = {"count": len(cleaned), "tags": cleaned}
    if len(cleaned) != len(set(cleaned)):
        result.warnings.append("存在重复话题")
    if any(" " in t for t in cleaned):
        result.warnings.append("话题中含空格，可能被截断")
    return result


def build_brief(topic: dict, lane: dict) -> dict:
    """给 Agent 的写作骨架：不是成品文案，而是必须命中的信息点。"""
    hooks = {
        "清单体": "开头直接给结论（一共几个），中间逐条给一句话理由，结尾给适用人群",
        "避坑体": "开头放大错误代价，中间 3-5 个坑按严重程度排序，结尾给替代方案",
        "对照体": "开头摆出两难，中间给判断标准而不是结论，结尾亮选择与适用边界",
        "结果体": "开头放结果画面/数字，中间倒推过程，结尾给可复制的最小步骤",
        "身份体": "开头用人设背书，中间给筛选逻辑，结尾给一句可执行的建议",
        "场景体": "开头还原具体场景，中间给自己的做法，结尾邀请对照自己的做法",
        "预算体": "开头给总预算，中间分项报价，结尾给可增减的弹性清单",
        "流程体": "开头承诺看完就能做，中间按步骤编号，结尾给常见失败点",
    }
    return {
        "goal": "让读者收藏或评论，而不是单纯点赞",
        "hook": hooks.get(topic.get("template", ""), "开头给结论，不要铺垫"),
        "must_include": [
            "具体数字（价格/数量/时长/次数至少一个）",
            "一个可复制的动作或判断标准",
            f"与「{topic.get('pillar', '')}」直接相关的场景细节",
        ],
        "avoid": [
            "空泛形容词（很好用、超级棒）",
            "无来源的效果承诺",
            "大段感慨与自我抒情（除人设篇外）",
        ],
        "cta": f"围绕「{topic.get('pillar', '')}」提一个只需一句话就能回答的问题",
        "title_limit": TITLE_MAX,
        "audience": lane.get("audience", ""),
        "asset_logic": lane.get("asset_logic", ""),
        "publish_note": "正文里不要出现站外联系方式",
    }


def assemble(
    title: str,
    body: str,
    tags: list[str],
    topic_id: str = "",
) -> dict:
    """汇总校验结果，并给出可直接落盘的正文文本。"""
    title_check = check_title(title)
    body_check = check_body(body, tags)
    tag_check = check_tags(tags)
    tag_line = " ".join(f"#{t}" for t in tags)
    content = body.strip()
    if tag_line:
        content = f"{content}\n\n{tag_line}"
    ok = title_check.ok and body_check.ok
    return {
        "ok": ok,
        "topic_id": topic_id,
        "title": title.strip(),
        "title_check": title_check.to_dict(),
        "body_check": body_check.to_dict(),
        "tag_check": tag_check.to_dict(),
        "content": content,
        "blocking": title_check.issues + body_check.issues,
    }
