from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings, loaded strictly from environment variables / .env."""

    DATABASE_URL: str
    REDIS_URL: str = "redis://localhost:6379/0"
    MOCK_SERVICE_URL: str = "http://localhost:8001"
    JWT_SECRET: str

    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60

    DOCTORS_CACHE_TTL: int = 60
    SLOTS_CACHE_TTL: int = 30
    SLOT_LOCK_TTL: int = 600
    EXTERNAL_TIMEOUT: float = 2.0

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
