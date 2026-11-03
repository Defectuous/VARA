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
uv run uvicorn vara.main:app --host 127.0.0.1 --port 8000
```

The health endpoint is available at `http://127.0.0.1:8000/health`.
Keep the service bound to localhost until device authentication is implemented.