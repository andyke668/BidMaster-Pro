from __future__ import annotations

from pydantic_settings import BaseSettings
from pathlib import Path
from urllib.parse import quote_plus


class Settings(BaseSettings):
    app_name: str = "智多星标书辅助系统"
    debug: bool = True
    host: str = "0.0.0.0"
    port: int = 8000

    db_type: str = "postgresql"
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/bidmaster"
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "root"
    mysql_password: str = "root"
    mysql_database: str = "bidmaster"

    redis_url: str = "redis://localhost:6379/0"
    chroma_dir: str = "./chroma_db"
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_bucket: str = "bidmaster"
    projects_root: str = "./projects"

    llm_default_model: str = "deepseek/deepseek-chat"
    llm_api_key: str = ""
    llm_api_base: str = "https://api.deepseek.com"
    llm_fallback_modes: str = "ollama/qwen2.5"
    llm_max_retries: int = 3

    embedding_mode: str = "api"
    embedding_model: str = "text-embedding-v3"
    embedding_api_key: str = ""
    embedding_api_base: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    mineru_mode: str = "cloud"
    mineru_api_key: str = ""
    mineru_endpoint: str = "https://mineru.net/api/v4"
    mineru_timeout: int = 180
    mineru_model_version: str = "vlm"
    mineru_poll_interval: int = 5
    mineru_max_polls: int = 60

    tender_text_max_chars: int = 32000

    # ── 管理监控 / 会话（v0.4.0 新增）──
    # 登录态 TTL；与原先 auth.py 里的 SESSION_TTL 保持一致（7 天）
    session_ttl_seconds: int = 86400 * 7
    # 心跳落库节流：每个活跃会话最多每 N 秒写一次 last_seen_at
    presence_touch_seconds: int = 30
    # 在线判定窗口：last_seen_at 在此窗口内即视为「在线」
    presence_online_seconds: int = 180
    # 行为流水 / token 流水保留天数，超期由后台协程清理
    activity_retention_days: int = 90
    # 日活与报表的自然日时区（库里存 naive UTC，展示按此时区换算）
    report_timezone: str = "Asia/Shanghai"
    # 每人每日默认配额，0 = 不限；被 user_quotas 表的按人配置覆盖
    default_daily_action_quota: int = 0
    default_daily_token_quota: int = 0

    model_config = {"env_file": ".env", "env_prefix": "BMP_", "extra": "ignore"}

    def get_database_url(self) -> str:
        if self.db_type == "mysql":
            return (
                f"mysql+aiomysql://{quote_plus(self.mysql_user)}:{quote_plus(self.mysql_password)}"
                f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}"
                f"?charset=utf8mb4"
            )
        return self.database_url


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
