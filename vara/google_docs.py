"""Read-only access to the user's Google Docs through the Google Drive API.

The OAuth client (created by the user in Google Cloud Console) and the refresh token live in
the OS credential store, never in files or the database. Nothing is fetched unless the user
also allowed Google Docs at setup.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from vara.credentials import delete_secret, get_api_key, set_secret

# Read-only: V.A.R.A. can never change, share, or delete anything in Drive.
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
CLIENT_ENTRY = "google-client"  # the OAuth client: client_id and client_secret
TOKEN_ENTRY = "google"  # the refresh token from signing in

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_FILES_URL = "https://www.googleapis.com/drive/v3/files"
_DOC_TYPE = "application/vnd.google-apps.document"


class GoogleError(Exception):
    """A failure the user can act on, with a message safe to show them."""


class NotConnected(GoogleError):
    pass


@dataclass(frozen=True)
class GoogleDoc:
    id: str
    name: str
    modified: str
    link: str


def load_client_file(path: Path) -> dict:
    """The client_id and client_secret from the JSON downloaded from Google Cloud Console."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GoogleError(f"Could not read the OAuth client file: {error}") from None
    client = data.get("installed")
    if not client:
        raise GoogleError(
            "This is not a Desktop app OAuth client. In Google Cloud Console, create an "
            "OAuth client ID with application type 'Desktop app' and download its JSON."
        )
    return {"client_id": client["client_id"], "client_secret": client["client_secret"]}


def sign_in(client: dict) -> None:
    """Open the browser for Google's consent screen and store the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    config = {
        "installed": {
            **client,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": _TOKEN_URL,
            "redirect_uris": ["http://127.0.0.1"],
        }
    }
    flow = InstalledAppFlow.from_client_config(config, SCOPES)
    credentials = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=True, prompt="consent", access_type="offline"
    )
    if not credentials.refresh_token:
        raise GoogleError("Google did not return a refresh token; run connect again.")
    set_secret(CLIENT_ENTRY, json.dumps(client))
    set_secret(TOKEN_ENTRY, credentials.refresh_token)


def stored_client() -> dict | None:
    raw = get_api_key(CLIENT_ENTRY)
    return json.loads(raw) if raw else None


def is_connected() -> bool:
    return bool(get_api_key(TOKEN_ENTRY) and stored_client())


class GoogleDocsClient:
    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self._http = http or httpx.AsyncClient(timeout=30)
        self._access_token: str | None = None

    async def _token(self) -> str:
        if self._access_token:
            return self._access_token
        client, refresh = stored_client(), get_api_key(TOKEN_ENTRY)
        if not client or not refresh:
            raise NotConnected("Google is not connected. Run: uv run python -m vara google connect")
        try:
            response = await self._http.post(
                _TOKEN_URL,
                data={**client, "refresh_token": refresh, "grant_type": "refresh_token"},
            )
        except httpx.HTTPError as error:
            raise GoogleError(f"Could not reach Google: {type(error).__name__}") from None
        if response.status_code in (400, 401):
            raise NotConnected(
                "Google sign-in has expired or was revoked. Run: uv run python -m vara google connect"
            )
        if response.is_error:
            raise GoogleError(f"Google sign-in failed with status {response.status_code}")
        self._access_token = response.json()["access_token"]
        return self._access_token

    async def _get(self, url: str, params: dict) -> httpx.Response:
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {await self._token()}"}
            try:
                response = await self._http.get(url, params=params, headers=headers)
            except httpx.HTTPError as error:
                raise GoogleError(f"Could not reach Google: {type(error).__name__}") from None
            if response.status_code == 401 and attempt == 0:
                self._access_token = None  # expired mid-session; refresh once and retry
                continue
            if response.status_code == 404:
                raise GoogleError("That document was not found, or is not shared with you.")
            if response.status_code == 403:
                raise GoogleError(f"Google refused the request: {response.text[:200]}")
            if response.is_error:
                raise GoogleError(f"Google returned status {response.status_code}")
            return response
        raise GoogleError("Google rejected the sign-in; run connect again.")

    async def list_docs(self, limit: int = 1000) -> list[GoogleDoc]:
        """Google Docs the account can see, most recently modified first (not in the bin)."""
        docs: list[GoogleDoc] = []
        params = {
            "q": f"mimeType='{_DOC_TYPE}' and trashed=false",
            "fields": "nextPageToken,files(id,name,modifiedTime,webViewLink)",
            "orderBy": "modifiedTime desc",
            "pageSize": 100,
        }
        while len(docs) < limit:
            body = (await self._get(_FILES_URL, params)).json()
            docs += [
                GoogleDoc(f["id"], f["name"], f["modifiedTime"], f.get("webViewLink", ""))
                for f in body.get("files", [])
            ]
            if not (page := body.get("nextPageToken")):
                break
            params = {**params, "pageToken": page}
        return docs[:limit]

    async def text(self, doc_id: str) -> str:
        """A document's text, exported by Google as plain text (Google limits this to 10 MB)."""
        url = f"{_FILES_URL}/{doc_id}/export"
        return (await self._get(url, {"mimeType": "text/plain"})).text

    async def aclose(self) -> None:
        await self._http.aclose()


def disconnect(http: httpx.Client | None = None) -> bool:
    """Revoke the token at Google and remove it from this PC. Returns whether one existed."""
    refresh = get_api_key(TOKEN_ENTRY)
    if refresh:
        try:
            (http or httpx.Client(timeout=15)).post(_REVOKE_URL, data={"token": refresh})
        except httpx.HTTPError:
            pass  # still removed locally; it can also be removed at myaccount.google.com
    delete_secret(TOKEN_ENTRY)
    return refresh is not None
