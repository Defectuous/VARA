"""Keeps the Ollama models current: pulls every installed model when V.A.R.A. starts, then once a day.

A pull of a model that hasn't changed only compares manifests, so it takes a second or two; a changed model is
downloaded in the background while the old copy keeps answering. Results are written to
`<data_dir>/modules/ollama-updates.json` and the log.
"""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("vara.models")

UPDATE_INTERVAL = timedelta(days=1)


async def _digests(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/tags")
    response.raise_for_status()
    return {m["name"]: m.get("digest", "") for m in response.json().get("models", [])}


async def update_models(client: httpx.AsyncClient) -> dict[str, str]:
    """Pull every model Ollama has. Returns {model: "up to date" | "updated" | "failed: ..."}."""
    before = await _digests(client)
    results: dict[str, str] = {}
    for name in before:
        try:
            response = await client.post("/api/pull", json={"model": name, "stream": False})
            response.raise_for_status()
            status = response.json().get("status", "")
            if status != "success":
                results[name] = f"failed: {status or 'no status'}"
        except (httpx.HTTPError, ValueError) as exc:
            results[name] = f"failed: {exc}"
    after = await _digests(client)
    for name, digest in before.items():
        if name not in results:
            results[name] = "updated" if after.get(name, digest) != digest else "up to date"
    return results


def _save(path: Path, results: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"checked_at": datetime.now(UTC).isoformat(timespec="seconds"), "models": results}
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")


async def maintain_models(
    base_url: str,
    status_path: Path,
    stop: asyncio.Event,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Background task: update the models now, then every day until `stop` is set."""
    # No overall timeout: downloading a changed model can take many minutes.
    client = client or httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(None, connect=10))
    while not stop.is_set():
        try:
            results = await update_models(client)
            _save(status_path, results)
            log.info("Ollama model update check: %s", results)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Ollama model update check failed: %s", exc)
        try:
            await asyncio.wait_for(stop.wait(), timeout=UPDATE_INTERVAL.total_seconds())
        except TimeoutError:
            pass
