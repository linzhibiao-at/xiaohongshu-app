from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = APP_DIR / "plugins"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./xiaohongshu-app.db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    jwt_secret: str = "dev-secret-change-me"
    jwt_expire_minutes: int = 60 * 24 * 7

    admin_username: str = "admin"
    admin_password: str = "change-me"

    llm_provider: str = "anthropic"
    llm_model: str = "claude-fable-5-2"
    llm_api_key: str = ""
    llm_base_url: str = ""

    e2b_api_key: str = ""

    xhs_connector: str = "mock"

    # ---- 微信登录（公众号网页授权）----
    wechat_mode: str = "mock"  # mock | live
    wechat_app_id: str = ""
    wechat_app_secret: str = ""
    wechat_mock_openid: str = "mock_openid_dev"
    wechat_mock_nickname: str = "微信测试用户"
    client_base_url: str = "http://localhost:5173"

    interaction_daily_limit: int = 60
    interaction_min_interval_seconds: int = 45

    cors_origins: str = "*"


@lru_cache
def get_settings() -> Settings:
    return Settings()
