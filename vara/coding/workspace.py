"""V.A.R.A.'s own Python projects: one folder each under data/projects, each a git repo with its own uv
environment. The coding agent can't see or change anything outside them.

Every path is resolved (so `..`, symlinks, and junctions can't escape) and checked against its project
before it's read or written. Changes are committed to the project's git history, so each one can be undone.
"""

import asyncio
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
# Tool and environment folders: never listed, read, or written by the agent.
SKIP_DIRS = frozenset(
    {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", "node_modules"}
)
MAX_READ_BYTES = 200_000
MAX_WRITE_BYTES = 500_000
MAX_LISTED = 500

GITIGNORE = ".venv/\n__pycache__/\n*.pyc\n.pytest_cache/\n.ruff_cache/\n"
_GIT_IDENTITY = ["-c", "user.name=V.A.R.A.", "-c", "user.email=vara@localhost"]


class ProjectError(Exception):
    """A project or path the agent can't use; the message is told to the model."""


@dataclass(frozen=True)
class Saved:
    """Whether a change made it into the project's git history (False: git missing or it failed)."""

    committed: bool
    note: str = ""


def _pyproject(name: str, description: str) -> str:
    description = description.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        f'description = "{description}"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = []\n"
    )


class Projects:
    def __init__(self, root: Path) -> None:
        self.root = root

    # ---- projects ----

    def project(self, name: str) -> Path:
        if not NAME.match(name):
            raise ProjectError(
                f'"{name}" isn\'t a valid project name: use lowercase letters, digits, - and _ '
                "(up to 40 characters, starting with a letter or digit)"
            )
        path = self.root / name
        if not path.is_dir():
            known = ", ".join(p["name"] for p in self.all()) or "none yet"
            raise ProjectError(f'There is no project "{name}". Projects: {known}.')
        return path

    def all(self) -> list[dict]:
        if not self.root.is_dir():
            return []
        found = []
        for path in sorted(p for p in self.root.iterdir() if p.is_dir() and NAME.match(p.name)):
            files = self.files(path.name)
            found.append(
                {
                    "name": path.name,
                    "description": _description(path),
                    "files": len(files),
                    "path": str(path.resolve()),
                }
            )
        return found

    async def create(self, name: str, description: str) -> Path:
        if not NAME.match(name):
            self.project(name)  # raises the name error
        path = self.root / name
        if path.exists():
            raise ProjectError(f'A project named "{name}" already exists')
        path.mkdir(parents=True)
        (path / "pyproject.toml").write_text(_pyproject(name, description), encoding="utf-8")
        (path / "README.md").write_text(f"# {name}\n\n{description}\n", encoding="utf-8")
        (path / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        if git := shutil.which("git"):
            await _git(git, path, "init", "-q")
        await self.save(name, f"Start {name}")
        return path

    # ---- paths ----

    def resolve(self, name: str, relative: str) -> Path:
        """A path inside a project, or ProjectError."""
        project = self.project(name).resolve()
        cleaned = relative.strip().replace("\\", "/") or "."
        if Path(cleaned).is_absolute() or re.match(r"^[A-Za-z]:", cleaned):
            raise ProjectError("Use a path relative to the project folder")
        target = (project / cleaned).resolve()
        if not target.is_relative_to(project):
            raise ProjectError(f'"{relative}" is outside the project')
        inside = target.relative_to(project).parts
        if any(part in SKIP_DIRS for part in inside):
            raise ProjectError(f'"{relative}" is in a tool folder ({", ".join(sorted(SKIP_DIRS))})')
        return target

    def relative(self, name: str, path: Path) -> str:
        return path.relative_to(self.project(name).resolve()).as_posix() or "."

    def files(self, name: str, under: str = ".") -> list[tuple[str, int]]:
        """(path, size) of the files in a project (or a folder of it), skipping tool folders."""
        project = self.project(name).resolve()
        start = self.resolve(name, under)
        if start.is_file():
            return [(start.relative_to(project).as_posix(), start.stat().st_size)]
        found: list[tuple[str, int]] = []
        if not start.is_dir():
            return found
        stack = [start]
        while stack and len(found) < MAX_LISTED:
            folder = stack.pop()
            for entry in sorted(folder.iterdir(), reverse=True):
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in SKIP_DIRS:
                        stack.append(entry)
                elif entry.is_file():
                    found.append((entry.relative_to(project).as_posix(), entry.stat().st_size))
        return sorted(found)

    # ---- reading and writing ----

    def read(self, name: str, relative: str) -> str:
        path = self.resolve(name, relative)
        if not path.is_file():
            raise ProjectError(f'There is no file "{relative}" in {name}')
        data = path.read_bytes()
        if len(data) > MAX_READ_BYTES:
            raise ProjectError(
                f"{relative} is {len(data):,} bytes, too big to read whole; read a range of lines"
                if b"\0" not in data[:8000]
                else f"{relative} is a binary file"
            )
        if b"\0" in data[:8000]:
            raise ProjectError(f"{relative} is a binary file")
        return data.decode("utf-8", errors="replace")

    def read_lines(self, name: str, relative: str) -> list[str]:
        """All lines of a text file of any size (for reading a range)."""
        path = self.resolve(name, relative)
        if not path.is_file():
            raise ProjectError(f'There is no file "{relative}" in {name}')
        with path.open("rb") as handle:
            if b"\0" in handle.read(8000):
                raise ProjectError(f"{relative} is a binary file")
        return path.read_text(encoding="utf-8", errors="replace").splitlines()

    def write(self, name: str, relative: str, content: str) -> bool:
        """Write a text file; True if it was created."""
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise ProjectError(f"That's more than {MAX_WRITE_BYTES:,} bytes; split it into files")
        path = self.resolve(name, relative)
        if path == self.project(name).resolve() or path.is_dir():
            raise ProjectError(f'"{relative}" is a folder')
        created = not path.exists()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="")
        return created

    def delete(self, name: str, relative: str) -> None:
        path = self.resolve(name, relative)
        if not path.is_file():
            raise ProjectError(f'There is no file "{relative}" in {name}')
        path.unlink()

    # ---- history ----

    async def save(self, name: str, message: str) -> Saved:
        """Commit everything changed in the project."""
        git = shutil.which("git")
        if git is None:
            return Saved(False, "git isn't installed, so this change can't be undone")
        path = self.project(name)
        code, out = await _git(git, path, "add", "-A")
        if code == 0:
            code, out = await _git(git, path, *_GIT_IDENTITY, "commit", "-q", "-m", message)
        if code != 0 and "nothing to commit" in out:
            return Saved(False, "nothing changed")
        if code != 0:
            return Saved(False, f"couldn't save it to the project history: {out.strip()[:200]}")
        return Saved(True)

    async def undo(self, name: str) -> str:
        """Undo the latest change with a new commit (so the undo can be undone too)."""
        git = shutil.which("git")
        if git is None:
            raise ProjectError("git isn't installed, so changes can't be undone")
        path = self.project(name)
        code, out = await _git(git, path, "log", "-2", "--format=%s")
        subjects = out.splitlines() if code == 0 else []
        if len(subjects) < 2:
            raise ProjectError("There's no change to undo yet")
        code, out = await _git(git, path, *_GIT_IDENTITY, "revert", "--no-edit", "HEAD")
        if code != 0:
            await _git(git, path, "revert", "--abort")
            raise ProjectError(f"Couldn't undo it: {out.strip()[:200]}")
        return subjects[0]

    async def history(self, name: str, limit: int = 20) -> list[str]:
        git = shutil.which("git")
        if git is None:
            return []
        code, out = await _git(
            git, self.project(name), "log", f"-{limit}", "--format=%h %ad %s", "--date=short"
        )
        return out.splitlines() if code == 0 else []


def _description(path: Path) -> str:
    try:
        text = (path / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r'^description\s*=\s*"(.*)"\s*$', text, re.MULTILINE)
    return match.group(1) if match else ""


async def _git(git: str, cwd: Path, *args: str) -> tuple[int, str]:
    process = await asyncio.create_subprocess_exec(
        git,
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await process.communicate()
    return process.returncode or 0, out.decode(errors="replace")
