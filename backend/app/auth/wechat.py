"""微信登录 provider 抽象。

`mock` 不发任何网络请求，本地无凭证即可跑通完整登录闭环；`live` 走公众号网页授权
（snsapi_userinfo）的两个官方接口。回调之后的建号、发 ticket、发 JWT 与 provider 无关，
两种模式共用同一条代码路径。
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass

import httpx

from app.config import get_settings

AUTHORIZE_URL = "https://open.weixin.qq.com/connect/oauth2/authorize"
TOKEN_URL = "https://api.weixin.qq.com/sns/oauth2/access_token"
USERINFO_URL = "https://api.weixin.qq.com/sns/userinfo"


class WeChatError(Exception):
    pass


@dataclass
class WeChatUser:
    openid: str
    unionid: str = ""
    nickname: str = ""
    avatar: str = ""


class WeChatProvider(ABC):
    @abstractmethod
    async def exchange_code(self, code: str) -> WeChatUser:
        """用微信回调带回的 code 换出用户身份。"""


class MockProvider(WeChatProvider):
    async def exchange_code(self, code: str) -> WeChatUser:  # noqa: ARG002 (mock ignores the code)
        s = get_settings()
        return WeChatUser(openid=s.wechat_mock_openid, nickname=s.wechat_mock_nickname)


class OfficialProvider(WeChatProvider):
    def __init__(self, app_id: str, app_secret: str) -> None:
        self.app_id = app_id
        self.app_secret = app_secret

    async def exchange_code(self, code: str) -> WeChatUser:
        async with httpx.AsyncClient(timeout=10) as client:
            token_res = await client.get(
                TOKEN_URL,
                params={
                    "appid": self.app_id,
                    "secret": self.app_secret,
                    "code": code,
                    "grant_type": "authorization_code",
                },
            )
            token = token_res.json()
            # 微信故障码只透出 errcode，绝不把 code / secret 拼进消息，避免进日志。
            if token.get("errcode"):
                raise WeChatError(f"微信登录失败({token['errcode']})")
            openid = token.get("openid") or ""
            access_token = token.get("access_token") or ""
            if not openid or not access_token:
                raise WeChatError("微信登录失败(缺少 openid)")

            info_res = await client.get(
                USERINFO_URL,
                params={"access_token": access_token, "openid": openid, "lang": "zh_CN"},
            )
            info = info_res.json()

        if info.get("errcode"):
            # 用户资料拿不到不影响登录，退化为空昵称/头像。
            return WeChatUser(openid=openid, unionid=token.get("unionid") or "")
        return WeChatUser(
            openid=openid,
            unionid=info.get("unionid") or token.get("unionid") or "",
            nickname=(info.get("nickname") or "")[:64],
            avatar=(info.get("headimgurl") or "")[:512],
        )


def build_provider() -> WeChatProvider | None:
    """None 表示微信登录未就绪（live 但缺 app_id/app_secret）。"""
    s = get_settings()
    if s.wechat_mode == "mock":
        return MockProvider()
    if s.wechat_app_id and s.wechat_app_secret:
        return OfficialProvider(s.wechat_app_id, s.wechat_app_secret)
    return None
