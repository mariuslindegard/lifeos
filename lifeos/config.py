from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    lifeos_env: str = "development"
    lifeos_password: str = "changeme"
    lifeos_secret_key: str = "dev-secret-change-me"
    lifeos_cookie_secure: bool = False
    database_url: str = "sqlite:///./data/lifeos.db"
    public_base_url: str = "http://localhost:8000"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "gemma4:4b"
    ollama_embed_model: str = "nomic-embed-text"

    scheduler_enabled: bool = True
    default_timezone: str = "Europe/Oslo"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
