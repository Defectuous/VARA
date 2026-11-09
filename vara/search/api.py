import asyncio
import logging
from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from vara.search.client import SearchFailed
from vara.search.service import SearchModuleError, SearxngService

web = APIRouter(prefix="/v1/web")
log = logging.getLogger("vara.search")


def _service(request: Request) -> SearxngService:
    return request.app.state.search_service


@web.get("/search")
async def search(q: str, request: Request, category: str = "general") -> dict:
    """Search the web through V.A.R.A.'s SearXNG (results merged from the major search engines)."""
    client = request.app.state.web
    if client is None:
        raise HTTPException(503, "Web search is off (VARA_WEB_SEARCH=off)")
    try:
        return (await client.search(q, category=category)).to_dict()
    except SearchFailed as exc:
        raise HTTPException(503, str(exc)) from exc


@web.get("/status")
async def status(request: Request) -> dict:
    """Whether SearXNG is installed and running, its version, and the last daily update check."""
    service = _service(request)
    running = await run_in_threadpool(service.healthy)
    commit = await run_in_threadpool(_commit_or_none, service)
    return {
        "enabled": request.app.state.web is not None,
        "installed": service.installed,
        "running": running,
        "url": service.url,
        "commit": commit,
        "update": asdict(service.status()),
    }


@web.post("/update")
async def update(request: Request) -> dict:
    """Check GitHub for a newer SearXNG now (this also happens once a day), update, and test it."""
    service = _service(request)
    if not service.installed:
        raise HTTPException(
            409, "SearXNG isn't installed; run: uv run python -m vara search install"
        )
    return asdict(await run_in_threadpool(service.check_for_update))


def _commit_or_none(service: SearxngService) -> str | None:
    try:
        return service.current_commit()[:9] if service.installed else None
    except SearchModuleError:
        return None


async def maintain(service: SearxngService, stop: asyncio.Event) -> None:
    """Background task: start SearXNG, then check for updates once a day while V.A.R.A. runs."""
    if not service.installed:
        log.warning(
            "Web search: SearXNG isn't installed; run: uv run python -m vara search install"
        )
        return
    try:
        await run_in_threadpool(service.start)
    except SearchModuleError as exc:
        log.error("Web search: %s", exc)
    while not stop.is_set():
        if service.update_due():
            status = await run_in_threadpool(service.check_for_update)
            log.info("SearXNG update check: %s (%s)", status.result, (status.commit or "")[:9])
        elif not await run_in_threadpool(service.healthy):
            log.warning("SearXNG stopped responding; restarting it")
            try:
                await run_in_threadpool(service.restart)
            except SearchModuleError as exc:
                log.error("Web search: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=600)  # look again in 10 minutes
        except TimeoutError:
            pass
