import argparse
import logging
from datetime import datetime
from pathlib import Path

import uvicorn

from vara import access, google_cli, setup_wizard
from vara.backup import backup_database, restore_database
from vara.config import get_settings
from vara.db.database import Database
from vara.logging import configure_logging


def serve() -> None:
    settings = get_settings()
    db = Database(settings.database_path)
    db.upgrade()
    with db.session() as session:
        if not access.setup_complete(session):
            logging.getLogger("vara").warning(
                "V.A.R.A. may not read any of your files yet. Run: uv run python -m vara setup"
            )
    db.dispose()
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger("vara").warning(
            "Binding to %s exposes the API beyond this computer; device authentication "
            "is not implemented yet.",
            settings.host,
        )
    uvicorn.run(
        "vara.main:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="vara", description="V.A.R.A. backend")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("serve", help="run the API (the default)")
    commands.add_parser("setup", help="choose which folders V.A.R.A. may read")
    google = commands.add_parser("google", help="connect or disconnect Google Docs (read-only)")
    google.add_argument("action", choices=["connect", "status", "disconnect"])
    google.add_argument("client_file", nargs="?", type=Path, help="OAuth client JSON (connect)")
    backup = commands.add_parser("backup", help="write a verified copy of the database")
    backup.add_argument("destination", nargs="?", type=Path)
    restore = commands.add_parser("restore", help="replace the database from a backup")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--yes", action="store_true", help="confirm overwriting the database")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging(settings.log_level)
    log = logging.getLogger("vara")

    if args.command == "setup":
        db = Database(settings.database_path)
        db.upgrade()
        setup_wizard.run(db)
    elif args.command == "google":
        db = Database(settings.database_path)
        db.upgrade()
        google_cli.run(db, args.action, args.client_file)
    elif args.command == "backup":
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        destination = args.destination or settings.data_dir / "backups" / f"vara-{stamp}.db"
        backup_database(settings.database_path, destination)
        log.info("Backup written and verified: %s", destination)
    elif args.command == "restore":
        if not args.yes:
            parser.error("restore overwrites the current database; stop the server and add --yes")
        restore_database(args.backup, settings.database_path)
        log.info("Database restored from %s", args.backup)
    else:
        serve()


if __name__ == "__main__":
    main()
