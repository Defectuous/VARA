"""First-run setup in the terminal: choose what V.A.R.A. may read."""

from collections.abc import Callable
from pathlib import Path

from vara import access
from vara.db.database import Database


def _yes(answer: str) -> bool:
    return answer.strip().lower() in ("y", "yes")


def run(db: Database, ask: Callable[[str], str] = input, say: Callable[[str], None] = print):
    say("V.A.R.A. can read your files to answer questions about them, citing what it used.")
    say("Nothing is read until you allow it, and you can change this later.\n")
    folders = access.suggested_folders()
    for key, path in folders.items():
        say(f"  {_label(key):<14} {path}")
    if "google_drive" in folders:
        say("  (Google Docs and Sheets in Drive are only placeholders; they are read through the")
        say("  Google sign-in at the end instead.)")

    if _yes(ask("\nAllow all of these? [y/N] ")):
        chosen = [p for p in folders.values() if p.is_dir()]
    else:
        chosen = [
            path
            for key, path in folders.items()
            if path.is_dir() and _yes(ask(f"Allow {_label(key)}? [y/N] "))
        ]
        say("Other folders V.A.R.A. may read (full paths, one per line; blank to finish):")
        chosen += _paths(ask, say)

    exclude: list[Path] = []
    if chosen:
        say("Folders inside those to keep private (full paths; blank to finish):")
        exclude = _paths(ask, say)

    google = _yes(
        ask("Allow V.A.R.A. to read your Google Docs, once you connect your Google account? [y/N] ")
    )

    with db.session() as session:
        access.complete_setup(session, folders=chosen, exclude=exclude, google_docs=google)
        summary = access.overview(session)

    say("\nSaved. V.A.R.A. may read:")
    for grant in summary["grants"]:
        label = {"folder": "allowed", "exclude": "private", "google_docs": "Google Docs"}
        say(f"  {label[grant['kind']]:<12} {grant['path'] or '(when connected)'}")
    if not summary["grants"]:
        say("  nothing yet")
    say(f"It never reads {access.NEVER_READ}.")
    return summary


def _label(key: str) -> str:
    return "Google Drive" if key == "google_drive" else key.capitalize()


def _paths(ask: Callable[[str], str], say: Callable[[str], None]) -> list[Path]:
    paths = []
    while answer := ask("> ").strip().strip('"'):
        try:
            paths.append(access.validate_folder(answer))
        except ValueError as error:
            say(f"  {error}")
    return paths
