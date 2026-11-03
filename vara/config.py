from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from vara.router import PrivacyMode


class Environment(StrEnum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Runtime settings, read from `VARA_*` environment variables or a local `.env` file.

    API keys do not belong here; they live in the OS credential store.
    """

    model_config = SettingsConfigDict(env_prefix="VARA_", env_file=".env", extra="ignore")

    environment: Environment = Environment.DEVELOPMENT
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    data_dir: Path = Path("data")

    privacy_mode: PrivacyMode = PrivacyMode.LOCAL_ONLY
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.1:8b"
    anthropic_model: str = "claude-opus-5-5"
    anthropic_effort: str = "medium"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "vara.db"

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION


@lru_cache
def get_settings() -> Settings:
    return Settings()
