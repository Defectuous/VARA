import pytest

from vara.config import Environment, Settings


def test_defaults_bind_to_localhost_in_development(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("VARA_ENVIRONMENT", "VARA_HOST", "VARA_PORT", "VARA_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)

    settings = Settings(_env_file=None)

    assert settings.host == "127.0.0.1"
    assert settings.port == 8000
    assert settings.environment is Environment.DEVELOPMENT
    assert not settings.is_production


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VARA_ENVIRONMENT", "production")
    monkeypatch.setenv("VARA_PORT", "9000")

    settings = Settings(_env_file=None)

    assert settings.is_production
    assert settings.port == 9000
