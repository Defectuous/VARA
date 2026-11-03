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

Optional fields: `provider` (for example `"anthropic"`; a manual choice never falls back), `model`, `system`,
and `confirm_cloud` (required in `ask` mode). Without a provider, local models are tried before cloud ones.
The response names the provider and model that answered, with token usage and estimated cost
(`0.0` for local models, `null` for a cloud model without a known price).

Other chat endpoints take the same body:

| Endpoint | What it does |
| --- | --- |
| `POST /v1/chat/stream` | Server-sent events: `delta` per text chunk, then `done` (usage and cost) or `error`. Privacy and provider failures before the first chunk are normal HTTP errors. |
| `POST /v1/chat/preview` | Calls no model. Lists the providers that would be tried, in order, with the exact request each would receive. |
| `GET /v1/usage` | Requests, tokens, and cost per provider and model since the server started (not yet persisted). |

Keep the service bound to localhost until device authentication is implemented.
