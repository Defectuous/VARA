import pytest

from vara.config import get_settings


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Every test gets its own empty database; nothing touches the real data folder."""
    monkeypatch.setenv("VARA_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    yield tmp_path / "data"
    get_settings.cache_clear()
