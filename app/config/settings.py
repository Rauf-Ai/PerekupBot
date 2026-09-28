from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")
    database_url: str = "postgresql+psycopg://perekup:perekup_local@localhost:5432/perekup"
    redis_url: str = "redis://localhost:6379/0"
    telegram_bot_token: str = ""
    telegram_admin_id: int | None = None
    telegram_admin_ids: str = ""
    telegram_api_id: int | None = None
    telegram_api_hash: str = ""
    telegram_session_path: str = "data/telegram"
    apify_token: str = ""
    apify_tokens: str = ""
    vk_token: str = ""
    sources_config: str = "app/config/sources.json"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
