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
| `POST /v1/memories` | Add one: `content`, optional `status` and `source_message_id` (provenance) |
| `PATCH /v1/memories/{id}` | Correct `content`, or approve with `status: "confirmed"` |
| `DELETE /v1/memories/{id}` | Delete it, including from the search index |
| `GET /v1/conversations`, `GET /v1/conversations/{id}` | List conversations, or one with its messages |
| `PATCH` / `DELETE /v1/conversations/{id}` | Rename, or delete with all its messages |
| `GET /v1/search?q=` | Full-text search over messages (with snippets) and memories |
| `GET /v1/usage` | Requests, tokens, and cost per provider and model, all time |
| `GET /v1/export` | Everything as one JSON document |

Deleted text is overwritten in the database file, not just hidden.

## Backups

```powershell
uv run python -m vara backup                       # writes data/backups/vara-<timestamp>.db, verified
uv run python -m vara restore <backup.db> --yes    # stop the server first; replaces the database
```

A backup can be taken while the server is running. Store copies off this PC, and keep the drive encrypted
(BitLocker), because the database holds your conversations and memories in readable form.

Keep the service bound to localhost until device authentication is implemented.
