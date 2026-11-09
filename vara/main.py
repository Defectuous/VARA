import asyncio
import contextlib
import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from vara import retention
from vara.api import health, v1
from vara.config import Settings, get_settings
from vara.credentials import get_api_key
from vara.data_api import data
from vara.db.database import Database
from vara.deals import DealFinder
from vara.deals_api import deals as deals_routes
from vara.events import EventBus
from vara.google_docs import GoogleDocsClient
from vara.markets import MarketData
from vara.model_updates import maintain_models
from vara.price_watch import maintain_watches
from vara.providers.anthropic import AnthropicProvider
from vara.providers.base import ModelProvider
from vara.providers.ollama import OllamaProvider
from vara.router import ModelRouter
from vara.search.api import maintain, web
from vara.search.client import SearxngClient
from vara.search.service import SearxngService
from vara.tts import KokoroSynthesizer, Synthesizer
from vara.voice import Transcriber, WhisperTranscriber
from vara.voice_api import voice

WEB_DIR = Path(__file__).parent / "web"


def chat_page() -> FileResponse:
    """The built-in chat page. It only uses the public API, like any other client."""
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})


# Icons for the chat page (built from assets/icons by tools/build_icons.py).
WEB_ICONS = ("favicon.ico", "favicon.svg", "apple-touch-icon.png")


def _icon_route(name: str):
    def icon() -> FileResponse:
        return FileResponse(WEB_DIR / name, headers={"Cache-Control": "public, max-age=86400"})

    return icon


def build_router(settings: Settings) -> ModelRouter:
    providers: list[ModelProvider] = [
        OllamaProvider(
            base_url=settings.ollama_url,
            default_model=settings.ollama_model,
            models=settings.ollama_model_list,
            keep_alive=settings.ollama_keep_alive,
        )
    ]
    # Cloud providers are registered only when their key is in the credential store.
    if key := get_api_key("anthropic"):
        providers.append(
            AnthropicProvider(
                key, default_model=settings.anthropic_model, effort=settings.anthropic_effort
            )
        )
    return ModelRouter(providers, settings.privacy_mode)


async def _warm_up_voice(app: FastAPI, settings: Settings) -> None:
    """Load speech-to-text and text-to-speech in the background; a failure here only means the first
    spoken request loads them instead."""
    log = logging.getLogger("vara.voice")
    jobs = []
    if warm := getattr(app.state.transcriber, "warm_up", None):
        jobs.append(("speech-to-text", warm))
    if warm := getattr(app.state.synthesizer, "warm_up", None):
        jobs.append(("text-to-speech", lambda: app.state.synthesizer.warm_up(settings.tts_voice)))
    for name, job in jobs:
        try:
            await asyncio.to_thread(job)
        except Exception as exc:  # noqa: BLE001 - never stop the server over a warm-up
            log.warning("Couldn't warm up %s: %s", name, exc)


def create_app(
    settings: Settings | None = None,
    router: ModelRouter | None = None,
    google: GoogleDocsClient | None = None,
    transcriber: Transcriber | None = None,
    synthesizer: Synthesizer | None = None,
    web_search=None,
    markets=None,
    deals=None,
) -> FastAPI:
    settings = settings or get_settings()
    search_service = SearxngService(
        settings.searxng_dir, settings.searxng_port, log_dir=settings.data_dir / "logs"
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        # SearXNG runs alongside the server and is updated daily; it stops with the server.
        # Ollama models are pulled at startup and daily the same way.
        stop = asyncio.Event()
        tasks = []
        if settings.web_search != "off" and web_search is None:
            tasks.append(asyncio.create_task(maintain(search_service, stop)))
        if settings.model_updates != "off":
            status_path = settings.data_dir / "modules" / "ollama-updates.json"
            tasks.append(
                asyncio.create_task(maintain_models(settings.ollama_url, status_path, stop))
            )
        # Price watches are re-checked every 6 hours (only with real web search, not in tests).
        if settings.web_search != "off" and web_search is None and app.state.deals is not None:
            tasks.append(
                asyncio.create_task(
                    maintain_watches(app.state.db, app.state.deals, app.state.events, stop)
                )
            )
        if settings.voice_warm_up:
            tasks.append(asyncio.create_task(_warm_up_voice(app, settings)))
        yield
        stop.set()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        search_service.stop()

    app = FastAPI(
        title="V.A.R.A. API",
        version="0.1.0",
        lifespan=lifespan,
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
    app.state.google = google or GoogleDocsClient()
    app.state.transcriber = transcriber or WhisperTranscriber(
        settings.whisper_model, settings.whisper_device, settings.whisper_compute_type
    )
    app.state.synthesizer = synthesizer or KokoroSynthesizer(
        settings.kokoro_model_path, settings.kokoro_voices_path
    )
    app.state.search_service = search_service
    app.state.events = EventBus()
    if settings.web_search == "off":
        app.state.web = None
    else:
        app.state.web = web_search or SearxngClient(search_service.url)
    # Market quotes and price checks follow the web-search setting; tests pass their own (or none).
    live_web = settings.web_search != "off" and web_search is None
    if markets is not None:
        app.state.markets = markets
    elif live_web:
        app.state.markets = MarketData()
    else:
        app.state.markets = None
    if deals is not None:
        app.state.deals = deals
    elif live_web:
        app.state.deals = DealFinder(app.state.web, bestbuy_key=get_api_key("bestbuy"))
    else:
        app.state.deals = None
    # Unversioned liveness check for scripts and service managers.
    app.add_api_route("/health", health, methods=["GET"])
    app.add_api_route("/", chat_page, methods=["GET"], include_in_schema=False)
    for name in WEB_ICONS:
        app.add_api_route(f"/{name}", _icon_route(name), methods=["GET"], include_in_schema=False)
    app.include_router(v1)
    app.include_router(data)
    app.include_router(voice)
    app.include_router(web)
    app.include_router(deals_routes)
    return app
