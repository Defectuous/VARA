"""Runs and maintains V.A.R.A.'s SearXNG module.

Layout under `<data_dir>/modules/searxng`:
    src/          a git checkout of https://github.com/searxng/searxng
    .venv/        SearXNG's own Python environment (kept apart from V.A.R.A.'s dependencies)
    secret_key    this install's SearXNG secret
    status.json   the last update check and its result

SearXNG listens on 127.0.0.1 only. It is designed for Linux (V.A.R.A.'s eventual host); on Windows two small
workarounds are applied: the checkout skips a folder whose file names Windows can't store, and a stand-in for
the Unix-only `pwd` module is added to SearXNG's environment.
"""

import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

log = logging.getLogger("vara.search")

REPO_URL = "https://github.com/searxng/searxng"
BRANCH = "master"
SETTINGS = Path(__file__).with_name("searxng_settings.yml")
IS_WINDOWS = sys.platform == "win32"
UPDATE_INTERVAL = timedelta(days=1)

_PWD_SHIM = '''"""Windows stand-in for the Unix-only `pwd` module, written by V.A.R.A.

SearXNG imports `pwd` only to name the current user in an error message about its optional Valkey database,
which V.A.R.A. doesn't use. Not needed (and not created) on Linux.
"""
import getpass
from types import SimpleNamespace


def getpwuid(_uid):
    return SimpleNamespace(pw_name=getpass.getuser())
'''


class SearchModuleError(RuntimeError):
    pass


@dataclass
class UpdateStatus:
    checked_at: str | None = None
    commit: str | None = None
    result: str = "never checked"  # "up to date", "updated", "rolled back: ...", "failed: ..."
    updated_at: str | None = None
    history: list[dict] = field(default_factory=list)


def _run(args: list[str], cwd: Path | None = None, timeout: float = 600) -> str:
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()[-1:] or ["no output"]
        raise SearchModuleError(f"{' '.join(args[:3])} failed: {detail[0]}")
    return result.stdout.strip()


class SearxngService:
    def __init__(self, module_dir: Path, port: int = 8890, log_dir: Path | None = None) -> None:
        self.dir = module_dir
        self.src = module_dir / "src"
        self.venv = module_dir / ".venv"
        self.port = port
        self.log_dir = log_dir or module_dir
        self._process: subprocess.Popen | None = None

    # Installation ---------------------------------------------------------------------------

    @property
    def python(self) -> Path:
        return self.venv / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

    @property
    def installed(self) -> bool:
        return (self.src / "searx" / "webapp.py").exists() and self.python.exists()

    def install(self) -> None:
        """Clone SearXNG and create its environment. Needs git and uv (both used by V.A.R.A.)."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if not (self.src / ".git").exists():
            log.info("Cloning SearXNG from %s", REPO_URL)
            _run(["git", "clone", "--no-checkout", "--filter=blob:none", REPO_URL, str(self.src)])
            if IS_WINDOWS:
                # utils/ holds Linux server templates with ':' in their names, which NTFS can't store.
                _run(["git", "config", "core.protectNTFS", "false"], self.src)
                _run(["git", "sparse-checkout", "set", "--no-cone", "/*", "!/utils/"], self.src)
            _run(["git", "checkout", "-q", BRANCH], self.src)
        self._hide_unstorable_paths()
        self._install_requirements()

    def _hide_unstorable_paths(self) -> None:
        """Windows: mark files that couldn't be written (see install) as intentionally absent, so they never
        show as deleted or block an update."""
        if not IS_WINDOWS:
            return
        for line in _run(["git", "status", "--porcelain"], self.src).splitlines():
            if line.startswith(" D "):
                _run(
                    ["git", "update-index", "--skip-worktree", "--", line[3:].strip('"')], self.src
                )

    def _install_requirements(self) -> None:
        uv = shutil.which("uv")
        if uv is None:
            raise SearchModuleError("uv is needed to set up SearXNG's environment")
        if not self.python.exists():
            _run([uv, "venv", "-q", "--python", "3.12", str(self.venv)])
        _run(
            [
                uv,
                "pip",
                "install",
                "-q",
                "--python",
                str(self.python),
                "-r",
                str(self.src / "requirements.txt"),
            ]
        )
        if IS_WINDOWS:
            site = _run(
                [
                    str(self.python),
                    "-c",
                    "import sysconfig; print(sysconfig.get_paths()['purelib'])",
                ]
            )
            Path(site, "pwd.py").write_text(_PWD_SHIM, encoding="utf-8")

    # Running --------------------------------------------------------------------------------

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _secret(self) -> str:
        path = self.dir / "secret_key"
        if not path.exists():
            path.write_text(secrets.token_hex(32), encoding="utf-8")
        return path.read_text(encoding="utf-8").strip()

    def healthy(self, timeout: float = 2.0) -> bool:
        try:
            return httpx.get(f"{self.url}/healthz", timeout=timeout).status_code == 200
        except httpx.HTTPError:
            return False

    def start(self, wait: float = 30.0) -> None:
        if self.healthy():
            return
        if not self.installed:
            raise SearchModuleError(
                "SearXNG isn't installed; run: uv run python -m vara search install"
            )
        env = {
            **os.environ,
            "SEARXNG_SETTINGS_PATH": str(SETTINGS),
            "SEARXNG_SECRET": self._secret(),
            "SEARXNG_PORT": str(self.port),
            "SEARXNG_BIND_ADDRESS": "127.0.0.1",
        }
        self.log_dir.mkdir(parents=True, exist_ok=True)
        output = open(self.log_dir / "searxng.log", "ab")  # noqa: SIM115 - kept open for the process
        flags = subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        self._process = subprocess.Popen(
            [str(self.python), "-m", "searx.webapp"],
            cwd=self.src,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=flags,
        )
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise SearchModuleError(
                    f"SearXNG exited at startup; see {self.log_dir / 'searxng.log'}"
                )
            if self.healthy(timeout=1):
                log.info("SearXNG running at %s (pid %s)", self.url, self._process.pid)
                return
            time.sleep(0.3)
        raise SearchModuleError("SearXNG didn't start within 30 seconds")

    def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
        self._process = None

    def restart(self) -> None:
        self.stop()
        self.start()

    # Daily update check ---------------------------------------------------------------------

    @property
    def status_path(self) -> Path:
        return self.dir / "status.json"

    def status(self) -> UpdateStatus:
        try:
            return UpdateStatus(**json.loads(self.status_path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return UpdateStatus()

    def _save(self, status: UpdateStatus) -> None:
        status.history = status.history[-30:]
        self.status_path.write_text(json.dumps(asdict(status), indent=2), encoding="utf-8")

    def update_due(self, now: datetime | None = None) -> bool:
        checked = self.status().checked_at
        if checked is None:
            return True
        now = now or datetime.now(UTC)
        return now - datetime.fromisoformat(checked) >= UPDATE_INTERVAL

    def current_commit(self) -> str:
        return _run(["git", "rev-parse", "HEAD"], self.src)

    def check_for_update(self, smoke_test=None) -> UpdateStatus:
        """Fetch SearXNG's latest commits; if there are new ones, update, restart, and run a test search.
        If anything fails, go back to the previous commit. `smoke_test()` returns True when search works."""
        smoke_test = smoke_test or self.smoke_test
        status = self.status()
        now = datetime.now(UTC).isoformat(timespec="seconds")
        before = self.current_commit()
        try:
            _run(["git", "fetch", "-q", "origin", BRANCH], self.src)
            latest = _run(["git", "rev-parse", f"origin/{BRANCH}"], self.src)
        except (SearchModuleError, subprocess.TimeoutExpired) as exc:
            status.checked_at, status.result = now, f"failed: {exc}"
            self._save(status)
            log.warning("SearXNG update check failed: %s", exc)
            return status

        if latest == before:
            status.checked_at, status.commit, status.result = now, before, "up to date"
            self._save(status)
            return status

        requirements_changed = bool(
            _run(["git", "diff", "--name-only", before, latest, "--", "requirements.txt"], self.src)
        )
        log.info("Updating SearXNG %s -> %s", before[:9], latest[:9])
        try:
            _run(["git", "merge", "-q", "--ff-only", latest], self.src)
            self._hide_unstorable_paths()
            if requirements_changed:
                self._install_requirements()
            if self._process is not None or self.healthy():
                self.restart()
            if not smoke_test():
                raise SearchModuleError("test search returned no results")
            result = "updated"
            status.updated_at = now
        except (SearchModuleError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("SearXNG update failed (%s); rolling back to %s", exc, before[:9])
            _run(["git", "reset", "-q", "--hard", before], self.src)
            if requirements_changed:
                self._install_requirements()
            if self._process is not None or self.healthy():
                self.restart()
            result = f"rolled back: {exc}"
        status.checked_at, status.commit, status.result = now, self.current_commit(), result
        status.history.append({"at": now, "from": before[:9], "to": latest[:9], "result": result})
        self._save(status)
        return status

    def smoke_test(self) -> bool:
        try:
            response = httpx.get(
                f"{self.url}/search", params={"q": "wikipedia", "format": "json"}, timeout=20
            )
            return response.status_code == 200 and len(response.json().get("results", [])) > 0
        except (httpx.HTTPError, ValueError):
            return False
