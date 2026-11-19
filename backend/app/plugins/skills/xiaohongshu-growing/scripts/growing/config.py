"""起号 agent 的全局配置：路径、四大赛道定义、评分权重。

约定：
- 本文件只放"稳定的静态知识"，不放运行时状态。
- 赛道定义里的商业价值权重是经验值，允许用户通过 track analyze --weights 覆盖。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ─── 路径 ────────────────────────────────────────────────────────────────────

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
AGENT_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = AGENT_ROOT.parent
XHS_SKILLS_ROOT = SKILLS_ROOT / "xiaohongshu-skills"
XHS_CLI = XHS_SKILLS_ROOT / "scripts" / "cli.py"


def xhs_python() -> str:
    """底层 CLI 使用的解释器。

    xiaohongshu-skills 依赖 requests/websockets，系统 python 往往没装，
    因此优先用它自带的 .venv，找不到才回退到当前解释器。
    """
    import sys

    for candidate in (
        XHS_SKILLS_ROOT / ".venv" / "bin" / "python",
        XHS_SKILLS_ROOT / ".venv" / "Scripts" / "python.exe",
    ):
        if candidate.exists():
            return str(candidate)
    return sys.executable


def data_dir() -> Path:
    """数据目录，可用 GROWING_DATA_DIR 覆盖（便于测试隔离）。"""
    env = os.environ.get("GROWING_DATA_DIR")
    root = Path(env).expanduser() if env else AGENT_ROOT / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root


def db_path() -> Path:
    return data_dir() / "growing.db"


def export_dir() -> Path:
    path = data_dir() / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_dir() -> Path:
    """方案文档目录：技能所在工程的 reports/。

    给人看的交付件（30 天排期、起号方案、复盘报告）落这里，统一用 HTML；
    草稿、封面、原始 CSV 这类过程数据仍走 export_dir()。
    """
    env = os.environ.get("GROWING_REPORT_DIR")
    root = Path(env).expanduser() if env else AGENT_ROOT.parents[2] / "reports"
    root.mkdir(parents=True, exist_ok=True)
    return root


# ─── 四大赛道 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Lane:
    """一个赛道的静态定义。"""

    key: str
    name: str
    emoji: str
    # 用于采集的种子关键词，第一个为主词
    keywords: tuple[str, ...]
    # 内容支柱：赛道内可长期复用的栏目
    pillars: tuple[str, ...]
    # 典型人群
    audience: str
    # 变现路径（经验性，需结合实际资源）
    monetize: tuple[str, ...]
    # 起号门槛：需要什么资源才能做得比别人好
    barrier: str
    # 内容方法：这个赛道什么样的内容容易被收藏
    asset_logic: str
    # 商业合作密度经验权重 0-100（母婴/家居品牌投放密集，旅行最低）
    commercial_weight: float
    # 是否为用户临时指定的自由赛道（非四大赛道预设）
    is_custom: bool = False

    def pillar_lines(self) -> str:
        return "、".join(self.pillars)


LANES: dict[str, Lane] = {
    "母婴育儿": Lane(
        key="母婴育儿",
        name="母婴育儿",
        emoji="\U0001F476",
        keywords=(
            "育儿经验",
            "宝宝辅食",
            "母婴好物",
            "亲子游",
            "绘本推荐",
            "产后修复",
            "儿童房布置",
            "早教",
        ),
        pillars=("喂养辅食", "成长发育", "亲子陪伴", "用品避坑", "绘本共读", "妈妈自我照顾"),
        audience="25-38 岁新手妈妈为主，少量爸爸；一二线及新一线城市占比高",
        monetize=(
            "品牌合作（奶粉/辅食/洗护/玩具）", "母婴好物带货",
            "育儿知识付费", "本地亲子服务",
        ),
        barrier="需要真实育儿场景与持续陪伴时间，人设可信度依赖自己的孩子与日常素材",
        asset_logic="清单型、避坑型、月龄对照型内容收藏率最高；评论区提问多，适合做系列",
        commercial_weight=95,
    ),
    "美食探店": Lane(
        key="美食探店",
        name="美食探店",
        emoji="\U0001F35C",
        keywords=(
            "探店",
            "本地美食",
            "餐厅推荐",
            "苍蝇馆子",
            "citywalk 美食",
            "网红店测评",
            "平价美食",
            "早餐",
        ),
        pillars=("单店测评", "区域清单", "价格带对比", "隐藏吃法", "排队避坑", "本地时令"),
        audience="18-35 岁本地生活人群，强地域属性，女性略多",
        monetize=("本地商家探店合作", "团购佣金", "美食节/市集合作", "跨城内容广告"),
        barrier="需要高频到店与稳定拍摄，地域绑定强，异地迁移成本高",
        asset_logic="清单+价格+地址+排队时长最实用；单店测评靠信息差与画面食欲感",
        commercial_weight=80,
    ),
    "旅行出行": Lane(
        key="旅行出行",
        name="旅行出行",
        emoji="\U0001F9F3",
        keywords=(
            "旅行攻略",
            "周末去哪儿",
            "citywalk",
            "小众旅行地",
            "自驾游",
            "亲子游攻略",
            "民宿推荐",
            "周边游",
        ),
        pillars=("目的地攻略", "路线规划", "住宿选择", "交通/预算", "拍照机位", "季节限定"),
        audience="20-40 岁计划出行人群，搜索意图强，决策周期长",
        monetize=("酒店/民宿合作", "OTA 分销", "目的地文旅合作", "装备广告"),
        barrier="需要真实出行频次与目的地覆盖，季节性强，内容衰减快",
        asset_logic="攻略型内容生命周期长但需要信息密度：预算、路线、时间表缺一不可",
        commercial_weight=65,
    ),
    "家居生活": Lane(
        key="家居生活",
        name="家居生活",
        emoji="\U0001F3E0",
        keywords=(
            "家居好物",
            "租房改造",
            "收纳",
            "装修避坑",
            "软装搭配",
            "厨房好物",
            "小户型",
            "清洁收纳",
        ),
        pillars=("空间改造", "好物清单", "收纳方法", "装修避坑", "清洁维护", "氛围布置"),
        audience="24-40 岁租房/买房人群，女性为主，装修与换季是强触发点",
        monetize=("家居品牌合作", "家装线索（本地）", "好物带货", "清洁/收纳品类复购"),
        barrier="需要可展示的空间与改造前后对比，素材门槛中等，可长期复用同一空间",
        asset_logic="前后对比+可复制步骤+价格清单，收藏与转发率都高，长尾流量好",
        commercial_weight=90,
    ),
}

LANE_ALIASES: dict[str, str] = {
    "母婴": "母婴育儿",
    "育儿": "母婴育儿",
    "母婴育儿": "母婴育儿",
    "美食": "美食探店",
    "探店": "美食探店",
    "美食探店": "美食探店",
    "旅行": "旅行出行",
    "旅游": "旅行出行",
    "旅行出行": "旅行出行",
    "家居": "家居生活",
    "家居家装": "家居生活",
    "家居生活": "家居生活",
}


# ─── 自由赛道兜底 ─────────────────────────────────────────────────────────────

# 用户指定四大赛道之外的赛道时，用这套通用配置起步。
# 原则：只保证流程能跑通，不假装知道该赛道的真实特征。
CUSTOM_LANE_PILLARS: tuple[str, ...] = ("入门指南", "避坑经验", "清单推荐", "对比测评", "场景实测")
CUSTOM_LANE_COMMERCIAL_WEIGHT = 60.0

# 未采集到作者数据时，新赛道爆款门槛的保守下限
DEFAULT_VIRAL_FLOOR = 500


def _custom_lane(name: str) -> Lane:
    """为四大赛道之外的输入动态构造赛道定义。

    不写回 LANES，避免自由赛道污染四大赛道的对比排序。
    pillars 与 audience 都是通用兜底，输出时必须标注 is_custom，
    提醒 Agent 不要把兜底文案当成该赛道的真实画像。
    """
    return Lane(
        key=name,
        name=name,
        emoji="\U0001F9ED",
        keywords=(name,),
        pillars=CUSTOM_LANE_PILLARS,
        audience="未采集：该赛道人群画像需先用 collect lane 补样本后判断",
        monetize=("未采集：变现路径需结合用户实际资源判断",),
        barrier="通用兜底：真实门槛需结合素材可得性与更新频率判断",
        asset_logic="通用兜底：先做清单/避坑/对比三类，再按实际数据收敛",
        commercial_weight=CUSTOM_LANE_COMMERCIAL_WEIGHT,
        is_custom=True,
    )


def resolve_lane(name: str) -> Lane:
    """把用户输入（别名/简称/自由赛道名）解析为赛道。

    四大赛道与别名走预设；其余输入按自由赛道处理，不再直接报错。
    """
    raw = (name or "").strip()
    if not raw:
        raise ValueError("赛道名不能为空")
    key = LANE_ALIASES.get(raw)
    if key:
        return LANES[key]
    return _custom_lane(raw)


def get_lane(name: str) -> Lane:
    """按赛道名取定义，含自由赛道兜底。"""
    return resolve_lane(name)


# ─── 评分权重 ─────────────────────────────────────────────────────────────────

DEFAULT_TRACK_WEIGHTS: dict[str, float] = {
    "demand": 0.30,       # 需求规模
    "saturation": 0.25,   # 供给饱和度（反向计入）
    "breakout": 0.20,     # 素人突围空间
    "commercial": 0.15,   # 商业价值
    "fit": 0.10,          # 个人匹配度（由 Agent 依据用户输入打分后传入）
    # 跨平台外部热度：默认 0，即不进总分，只作为旁证输出。
    # 只有当用户显式传 --weights ...external=0.1 时才参与加权。
    "external": 0.0,
}

# 爆款判定：样本内分位数 + 绝对下限，避免小众赛道把 20 赞当爆款
VIRAL_PERCENTILE = 0.90
VIRAL_FLOOR = {"母婴育儿": 800, "美食探店": 500, "旅行出行": 500, "家居生活": 600}

# ─── 跨平台热点 ───────────────────────────────────────────────────────────────

# 热点来源等级：等级决定能下什么结论，不能混用。
HOTSPOT_SOURCE_LEVELS: dict[str, str] = {
    "L1": "平台公开数据接口直连，结构化，可直接引用",
    "L2": "xiaohongshu-skills 站内 CLI，真实采集，可直接引用",
    "L3": "公开网页/搜索摘要，非结构化，只能作为方向提示",
}

# 支持的热点平台及其中文名
HOTSPOT_PLATFORMS: dict[str, str] = {
    "douyin": "抖音",
    "xhs": "小红书",
}

# 抖音热榜公开接口：返回 word_list[{word, hot_value, label}]，无需登录与签名
DOUYIN_HOT_WORD_URL = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
DOUYIN_HOT_TIMEOUT = 15

# 热度口径：同一个平台下的数值不代表同一种含义，不能混在一起归一化
HOTSPOT_HEAT_BASIS: dict[str, str] = {
    "hot_value": "平台官方热度值（抖音热榜）",
    "engagement_median": "样本中位互动分（站内搜索结果）",
    "search_rank": "搜索联想词位次（站内需求侧，无热度值）",
}

# 外溢判定阈值：抖音热度归一化后 >= 60 且站内样本 < 20 视为“外溢未满足”
HOTSPOT_SPILL_HEAT = 60.0
HOTSPOT_SUPPLY_GAP = 20

# 互动热度权重：收藏代表"有用"，评论代表"想聊"，分享代表"想转给别人"
ENGAGE_WEIGHTS = {"liked": 1.0, "collected": 1.5, "comment": 2.0, "shared": 2.5}

# 采集节流（防风控）：每 N 次详情请求后休息
THROTTLE_EVERY = 3
THROTTLE_SLEEP_RANGE = (10.0, 20.0)


# ─── 排期节奏 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Phase:
    """30 天冷启动的阶段定义。"""

    name: str
    days: tuple[int, int]
    goal: str
    posts_per_week: int
    focus: str
    extra: dict = field(default_factory=dict)


PHASES: tuple[Phase, ...] = (
    Phase(
        name="立人设",
        days=(1, 7),
        goal="让算法和用户都知道你是谁、为谁服务",
        posts_per_week=3,
        focus="自我介绍 + 3 个内容支柱各 1 篇，统一封面风格与简介",
    ),
    Phase(
        name="测选题",
        days=(8, 14),
        goal="用低成本内容测出这个账号能拿流量的选题方向",
        posts_per_week=5,
        focus="5 个不同钩子各 1 篇，保留数据最好的 2 个方向",
    ),
    Phase(
        name="放大",
        days=(15, 21),
        goal="把已验证的选题做成系列，建立稳定预期",
        posts_per_week=4,
        focus="围绕 2 个胜出方向各做 2 篇系列内容，加互动钩子",
    ),
    Phase(
        name="沉淀",
        days=(22, 30),
        goal="拉收藏与搜索长尾，形成可复用的内容资产",
        posts_per_week=4,
        focus="清单/攻略型长尾内容 + 首月数据复盘 + 下月选题池",
    ),
)

# 默认发布时间（经验值，需按账号实际数据回流修正）
PUBLISH_SLOTS = {
    "母婴育儿": ("07:30", "12:30", "21:00"),
    "美食探店": ("11:30", "17:30", "20:30"),
    "旅行出行": ("12:00", "18:30", "21:30"),
    "家居生活": ("08:00", "12:00", "21:00"),
}
