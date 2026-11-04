import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vara import access, setup_wizard
from vara.config import Settings
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """A fake user folder with Documents, Downloads, and Desktop."""
    root = tmp_path / "home"
    for name in ("Documents", "Downloads", "Desktop"):
        (root / name).mkdir(parents=True)
    paths = {"documents": "Documents", "downloads": "Downloads", "desktop": "Desktop"}
    monkeypatch.setattr(access, "known_folder", lambda key: root / paths[key])
    monkeypatch.setattr(access, "find_google_drive", lambda: None)
    return root


@pytest.fixture
def client() -> TestClient:
    router = ModelRouter([FakeProvider("local")], PrivacyMode.LOCAL_ONLY)
    return TestClient(create_app(Settings(), router=router))


def check(client: TestClient, path: Path) -> dict:
    return client.get("/v1/access/check", params={"path": str(path)}).json()


def test_nothing_is_readable_before_setup(client, home) -> None:
    overview = client.get("/v1/access").json()

    assert overview["setup_complete"] is False
    assert overview["grants"] == []
    assert [s["key"] for s in overview["suggested"]] == ["documents", "downloads", "desktop"]
    assert check(client, home / "Documents" / "notes.txt")["allowed"] is False


def test_allowing_all_three_folders(client, home) -> None:
    body = {"folders": ["documents", "downloads", "desktop"], "google_docs": True}

    overview = client.post("/v1/access/setup", json=body).json()

    assert overview["setup_complete"] is True
    assert [g["kind"] for g in overview["grants"]] == ["folder"] * 3 + ["google_docs"]
    assert overview["google_docs"] == {"allowed": True, "connected": False}
    for name in ("Documents", "Downloads", "Desktop"):
        assert check(client, home / name / "a" / "file.pdf")["allowed"] is True
    assert check(client, home / "Pictures" / "cat.jpg")["allowed"] is False


def test_choosing_only_some_folders_and_keeping_one_private(client, home, tmp_path) -> None:
    other = tmp_path / "Projects"
    (other / "secret").mkdir(parents=True)

    client.post(
        "/v1/access/setup",
        json={"folders": ["documents", str(other)], "exclude": [str(other / "secret")]},
    )

    assert check(client, home / "Documents" / "x.md")["allowed"] is True
    assert check(client, home / "Downloads" / "x.md")["allowed"] is False
    assert check(client, other / "readme.md")["allowed"] is True
    assert check(client, other / "secret" / "plan.md") == {
        "path": str((other / "secret" / "plan.md").resolve()),
        "allowed": False,
        "reason": "inside a folder you excluded",
    }


def test_paths_cannot_escape_an_allowed_folder(client, home) -> None:
    client.post("/v1/access/setup", json={"folders": ["documents"]})

    sneaky = home / "Documents" / ".." / "Downloads" / "bank.pdf"
    assert check(client, sneaky)["allowed"] is False
    assert check(client, str(home / "DOCUMENTS" / "x.txt").upper())["allowed"] is (os.name == "nt")


def test_links_out_of_an_allowed_folder_are_followed_and_refused(client, home) -> None:
    client.post("/v1/access/setup", json={"folders": ["documents"]})
    link = home / "Documents" / "shortcut"
    if os.name == "nt":  # a junction needs no admin rights, unlike a symlink
        import _winapi

        _winapi.CreateJunction(str(home / "Downloads"), str(link))
    else:
        link.symlink_to(home / "Downloads", target_is_directory=True)

    assert check(client, link / "bank.pdf")["allowed"] is False


def test_credentials_and_vara_data_are_never_read(client, home, isolated_data_dir) -> None:
    client.post("/v1/access/setup", json={"folders": ["documents"]})
    docs = home / "Documents"

    for path in (
        docs / "project" / ".env",
        docs / "keys" / "id_ed25519",
        docs / "Passwords.kdbx",
        docs / "certs" / "server.pem",
        docs / "repo" / ".git" / "config",
        docs / "app" / "node_modules" / "x.js",
    ):
        assert check(client, path)["allowed"] is False, path

    # V.A.R.A.'s own database stays off-limits even inside an allowed folder.
    client.post("/v1/access", json={"kind": "folder", "path": str(isolated_data_dir.parent)})
    assert check(client, isolated_data_dir / "vara.db")["reason"] == (
        "V.A.R.A.'s own data is never read"
    )


def test_a_bad_path_changes_nothing(client, home) -> None:
    client.post("/v1/access/setup", json={"folders": ["documents"]})

    for folders in (["relative/path"], [str(home / "missing")]):
        response = client.post("/v1/access/setup", json={"folders": ["downloads", *folders]})
        assert response.status_code == 422

    grants = client.get("/v1/access").json()["grants"]
    assert [g["path"] for g in grants] == [str((home / "Documents").resolve())]


def test_adding_and_revoking_a_folder(client, home) -> None:
    grant = client.post("/v1/access", json={"kind": "folder", "path": "desktop"}).json()
    again = client.post("/v1/access", json={"kind": "folder", "path": "desktop"}).json()
    assert grant["id"] == again["id"]
    assert check(client, home / "Desktop" / "todo.txt")["allowed"] is True

    assert client.delete(f"/v1/access/{grant['id']}").status_code == 204
    assert check(client, home / "Desktop" / "todo.txt")["allowed"] is False
    assert client.delete(f"/v1/access/{grant['id']}").status_code == 404


def test_terminal_setup_asks_for_each_folder_when_not_all(client, home, tmp_path) -> None:
    extra = tmp_path / "Notes"
    extra.mkdir()
    answers = iter(["n", "n", "y", "n", str(home / "nope"), str(extra), "", "", "y"])
    said: list[str] = []

    summary = setup_wizard.run(client.app.state.db, ask=lambda _: next(answers), say=said.append)

    assert [g["path"] for g in summary["grants"] if g["kind"] == "folder"] == [
        str((home / "Downloads").resolve()),
        str(extra.resolve()),
    ]
    assert summary["google_docs"]["allowed"] is True
    assert any("Not a folder" in line for line in said)


def test_terminal_setup_allow_all(client, home) -> None:
    answers = iter(["y", "", "n"])

    summary = setup_wizard.run(client.app.state.db, ask=lambda _: next(answers), say=lambda _: None)

    assert len([g for g in summary["grants"] if g["kind"] == "folder"]) == 3
    assert summary["google_docs"]["allowed"] is False


def test_google_drive_is_offered_when_installed(client, home, monkeypatch, tmp_path) -> None:
    drive = tmp_path / "G" / "My Drive"
    drive.mkdir(parents=True)
    monkeypatch.setattr(access, "find_google_drive", lambda: drive)

    suggested = client.get("/v1/access").json()["suggested"]
    client.post("/v1/access/setup", json={"folders": ["google_drive"]})

    assert suggested[-1] == {"key": "google_drive", "path": str(drive), "exists": True}
    assert check(client, drive / "Taxes 2025.pdf")["allowed"] is True
    placeholder = check(client, drive / "Trip plan.gdoc")
    assert placeholder["allowed"] is False
    assert "placeholder" in placeholder["reason"]


def test_terminal_setup_offers_google_drive(client, home, monkeypatch, tmp_path) -> None:
    drive = tmp_path / "G" / "My Drive"
    drive.mkdir(parents=True)
    monkeypatch.setattr(access, "find_google_drive", lambda: drive)
    asked: list[str] = []
    answers = iter(["n", "n", "n", "n", "y", "", "", "y"])

    def ask(question: str) -> str:
        asked.append(question)
        return next(answers)

    summary = setup_wizard.run(client.app.state.db, ask=ask, say=lambda _: None)

    assert "Allow Google Drive? [y/N] " in asked
    assert [g["path"] for g in summary["grants"] if g["kind"] == "folder"] == [str(drive.resolve())]
