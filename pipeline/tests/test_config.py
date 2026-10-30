import pytest

import config


def test_paths_point_inside_repo():
    assert config.WEB_DATA_DIR == config.REPO_ROOT / "web" / "data"
    assert config.PRICES_DIR.parent == config.WEB_DATA_DIR


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("TIINGO_API_KEY", raising=False)
    assert config.tiingo_api_key(required=False) is None
    with pytest.raises(RuntimeError):
        config.tiingo_api_key()


def test_key_is_read_from_env(monkeypatch):
    monkeypatch.setenv("TIINGO_API_KEY", "  abc123 ")
    assert config.tiingo_api_key() == "abc123"


def test_ci_run(monkeypatch):
    import config
    for k in ("GITHUB_RUN_ID", "GITHUB_REPOSITORY", "GITHUB_SERVER_URL", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(k, raising=False)
    assert config.ci_run() is None
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/tracker")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "schedule")
    assert config.ci_run() == {"url": "https://github.com/me/tracker/actions/runs/42", "event": "schedule"}
