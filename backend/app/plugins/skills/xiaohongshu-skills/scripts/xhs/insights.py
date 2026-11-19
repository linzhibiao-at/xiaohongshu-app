"""站内搜索需求词：读出「用户真的在小红书搜什么」。

小红书 PC web 端没有全站热榜接口。首页和搜索页的
`__INITIAL_STATE__.search.searchHotSpots` 恒为空数组，全站热榜只走 App 端。
但搜索页会随页面一起下发两组需求侧信号：

    search.suggestions   搜索联想词，按用户实际搜索行为排序
    search.tagSearch     站内已归类的话题，反映内容是怎么被组织的

这两组数据不需要额外签名，也不需要调用任何 edith 接口，
是能直接引用的站内需求信号。它们回答的问题比全国热榜更具体：
「看这个赛道的人，具体在搜什么词」。
"""

from __future__ import annotations

import json
import logging
import time

from .cdp import Page
from .urls import make_search_url

logger = logging.getLogger(__name__)

# 搜索页状态由 wmr 托管，取值要兼容 _value / value 两种包装
_EXTRACT_INSIGHTS_JS = r"""
(() => {
    const read = (key) => {
        const v = window.__INITIAL_STATE__?.search?.[key];
        if (!v) return [];
        const raw = v._value !== undefined ? v._value : (v.value !== undefined ? v.value : v);
        return Array.isArray(raw) ? raw : [];
    };
    return JSON.stringify({
        suggestions: read("suggestions"),
        tag: read("tagSearch"),
    });
})()
"""

# 「综合」是筛选器占位项，不是话题
_TAG_PLACEHOLDER = "综合"

# 未登录时搜索页会被重定向到登录页，联想词自然为空
_LOGIN_CHECK_JS = r"""
(() => {
    if (location.pathname.includes("login")) return "1";
    return document.querySelector(".login-container") ? "1" : "0";
})()
"""

# 联想词由前端在页面加载后异步回填，导航完成时往往还是空的
_DEFAULT_WAIT = 4.0


def fetch_search_insights(page: Page, keyword: str, wait: float = _DEFAULT_WAIT) -> dict:
    """打开搜索页，读出该关键词的联想词与相关话题。

    Returns:
        {
            "keyword": "厦门旅拍",
            "suggestions": [{"text": ..., "type": ..., "search_type": ...}],
            "tags": [{"name": ..., "origin_text": ...}],
        }
    """
    page.navigate(make_search_url(keyword))
    page.wait_for_load()
    page.wait_dom_stable()

    # 未登录时会停在登录页，这时取到的空列表是“没权限”而不是“没需求”
    if page.evaluate(_LOGIN_CHECK_JS) == "1":
        logger.warning("未登录，搜索页停在登录页，无法采集联想词")
        return {
            "keyword": keyword,
            "suggestions": [],
            "tags": [],
            "login_required": True,
        }

    time.sleep(wait)

    raw = page.evaluate(_EXTRACT_INSIGHTS_JS)
    data: dict = {}
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("搜索页状态解析失败，按空结果处理")

    suggestions = []
    for item in data.get("suggestions") or []:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        suggestions.append(
            {
                "text": text,
                "type": item.get("type") or "",
                "search_type": item.get("searchType") or "",
            }
        )

    tags = []
    for item in data.get("tag") or []:
        name = str(item.get("name") or "").strip()
        if not name or name == _TAG_PLACEHOLDER:
            continue
        tags.append(
            {
                "name": name,
                "origin_text": str(item.get("originText") or "").strip(),
            }
        )

    logger.info("「%s」联想词 %d 条，相关话题 %d 条", keyword, len(suggestions), len(tags))
    return {
        "keyword": keyword,
        "suggestions": suggestions,
        "tags": tags,
        "login_required": False,
    }
