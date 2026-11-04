"""What V.A.R.A. may read on this computer. Nothing is read without the user's permission.

Every file read must go through `AccessPolicy.check`, which resolves the path (so `..`,
symlinks, and junctions cannot escape an allowed folder) before comparing it.
"""

import os
import re
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from vara import google_docs
from vara.db.models import AccessGrant, AccessKind, SetupStep

SETUP_STEP = "access"

# Offered at first run. Windows GUIDs find the real location, including OneDrive redirection.
KNOWN_FOLDERS = {
    "documents": ("Documents", "{FDD39AD0-238F-46AF-ADB4-6C85480369C7}"),
    "downloads": ("Downloads", "{374DE290-123F-4565-9164-39C4925E467B}"),
    "desktop": ("Desktop", "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"),
}

# Never read, even inside an allowed folder: credentials, keys, and system or tool folders.
_BLOCKED_DIRS = frozenset(
    [
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".git",
        ".venv",
        "node_modules",
        "appdata",
        "$recycle.bin",
    ]
)
_BLOCKED_FILES = re.compile(
    r"^(\.env(\..+)?|id_(rsa|dsa|ecdsa|ed25519)(\.pub)?|wallet\.dat|credentials(\.json)?"
    r"|.+\.(kdbx|kdb|pem|key|pfx|p12|ppk|gpg|asc|keychain))$",
    re.IGNORECASE,
)
# Drive for Desktop shows Google Docs, Sheets, and Slides as unreadable placeholder files.
GOOGLE_PLACEHOLDERS = frozenset({".gdoc", ".gsheet", ".gslides", ".gdraw", ".gform", ".gmap"})
NEVER_READ = (
    "V.A.R.A.'s own data folder; files such as .env, private keys, certificates, and "
    "password databases; and folders such as .ssh, .git, AppData, and node_modules"
)


def known_folder(key: str) -> Path:
    name, guid = KNOWN_FOLDERS[key]
    if sys.platform == "win32" and (path := _windows_known_folder(guid)):
        return path
    return Path.home() / name


def find_google_drive() -> Path | None:
    """My Drive from Google Drive for Desktop, which mounts it as a drive letter (often G:)."""
    if sys.platform != "win32":
        return None
    for letter in "GHIJKLMNOPQRSTUVWXYZDEF":
        if (path := Path(f"{letter}:/My Drive")).is_dir():
            return path
    return None


def suggested_folders() -> dict[str, Path]:
    """Folders offered at setup: Documents, Downloads, Desktop, and Google Drive if installed."""
    folders = {key: known_folder(key) for key in KNOWN_FOLDERS}
    if drive := find_google_drive():
        folders["google_drive"] = drive
    return folders


def _windows_known_folder(guid: str) -> Path | None:
    import ctypes

    folder_id = (ctypes.c_char * 16).from_buffer_copy(uuid.UUID(guid).bytes_le)
    out = ctypes.c_wchar_p()
    if ctypes.windll.shell32.SHGetKnownFolderPath(folder_id, 0, None, ctypes.byref(out)) != 0:
        return None
    try:
        return Path(out.value) if out.value else None
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def resolve(path: str | Path) -> Path:
    return Path(os.path.abspath(path)).resolve()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


@dataclass(frozen=True)
class AccessPolicy:
    allowed: tuple[Path, ...]
    excluded: tuple[Path, ...]
    protected: tuple[Path, ...]  # always blocked, such as V.A.R.A.'s data folder

    def check(self, path: str | Path) -> Decision:
        target = resolve(path)
        if any(target.is_relative_to(p) for p in self.protected):
            return Decision(False, "V.A.R.A.'s own data is never read")
        if _BLOCKED_FILES.match(target.name):
            return Decision(False, "credentials, keys, and system folders are never read")
        if target.suffix.lower() in GOOGLE_PLACEHOLDERS:
            return Decision(
                False, "a Google Drive placeholder; Google Docs are read through Google"
            )
        if any(target.is_relative_to(p) for p in self.excluded):
            return Decision(False, "inside a folder you excluded")
        roots = [p for p in self.allowed if target.is_relative_to(p)]
        if not roots:
            return Decision(False, "not inside a folder you allowed")
        # Blocked folder names count below the allowed folder only, so explicitly allowing
        # a folder that happens to sit under, say, AppData still works.
        inside = target.relative_to(max(roots, key=lambda p: len(p.parts))).parts
        if any(part.lower() in _BLOCKED_DIRS for part in inside):
            return Decision(False, "credentials, keys, and system folders are never read")
        return Decision(True, "inside a folder you allowed")


def policy(session: Session, data_dir: Path) -> AccessPolicy:
    grants = list(session.scalars(select(AccessGrant)))
    return AccessPolicy(
        allowed=tuple(Path(g.path) for g in grants if g.kind == AccessKind.FOLDER),
        excluded=tuple(Path(g.path) for g in grants if g.kind == AccessKind.EXCLUDE),
        protected=(resolve(data_dir),),
    )


def setup_complete(session: Session) -> bool:
    return session.get(SetupStep, SETUP_STEP) is not None


def google_docs_allowed(session: Session) -> bool:
    return _google_grant(session) is not None


def _google_grant(session: Session) -> AccessGrant | None:
    return session.scalar(select(AccessGrant).where(AccessGrant.kind == AccessKind.GOOGLE_DOCS))


def validate_folder(path: str | Path) -> Path:
    """The resolved folder, or ValueError with a message the user can act on."""
    if not Path(path).is_absolute():
        raise ValueError(f"Use a full path, such as C:\\Users\\you\\Notes: {path}")
    folder = resolve(path)
    if not folder.is_dir():
        raise ValueError(f"Not a folder on this computer: {path}")
    return folder


def add(session: Session, kind: AccessKind, path: str | Path | None = None) -> AccessGrant:
    if kind == AccessKind.GOOGLE_DOCS:
        existing = _google_grant(session)
        if existing:
            return existing
        grant = AccessGrant(kind=kind, path=None)
    else:
        folder = str(validate_folder(path or ""))
        existing = session.scalar(
            select(AccessGrant).where(AccessGrant.kind == kind, AccessGrant.path == folder)
        )
        if existing:
            return existing
        grant = AccessGrant(kind=kind, path=folder)
    session.add(grant)
    session.flush()
    return grant


def complete_setup(
    session: Session,
    *,
    folders: Sequence[str | Path],
    exclude: Sequence[str | Path] = (),
    google_docs: bool = False,
) -> None:
    """Replace every grant with the user's first-run choices and mark setup as done.

    All paths are validated before anything changes, so a bad path leaves the old choices."""
    allowed = [validate_folder(f) for f in folders]
    excluded = [validate_folder(f) for f in exclude]
    session.execute(delete(AccessGrant))
    for folder in allowed:
        add(session, AccessKind.FOLDER, folder)
    for folder in excluded:
        add(session, AccessKind.EXCLUDE, folder)
    if google_docs:
        add(session, AccessKind.GOOGLE_DOCS)
    session.merge(SetupStep(key=SETUP_STEP))
    session.flush()


def grant_dict(g: AccessGrant) -> dict:
    return {"id": g.id, "kind": g.kind, "path": g.path, "granted_at": g.granted_at.isoformat()}


def overview(session: Session) -> dict:
    grants = session.scalars(select(AccessGrant).order_by(AccessGrant.id))
    return {
        "setup_complete": setup_complete(session),
        "suggested": [
            {"key": key, "path": str(path), "exists": path.is_dir()}
            for key, path in suggested_folders().items()
        ],
        "grants": [grant_dict(g) for g in grants],
        "google_docs": {
            "allowed": _google_grant(session) is not None,
            "connected": google_docs.is_connected(),
        },
        "never_read": NEVER_READ,
    }
