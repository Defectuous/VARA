# V.A.R.A.

Visual and Audio Reality Assistant. The initial backend is a local-only FastAPI service.

## Setup

Install Python 3.12 with `uv` and sync the project environment:

```powershell
uv python pin 3.12
uv sync
```

Run the checks:

```powershell
uv run pytest
uv run ruff check .
```

Start the API bound to this computer only:

```powershell
uv run python -m vara
```

The health endpoint is available at `http://127.0.0.1:8000/v1/health` (and unversioned at `/health`).
Interactive API docs are at `/docs` in development.

## Configuration

Settings come from `VARA_*` environment variables or a local `.env` file (never committed):

| Variable | Default | Notes |
| --- | --- | --- |
| `VARA_ENVIRONMENT` | `development` | `production` disables `/docs` and the OpenAPI schema |
| `VARA_HOST` | `127.0.0.1` | Anything other than localhost logs a warning |
| `VARA_PORT` | `8000` | |
| `VARA_LOG_LEVEL` | `INFO` | |
| `VARA_DATA_DIR` | `data` | Holds `vara.db` and `backups/`; ignored by Git |
| `VARA_RETENTION_ON_STARTUP` | `true` | Apply retention rules each time the server starts |
| `VARA_PRIVACY_MODE` | `local_only` | `local_only` blocks all cloud calls; `ask` needs `confirm_cloud: true` per request; `allowed` permits them |
| `VARA_OLLAMA_URL` | `http://127.0.0.1:11434` | |
| `VARA_OLLAMA_MODEL` | `llama3.1:8b` | |
| `VARA_ANTHROPIC_MODEL` | `claude-opus-5-5` | |
| `VARA_ANTHROPIC_EFFORT` | `medium` | `low`, `medium`, `high`, `xhigh`, or `max` |

Logs are JSON lines, with API keys, bearer tokens, and password/secret fields redacted.

API keys are not configuration. Store them in Windows Credential Manager under the service `vara`:

```powershell
uv run --with keyring python -c "import keyring, getpass; keyring.set_password('vara', 'anthropic', getpass.getpass('Key: '))"
```

A cloud provider is enabled only when its key is present. Check with `GET /v1/providers`.

## Chat

```powershell
$body = @{ messages = @(@{ role = "user"; content = "Hello" }) } | ConvertTo-Json -Depth 3
Invoke-RestMethod http://127.0.0.1:8000/v1/chat -Method Post -ContentType application/json -Body $body
```

Every exchange is stored. The response includes `conversation_id`; to continue that conversation, send it
back with only the new message, and the stored history is included automatically.

Optional fields: `conversation_id`, `provider` (for example `"anthropic"`; a manual choice never falls back),
`model`, `system`, `confirm_cloud` (required in `ask` mode), and `use_memory` (default `true`). Without a
provider, local models are tried before cloud ones. The response names the provider and model that answered,
with token usage, estimated cost (`0.0` for local models, `null` for a cloud model without a known price), and
`memories_used`, which lists the memories that were added to the request.

Other chat endpoints take the same body:

| Endpoint | What it does |
| --- | --- |
| `POST /v1/chat/stream` | Server-sent events: `delta` per text chunk, then `done` (usage and cost) or `error`. Privacy and provider failures before the first chunk are normal HTTP errors. |
| `POST /v1/chat/preview` | Calls no model and stores nothing. Lists the memories that would be used and the providers that would be tried, in order, with the exact request each would receive. |

A failed or blocked request stores nothing.

## Memory and history

Memories are facts you choose to keep, stored separately from conversation history. For each request, the
confirmed memories that best match the latest message (keyword search, up to five) are added to the system
prompt. Memories marked `proposed` are never used until you confirm them.

| Endpoint | What it does |
| --- | --- |
| `GET /v1/memories?status=` | List memories, optionally only `confirmed` or `proposed` |
| `POST /v1/memories` | Add one: `content`, optional `category`, `status`, `pinned`, and `source_message_id` (provenance) |
| `PATCH /v1/memories/{id}` | Correct `content` or `category`, approve with `status: "confirmed"`, or set `pinned` |
| `POST /v1/conversations/{id}/suggest-memories` | A model proposes facts from your messages in that conversation (see below) |
| `DELETE /v1/memories/{id}` | Delete it, including from the search index |
| `GET /v1/conversations`, `GET /v1/conversations/{id}` | List conversations, or one with its messages |
| `PATCH` / `DELETE /v1/conversations/{id}` | Rename, or delete with all its messages |
| `GET /v1/search?q=` | Full-text search over messages (with snippets) and memories |
| `GET /v1/usage` | Requests, tokens, and cost per provider and model, all time |
| `GET /v1/export` | Everything as one JSON document |

Deleted text is overwritten in the database file, not just hidden.

### Suggested memories

`suggest-memories` sends only your own messages from one conversation to a model, routed under the privacy
mode like chat (optional `provider`, `model`, `confirm_cloud`). Each suggested fact gets a category and a link
to the message it came from, and is saved as `proposed`. Suggestions are dropped if they look like a
credential, share no meaningful word with what you wrote (which catches invented facts), or duplicate an
existing memory. Approve a suggestion with `PATCH /v1/memories/{id}` and `status: "confirmed"`.

Categories: `identity`, `preference`, `relationship`, `location`, `work`, `health`, `finance`, `commitment`,
and `other`.

### Retention

There is one rule for each kind of data. `keep_days: null` means keep until you delete it.

| Rule | Default |
| --- | --- |
| `conversations` | Keep (counted from the last message) |
| `proposed` (unapproved suggestions) | 30 days |
| Every memory category, `commitment` included | Keep |

`GET /v1/retention` lists every rule with how much you have saved of each kind, the categories you use most
first, and flags `health` and `finance` as sensitive. Change a rule with `PUT /v1/retention/{key}` and
`{"keep_days": 90}` or `{"keep_days": null}`. A **pinned** memory is never deleted by any rule, so you can keep
particular items when a category has a time limit. `GET /v1/retention/due` shows what the current rules would
delete, so you can pin items first. Rules are applied when the server starts (`VARA_RETENTION_ON_STARTUP=false`
turns that off) and by `POST /v1/retention/apply`.

## File access

V.A.R.A. reads none of your files until you allow it. Run the first-run setup in a terminal:

```powershell
uv run python -m vara setup
```

It offers Documents, Downloads, and Desktop together, plus `My Drive` if Google Drive for Desktop is
installed. If you decline, it asks about each one, then for any
other folders, then for folders inside those to keep private, and whether it may read your Google Docs (see
[Google Docs](#google-docs) for connecting the account). Running it again replaces your choices. The
server logs a warning on startup until setup is done.

Some things are never read, whatever you allow: V.A.R.A.'s own data folder; files such as `.env`, private
keys (`id_rsa`, `*.pem`, `*.key`, `*.pfx`), and password databases (`*.kdbx`); and folders named `.ssh`,
`.git`, `AppData`, or `node_modules` inside an allowed folder. Drive placeholders (`.gdoc`, `.gsheet`) are
skipped; Google Docs are read through Google instead. Paths are resolved before checking, so `..`,
shortcuts, symlinks, and junctions cannot reach outside an allowed folder.

| Endpoint | What it does |
| --- | --- |
| `GET /v1/access` | Whether setup is done, the suggested folders, your current permissions, and Google Docs status |
| `POST /v1/access/setup` | The same choices as the terminal setup: `folders` (`documents`, `downloads`, `desktop`, `google_drive`, or full paths), `exclude`, `google_docs` |
| `POST /v1/access` | Add one permission: `kind` (`folder`, `exclude`, `google_docs`) and `path` |
| `DELETE /v1/access/{id}` | Revoke one |
| `GET /v1/access/check?path=` | Whether a path may be read, and why |

### Google Docs

V.A.R.A. reads Google Docs through the Google Drive API with read-only access: it can list and read your
documents but never change, share, or delete anything. It needs both the Google Docs permission from setup and
a connected account. Google Drive for Desktop does not help here, because its `.gdoc` files are only links;
it is still useful for ordinary files in Drive (PDFs, Word files), which you can allow as a folder, such as
`G:\My Drive`.

One-time setup in [Google Cloud Console](https://console.cloud.google.com/), signed in with the Google account
whose documents you want:

1. Create a project (for example `VARA`).
2. **APIs & Services → Library**: enable the **Google Drive API**.
3. **Google Auth Platform → Branding**: fill in the app name and your email. **Audience**: choose
   **External**, and add your own address as a test user.
4. **Clients → Create client**: application type **Desktop app**. Download the JSON file.
5. **Audience → Publish app**. In *Testing*, Google expires the sign-in every 7 days; once published, it
   lasts until you revoke it. Google will not review an app only you use, so the sign-in page warns that it
   is unverified: choose **Advanced**, then go to the app.

Then connect:

```powershell
uv run python -m vara google connect C:\path\to\client_secret.json   # opens the browser to sign in
uv run python -m vara google status
uv run python -m vara google disconnect                               # revokes access at Google too
```

The client and the sign-in token are stored in Windows Credential Manager (`vara/google-client` and
`vara/google`), so you can delete the downloaded JSON file afterwards. You can also remove access at any time
at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).

| Endpoint | What it does |
| --- | --- |
| `GET /v1/google/docs?limit=` | Your Google Docs, most recently modified first (not including the bin) |
| `GET /v1/google/docs/{id}/text` | One document as plain text |

Both return 403 until Google Docs is allowed in setup, and 409 with instructions if the account is not
connected or the sign-in has expired.

## Backups

```powershell
uv run python -m vara backup                       # writes data/backups/vara-<timestamp>.db, verified
uv run python -m vara restore <backup.db> --yes    # stop the server first; replaces the database
```

A backup can be taken while the server is running. Store copies off this PC, and keep the drive encrypted
(BitLocker), because the database holds your conversations and memories in readable form.

Keep the service bound to localhost until device authentication is implemented.
