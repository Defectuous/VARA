import logging

from fastapi import FastAPI

from vara import retention
from vara.api import health, v1
from vara.config import Settings, get_settings
from vara.credentials import get_api_key
from vara.data_api import data
from vara.db.database import Database
from vara.providers.anthropic import AnthropicProvider
from vara.providers.base import ModelProvider
from vara.providers.ollama import OllamaProvider
from vara.router import ModelRouter


def build_router(settings: Settings) -> ModelRouter:
    providers: list[ModelProvider] = [
        OllamaProvider(base_url=settings.ollama_url, default_model=settings.ollama_model)
    ]
    # Cloud providers are registered only when their key is in the credential store.
    if key := get_api_key("anthropic"):
        providers.append(
            AnthropicProvider(
                key, default_model=settings.anthropic_model, effort=settings.anthropic_effort
            )
        )
    return ModelRouter(providers, settings.privacy_mode)


def create_app(settings: Settings | None = None, router: ModelRouter | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="V.A.R.A. API",
        version="0.1.0",
        # Interactive docs are a development convenience only.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    db = Database(settings.database_path)
    db.upgrade()
    if settings.retention_on_startup:
        with db.session() as session:
            deleted = retention.apply(session)
        if any(deleted.values()):
            logging.getLogger("vara").info("Retention rules deleted %s", deleted)
    app.state.settings = settings
    app.state.db = db
    app.state.router = router or build_router(settings)
    # Unversioned liveness check for scripts and service managers.
    app.add_api_route("/health", health, methods=["GET"])
    app.include_router(v1)
    app.include_router(data)
    return app
