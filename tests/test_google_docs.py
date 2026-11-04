import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from vara import google_cli, google_docs
from vara.config import Settings
from vara.main import create_app
from vara.providers.fake import FakeProvider
from vara.router import ModelRouter, PrivacyMode

CLIENT = {"client_id": "id.apps.googleusercontent.com", "client_secret": "GOCSPX-test"}


@pytest.fixture(autouse=True)
def vault(monkeypatch) -> dict:
    """An in-memory credential store, so tests never touch Windows Credential Manager."""
    secrets: dict[str, str] = {}
    monkeypatch.setattr(google_docs, "get_api_key", secrets.get)
    monkeypatch.setattr(google_docs, "set_secret", secrets.__setitem__)
    monkeypatch.setattr(google_docs, "delete_secret", lambda name: secrets.pop(name, None))
    return secrets


@pytest.fixture
def connected(vault) -> dict:
    vault[google_docs.CLIENT_ENTRY] = json.dumps(CLIENT)
    vault[google_docs.TOKEN_ENTRY] = "refresh-123"
    return vault


class FakeGoogle:
    """Answers the token, list, and export endpoints like the real APIs."""

    def __init__(self, *, refresh_ok: bool = True, expire_first_token: bool = False) -> None:
        self.refresh_ok = refresh_ok
        self.expire_first_token = expire_first_token
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = str(request.url)
        if url.startswith("https://oauth2.googleapis.com/token"):
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"access-{self.tokens_issued}"})
        if self.expire_first_token and request.headers["Authorization"] == "Bearer access-1":
            return httpx.Response(401)
        if request.url.path == "/drive/v3/files":
            if request.url.params.get("pageToken") != "p2":
                files = [{"id": "d1", "name": "Trip plan", "modifiedTime": "2026-11-01T10:00:00Z"}]
                return httpx.Response(200, json={"files": files, "nextPageToken": "p2"})
            files = [{"id": "d2", "name": "Recipes", "modifiedTime": "2026-10-01T10:00:00Z"}]
            return httpx.Response(200, json={"files": files})
        if request.url.path == "/drive/v3/files/d1/export":
            return httpx.Response(200, text="Day 1: Porto. Day 2: Douro valley.")
        return httpx.Response(404)


def make_client(google: FakeGoogle) -> TestClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(google))
    router = ModelRouter([FakeProvider("local")], PrivacyMode.LOCAL_ONLY)
    return TestClient(
        create_app(Settings(), router=router, google=google_docs.GoogleDocsClient(http))
    )


def allow(client: TestClient) -> None:
    client.post("/v1/access", json={"kind": "google_docs"})


def test_docs_need_the_setup_permission_even_when_connected(connected) -> None:
    google = FakeGoogle()
    client = make_client(google)

    assert client.get("/v1/google/docs").status_code == 403
    assert google.requests == []  # nothing was sent to Google


def test_listing_and_reading_docs_read_only(connected) -> None:
    google = FakeGoogle()
    client = make_client(google)
    allow(client)

    docs = client.get("/v1/google/docs").json()
    text = client.get("/v1/google/docs/d1/text").json()

    assert [d["name"] for d in docs] == ["Trip plan", "Recipes"]  # both pages
    assert text == {"id": "d1", "text": "Day 1: Porto. Day 2: Douro valley."}
    assert all(r.method in ("GET", "POST") for r in google.requests)
    assert {r.method for r in google.requests if "googleapis.com/drive" in str(r.url)} == {"GET"}
    listing = next(r for r in google.requests if r.url.path == "/drive/v3/files")
    assert "trashed=false" in listing.url.params["q"]
    assert google.tokens_issued == 1  # the access token is reused


def test_an_expired_access_token_is_refreshed_once(connected) -> None:
    google = FakeGoogle(expire_first_token=True)
    client = make_client(google)
    allow(client)

    assert client.get("/v1/google/docs/d1/text").status_code == 200
    assert google.tokens_issued == 2


def test_not_connected_or_revoked_sign_in_says_how_to_fix_it(vault) -> None:
    client = make_client(FakeGoogle())
    allow(client)
    missing = client.get("/v1/google/docs")

    vault[google_docs.CLIENT_ENTRY] = json.dumps(CLIENT)
    vault[google_docs.TOKEN_ENTRY] = "revoked"
    revoked = make_client(FakeGoogle(refresh_ok=False))
    allow(revoked)
    expired = revoked.get("/v1/google/docs")

    assert missing.status_code == expired.status_code == 409
    assert "google connect" in missing.json()["detail"]
    assert "expired or was revoked" in expired.json()["detail"]


def test_missing_document_is_a_clear_error(connected) -> None:
    client = make_client(FakeGoogle())
    allow(client)

    response = client.get("/v1/google/docs/nope/text")

    assert response.status_code == 502
    assert "not found" in response.json()["detail"]


def test_access_overview_shows_google_status(connected) -> None:
    client = make_client(FakeGoogle())

    before = client.get("/v1/access").json()["google_docs"]
    allow(client)
    after = client.get("/v1/access").json()["google_docs"]

    assert before == {"allowed": False, "connected": True}
    assert after == {"allowed": True, "connected": True}


def test_client_file_must_be_a_desktop_app(tmp_path: Path) -> None:
    desktop = tmp_path / "desktop.json"
    desktop.write_text(json.dumps({"installed": {**CLIENT, "project_id": "p"}}))
    web = tmp_path / "web.json"
    web.write_text(json.dumps({"web": CLIENT}))

    assert google_docs.load_client_file(desktop) == CLIENT
    with pytest.raises(google_docs.GoogleError, match="Desktop app"):
        google_docs.load_client_file(web)


def test_connect_asks_for_permission_first_and_stores_secrets(vault, tmp_path, monkeypatch):
    client = make_client(FakeGoogle())
    file = tmp_path / "client_secret.json"
    file.write_text(json.dumps({"installed": CLIENT}))
    signed_in = []

    def fake_sign_in(c):
        signed_in.append(c)
        vault[google_docs.CLIENT_ENTRY] = json.dumps(c)
        vault[google_docs.TOKEN_ENTRY] = "refresh-new"

    monkeypatch.setattr(google_docs, "sign_in", fake_sign_in)

    async def count():
        return 2

    monkeypatch.setattr(google_cli, "_count", count)
    said: list[str] = []
    db = client.app.state.db

    google_cli.run(db, "connect", file, ask=lambda _: "n", say=said.append)
    assert signed_in == [] and said[-1] == "Nothing changed."

    google_cli.run(db, "connect", file, ask=lambda _: "y", say=said.append)
    assert signed_in == [CLIENT]
    assert client.get("/v1/access").json()["google_docs"] == {"allowed": True, "connected": True}
    assert "Connected. Google Docs visible: 2" in said


def test_disconnect_revokes_and_forgets_the_token(connected) -> None:
    revoked = []
    http = httpx.Client(
        transport=httpx.MockTransport(lambda r: revoked.append(r) or httpx.Response(200))
    )

    assert google_docs.disconnect(http) is True
    assert google_docs.TOKEN_ENTRY not in connected
    assert b"token=refresh-123" in revoked[0].content
    assert google_docs.disconnect(http) is False
