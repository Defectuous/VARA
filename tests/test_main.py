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


def test_root_serves_the_chat_page() -> None:
    response = TestClient(create_app(Settings())).get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "/v1/chat/stream" in response.text


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/favicon.ico", "image/"),
        ("/favicon.svg", "image/svg+xml"),
        ("/apple-touch-icon.png", "image/png"),
    ],
)
def test_chat_page_icons_are_served(path: str, content_type: str) -> None:
    response = TestClient(create_app(Settings())).get(path)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert len(response.content) > 100
