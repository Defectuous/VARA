from fastapi import FastAPI

from vara.api import health, v1
from vara.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="V.A.R.A. API",
        version="0.1.0",
        # Interactive docs are a development convenience only.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings
    # Unversioned liveness check for scripts and service managers.
    app.add_api_route("/health", health, methods=["GET"])
    app.include_router(v1)
    return app


app = create_app()
