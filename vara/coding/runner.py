"""Runs a command for the coding agent: in its project folder, without the server's secrets in its
environment, with no keyboard input, a time limit, and its output capped (the start and the end are
kept, since a traceback's last lines matter most). On a timeout the whole process tree is stopped.

This is not a sandbox: the program can do anything this user account can, which is why every run is
approved first (Risk.RUN_CODE).
"""

import asyncio
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HEAD_CHARS = 3000
TAIL_CHARS = 9000
# Environment variables that look like credentials are left out of what a program sees.
_SECRET_NAME = re.compile(
    r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|PASSPHRASE|AUTH", re.IGNORECASE
)
# The server's own environment, which would point uv at V.A.R.A.'s virtualenv instead of the project's.
_DROPPED = {"VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT", "PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX"}


@dataclass(frozen=True)
class Outcome:
    exit_code: int | None  # None when it was stopped for taking too long
    output: str
    seconds: float

    @property
    def timed_out(self) -> bool:
        return self.exit_code is None


def program_environment() -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if name.upper() not in _DROPPED and not _SECRET_NAME.search(name)
    }
    env.update(PYTHONUNBUFFERED="1", PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1", NO_COLOR="1")
    env.setdefault("UV_NO_PROGRESS", "1")
    return env


class _Capped:
    """Keeps the first HEAD_CHARS and last TAIL_CHARS bytes of a stream, however long it gets."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = HEAD_CHARS - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        self.tail += chunk
        if len(self.tail) > TAIL_CHARS:
            cut = len(self.tail) - TAIL_CHARS
            self.dropped += cut
            del self.tail[:cut]

    def text(self) -> str:
        middle = f"\n[... {self.dropped:,} bytes of output left out ...]\n" if self.dropped else ""
        return (bytes(self.head).decode(errors="replace") + middle) + bytes(self.tail).decode(
            errors="replace"
        )


def _stop_tree(process: asyncio.subprocess.Process) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


async def run(command: list[str], cwd: Path, timeout: float) -> Outcome:
    loop = asyncio.get_running_loop()
    started = loop.time()
    extra: dict = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=cwd,
        env=program_environment(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **extra,
    )
    output = _Capped()

    async def pump() -> None:
        assert process.stdout is not None
        while chunk := await process.stdout.read(65536):
            output.add(chunk)
        await process.wait()

    try:
        await asyncio.wait_for(pump(), timeout)
        code: int | None = process.returncode
    except TimeoutError:
        _stop_tree(process)
        code = None
    except asyncio.CancelledError:
        _stop_tree(process)
        raise
    return Outcome(code, output.text(), loop.time() - started)
