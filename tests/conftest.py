import pytest

from vara.config import get_settings


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own empty database; nothing touches the real data folder. Web search is
    off so no test reaches the internet; tests/test_search.py turns it on with a fake search."""
    monkeypatch.setenv("VARA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VARA_WEB_SEARCH", "off")
    monkeypatch.setenv("VARA_MODEL_UPDATES", "off")
    monkeypatch.setenv("VARA_VOICE_WARM_UP", "false")
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()
