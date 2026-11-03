from fastapi import FastAPI

app = FastAPI(title="V.A.R.A. API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}