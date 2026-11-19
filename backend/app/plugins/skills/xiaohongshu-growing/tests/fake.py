"""构造与 xiaohongshu-skills CLI 输出同构的假数据，用于离线验证链路。"""

from __future__ import annotations

import time

# 母婴赛道样例：覆盖不同支柱、不同量级、不同形式
SAMPLES = [
    ("宝宝辅食第一次吃蛋黄，我踩了3个坑", "辅食", "normal", "3200", "1800", "210", "45"),
    ("6-12月龄辅食清单｜12个真能吃进去的搭配", "辅食", "normal", "1.2万", "8600", "430", "210"),
    ("宝宝辅食别乱加，这5样顺序错了会积食", "辅食", "normal", "5600", "3100", "520", "88"),
    ("10个月宝宝一日三餐，照着做就行", "辅食", "video", "880", "410", "62", "12"),
    ("母婴好物避坑｜这6件我买了就后悔", "好物", "normal", "2.3万", "1.4万", "960", "320"),
    ("婴儿车到底怎么选，我对比了5款", "好物", "normal", "7500", "4200", "380", "66"),
    ("300以内搞定宝宝秋冬穿搭", "好物", "normal", "1500", "980", "95", "24"),
    ("儿童房改造前后对比，8平米也能塞下书桌", "儿童房", "normal", "4.1万", "2.6万", "1200", "880"),
    ("租房也能做的儿童房布置，花了不到800", "儿童房", "normal", "6800", "5100", "240", "150"),
    ("绘本推荐｜2岁前反复读的10本", "绘本", "normal", "9200", "7800", "310", "140"),
    ("绘本共读怎么讲，孩子才愿意听", "绘本", "video", "2100", "1300", "180", "36"),
    ("产后修复别乱练，这4个动作先做", "产后", "video", "1.6万", "1.1万", "740", "260"),
    ("职场妈妈的下班后2小时怎么安排", "日常", "normal", "4300", "2600", "610", "90"),
    ("亲子游｜带1岁宝宝去三亚的真实开销", "亲子游", "normal", "3.2万", "2.2万", "880", "430"),
    ("早教班到底要不要报，我退了3家", "早教", "normal", "1.1万", "6200", "980", "180"),
    ("宝宝感冒护理，医生说这么做", "发育", "normal", "760", "420", "88", "20"),
    ("断奶全过程记录，第4天最难", "辅食", "normal", "2600", "1400", "320", "40"),
    ("宝宝睡眠训练｜一周调回整觉", "发育", "normal", "1.9万", "1.3万", "720", "310"),
    ("婴儿洗护好物｜敏感肌只留这4样", "好物", "normal", "5800", "4400", "260", "110"),
    ("幼儿园入园准备清单，18项一次说清", "日常", "normal", "2.7万", "2.1万", "540", "390"),
]

AUTHORS = [
    ("u_big_01", "大号育儿博主", "48万"),
    ("u_mid_01", "腰部妈妈", "6.2万"),
    ("u_small_01", "素人妈妈A", "6800"),
    ("u_small_02", "素人妈妈B", "2300"),
    ("u_small_03", "素人妈妈C", "1200"),
]


def _author_of(index: int) -> tuple[str, str]:
    # 让头部账号占 30% 左右，模拟真实集中度
    if index % 10 in (0, 1, 2):
        return AUTHORS[0][0], AUTHORS[0][1]
    if index % 10 in (3, 4):
        return AUTHORS[1][0], AUTHORS[1][1]
    return AUTHORS[2 + (index % 3)][0], AUTHORS[2 + (index % 3)][1]


def make_feed(index: int, title: str, keyword: str, note_type: str, liked: str,
              collected: str, comment: str, shared: str) -> dict:
    """keyword 只用于在样例里标注主题，CLI 输出本身不含该字段。"""
    user_id, nickname = _author_of(index)
    return {
        "id": f"feed{index:03d}",
        "xsecToken": f"token{index:03d}",
        "modelType": "note",
        "index": index,
        "displayTitle": title,
        "type": note_type,
        "user": {"userId": user_id, "nickname": nickname},
        "interactInfo": {
            "likedCount": liked,
            "collectedCount": collected,
            "commentCount": comment,
            "sharedCount": shared,
        },
        "cover": f"https://example.com/cover{index}.jpg",
    }


def all_feeds() -> list[dict]:
    return [make_feed(i, *sample) for i, sample in enumerate(SAMPLES)]


def feed_detail(feed_id: str, tags: list[str] | None = None) -> dict:
    index = int(feed_id.replace("feed", ""))
    title, keyword, note_type, liked, collected, comment, shared = SAMPLES[index]
    user_id, nickname = _author_of(index)
    return {
        "note": {
            "noteId": feed_id,
            "xsecToken": f"token{index:03d}",
            "title": title,
            "desc": f"{title} 的正文",
            "body": (
                f"{title}。\n\n先说结论：一共 {3 + index % 5} 个要点，"
                "每一个都写清楚适用条件和价格。\n\n最后是我的判断标准，"
                "你可以对照自己的情况。评论区说说你遇到的是哪一种？"
            ),
            "tags": tags or [keyword, "母婴", "育儿经验"],
            "type": note_type,
            "time": int((time.time() - (index + 1) * 86400 * 2) * 1000),
            "ipLocation": "上海",
            "user": {"userId": user_id, "nickname": nickname},
            "interactInfo": {
                "liked": False,
                "likedCount": liked,
                "collectedCount": collected,
                "collected": False,
                "commentCount": comment,
                "sharedCount": shared,
            },
            "imageList": [
                {"width": 1080, "height": 1440, "urlDefault": "https://example.com/a.jpg"}
            ],
        },
        "comments": [
            {
                "id": f"c{index}",
                "content": "请问宝宝多大可以吃？",
                "likeCount": "12",
                "createTime": 0,
                "ipLocation": "上海",
                "userInfo": {"userId": "u_x", "nickname": "路人"},
                "subCommentCount": "0",
            }
        ],
    }


def user_profile(user_id: str) -> dict:
    name = next((n for i, n, _ in AUTHORS if i == user_id), "未知作者")
    fans = next((f for i, _, f in AUTHORS if i == user_id), "1200")
    feeds = [
        make_feed(i, *sample)
        for i, sample in enumerate(SAMPLES)
        if _author_of(i)[0] == user_id
    ]
    return {
        "basicInfo": {
            "nickname": name,
            "redId": "123456789",
            "desc": "记录育儿日常",
            "gender": 1,
            "ipLocation": "上海",
        },
        "interactions": [
            {"type": "fans", "name": "粉丝", "count": fans},
            {"type": "follows", "name": "关注", "count": "128"},
            {"type": "like", "name": "获赞与收藏", "count": "3.2万"},
        ],
        "feeds": feeds,
    }


def self_profile() -> dict:
    feeds = [
        {
            "id": "selffeed01",
            "xsecToken": "selftoken01",
            "displayTitle": "新手妈妈的第一个记录",
            "type": "normal",
            "user": {"userId": "u_self", "nickname": "我的账号"},
            "interactInfo": {
                "likedCount": "23",
                "collectedCount": "5",
                "commentCount": "1",
                "sharedCount": "0",
            },
        }
    ]
    return {
        "basicInfo": {
            "nickname": "我的账号",
            "redId": "999888777",
            "desc": "一起养娃",
            "gender": 1,
            "ipLocation": "上海",
        },
        "interactions": [
            {"type": "fans", "name": "粉丝", "count": "12"},
            {"type": "follows", "name": "关注", "count": "60"},
            {"type": "like", "name": "获赞与收藏", "count": "30"},
        ],
        "feeds": feeds,
    }
