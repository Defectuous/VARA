"""`python -m vara google connect|status|disconnect`."""

import asyncio
from collections.abc import Callable
from pathlib import Path

from vara import access, google_docs
from vara.db.database import Database
from vara.db.models import AccessKind


def run(
    db: Database,
    action: str,
    client_file: Path | None = None,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
) -> None:
    if action == "status":
        with db.session() as session:
            allowed = access.google_docs_allowed(session)
        say(f"Allowed at setup: {'yes' if allowed else 'no'}")
        say(f"Account connected: {'yes' if google_docs.is_connected() else 'no'}")
        if allowed and google_docs.is_connected():
            say(f"Google Docs visible: {asyncio.run(_count())}")
    elif action == "disconnect":
        removed = google_docs.disconnect()
        say("Disconnected and revoked at Google." if removed else "Google was not connected.")
    else:
        connect(db, client_file, ask, say)


def connect(db, client_file, ask, say) -> None:
    with db.session() as session:
        if not access.google_docs_allowed(session):
            answer = ask("V.A.R.A. may not read your Google Docs yet. Allow it now? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                say("Nothing changed.")
                return
            access.add(session, AccessKind.GOOGLE_DOCS)

    if client_file:
        client = google_docs.load_client_file(client_file)
    elif not (client := google_docs.stored_client()):
        say("Give the OAuth client file you downloaded from Google Cloud Console:")
        say("  uv run python -m vara google connect C:\\path\\to\\client_secret.json")
        return

    say("Opening your browser to sign in to Google (read-only access to Drive)...")
    google_docs.sign_in(client)
    say(f"Connected. Google Docs visible: {asyncio.run(_count())}")
    if client_file:
        say(f"The client is saved in Windows Credential Manager; you can delete {client_file}.")


async def _count() -> int:
    client = google_docs.GoogleDocsClient()
    try:
        return len(await client.list_docs())
    finally:
        await client.aclose()
