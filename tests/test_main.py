import pytest
from fastapi.testclient import TestClient

from vara.config import Environment, Settings
from vara.main import create_app


@pytest.mark.parametrize("path", ["/health", "/v1/health"])
def test_health_endpoints_return_ok(path: str) -> None:
    response = TestClient(create_app(Settings())).get(path)

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_docs_are_disabled_in_production() -> None:
    client = TestClient(create_app(Settings(environment=Environment.PRODUCTION)))

    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_docs_are_available_in_development() -> None:
    client = TestClient(create_app(Settings(environment=Environment.DEVELOPMENT)))

    assert client.get("/docs").status_code == 200
