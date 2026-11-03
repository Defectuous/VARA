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

Logs are JSON lines, with API keys, bearer tokens, and password/secret fields redacted.

API keys are not configuration. Store them in Windows Credential Manager under the service `vara`:

```powershell
uv run --with keyring python -c "import keyring, getpass; keyring.set_password('vara', 'anthropic', getpass.getpass('Key: '))"
```

Keep the service bound to localhost until device authentication is implemented.
