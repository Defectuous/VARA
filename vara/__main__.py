import argparse
import getpass
import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

import uvicorn

from vara import access, evals, google_cli, setup_wizard
from vara.backup import backup_database, restore_database
from vara.config import get_settings
from vara.credentials import delete_secret, set_secret
from vara.db.database import Database
from vara.logging import configure_logging
from vara.search.service import SearxngService


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


def run_evals(settings, args) -> int:
    """Run the eval cases for each model; 1 if any model scored lower than its previous run."""
    logging.getLogger("httpx").setLevel(
        logging.WARNING
    )  # one line per request would bury the results
    cases = evals.load_cases()
    if args.category:
        cases = [c for c in cases if c.category in args.category]
    if args.case:
        cases = [c for c in cases if c.id in args.case]
    if args.offline:
        cases = [c for c in cases if not c.request.get("web_search")]
    if args.list:
        for c in cases:
            print(f"{c.category:<10} {c.id}")
        return 0
    if not cases:
        print("No eval cases match.")
        return 2

    runner = evals.EvalRunner(f"http://127.0.0.1:{settings.port}", settings.evals_dir)
    try:
        runner.check_backend()
    except RuntimeError as exc:
        print(exc)
        return 2
    worse = False
    for model in args.model or settings.eval_model_list:
        print(f"\n{model}: {len(cases)} cases")
        results = []
        for case in cases:
            result = runner.run_case(case, model, keep=args.keep)
            results.append(result)
            mark = "pass" if result.passed else "FAIL"
            why = f"  ({'; '.join(result.failures)})" if result.failures else ""
            print(f"  {mark}  {case.id:<28} {result.seconds:>6.1f}s{why}", flush=True)
        previous = runner.previous(model, {c.id for c in cases})
        summary = runner.save(model, runner.model_digest(settings.ollama_url, model), results)
        change = evals.compare(previous, summary)
        print(f"  Score: {summary['passed']}/{summary['total']} ({summary['score']}%)")
        print("  By category: " + ", ".join(f"{k} {v}" for k, v in summary["by_category"].items()))
        if change["delta"] is not None:
            note = " (the model itself changed)" if change["model_changed"] else ""
            print(f"  Since last run: {change['delta']:+} points{note}")
            if change["regressed"]:
                print("  Newly failing: " + ", ".join(change["regressed"]))
            if change["fixed"]:
                print("  Newly passing: " + ", ".join(change["fixed"]))
            worse = worse or change["delta"] < 0
        blocked = sorted({e for r in results for e in r.unresponsive_engines})
        if blocked:
            print("  Search engines that didn't answer: " + ", ".join(blocked))
    print(f"\nFull results: {settings.evals_dir / 'runs'}")
    return 1 if worse else 0


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
    search = commands.add_parser("search", help="install, update, or check the web search module")
    search.add_argument("action", choices=["install", "update", "status"])
    deals = commands.add_parser("deals", help="store or remove the optional Best Buy API key")
    deals.add_argument("action", choices=["bestbuy-key", "remove-bestbuy-key"])
    evaluate = commands.add_parser(
        "eval", help="grade the models' answers on a fixed question set (needs V.A.R.A. running)"
    )
    evaluate.add_argument(
        "--model",
        action="append",
        help="Ollama model to test (repeatable; default VARA_EVAL_MODELS)",
    )
    evaluate.add_argument("--category", action="append", help="only these categories (repeatable)")
    evaluate.add_argument("--case", action="append", help="only these case ids (repeatable)")
    evaluate.add_argument(
        "--offline", action="store_true", help="skip cases that need live web search"
    )
    evaluate.add_argument(
        "--keep", action="store_true", help="keep the test conversations in the chat history"
    )
    evaluate.add_argument("--list", action="store_true", help="list the cases and exit")
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
    elif args.command == "search":
        service = SearxngService(
            settings.searxng_dir, settings.searxng_port, log_dir=settings.data_dir / "logs"
        )
        if args.action == "install":
            service.install()
            log.info(
                "SearXNG installed in %s (commit %s)", service.dir, service.current_commit()[:9]
            )
        elif args.action == "update":
            started = not service.healthy()
            if started:
                service.start()  # the update's test search needs it running
            status = service.check_for_update()
            if started:
                service.stop()
            log.info(
                "SearXNG update check: %s (commit %s)", status.result, (status.commit or "")[:9]
            )
        else:
            print(
                json.dumps(
                    {
                        "installed": service.installed,
                        "running": service.healthy(),
                        **asdict(service.status()),
                    },
                    indent=2,
                )
            )
    elif args.command == "deals":
        if args.action == "bestbuy-key":
            # Typed, not passed as an argument, so the key doesn't end up in shell history.
            key = getpass.getpass(
                "Best Buy API key (from developer.bestbuy.com; input is hidden): "
            )
            if not key.strip():
                parser.error("no key entered")
            set_secret("bestbuy", key.strip())
            log.info("Best Buy key saved in the credential store; restart V.A.R.A. to use it")
        else:
            delete_secret("bestbuy")
            log.info("Best Buy key removed")
    elif args.command == "eval":
        raise SystemExit(run_evals(settings, args))
    else:
        serve()


if __name__ == "__main__":
    main()
