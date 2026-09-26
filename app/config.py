from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    supabase_jwt_secret: str
    meta_muse_api_key: str = ""
    meta_muse_base_url: str = "https://api.meta.ai/v1"
    meta_muse_model: str = "muse-spark-1.3"
    allowed_origins: str = "http://localhost:8081"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
