import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

import ux_swarm.config as cfg
from ux_swarm.config import (
    PROVIDERS,
    ProviderAuthError,
    fetch_provider_models,
    load_config,
    save_config,
)


def test_load_config_no_files(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", tmp_path / "local.json")
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", tmp_path / "global.json")
    assert load_config() == {}


def test_load_config_local_only(monkeypatch, tmp_path):
    local = tmp_path / "local.json"
    local.write_text(json.dumps({"model": "gpt-4o"}))
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", local)
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", tmp_path / "global.json")
    assert load_config() == {"model": "gpt-4o"}


def test_load_config_global_only(monkeypatch, tmp_path):
    global_ = tmp_path / "global.json"
    global_.write_text(json.dumps({"provider": "openai"}))
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", tmp_path / "local.json")
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", global_)
    assert load_config() == {"provider": "openai"}


def test_load_config_local_overrides_global(monkeypatch, tmp_path):
    local = tmp_path / "local.json"
    global_ = tmp_path / "global.json"
    global_.write_text(json.dumps({"model": "gpt-4", "provider": "openai"}))
    local.write_text(json.dumps({"model": "gpt-4o"}))
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", local)
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", global_)
    result = load_config()
    assert result["model"] == "gpt-4o"
    assert result["provider"] == "openai"


def test_load_config_corrupt_local_raises(monkeypatch, tmp_path):
    local = tmp_path / "local.json"
    local.write_text("not json {{{")
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", local)
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", tmp_path / "global.json")
    with pytest.raises(ValueError, match="Fix or delete"):
        load_config()


def test_load_config_corrupt_global_raises(monkeypatch, tmp_path):
    global_ = tmp_path / "global.json"
    global_.write_text("{bad}")
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", tmp_path / "local.json")
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", global_)
    with pytest.raises(ValueError, match="Fix or delete"):
        load_config()


def test_save_config_writes_correct_content(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", tmp_path / "config.json")
    data = {"provider": "anthropic", "api_key": "sk-test", "model": "claude-3"}
    path = save_config(data)
    assert json.loads(path.read_text()) == data


def test_save_config_creates_parent_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", tmp_path / "nested" / "dir" / "config.json")
    path = save_config({"model": "x"})
    assert path.exists()


def test_save_config_trailing_newline(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", tmp_path / "config.json")
    path = save_config({"x": 1})
    assert path.read_text().endswith("\n")


def test_save_config_returns_local_path_by_default(monkeypatch, tmp_path):
    local = tmp_path / "config.json"
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", local)
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", tmp_path / "global.json")
    assert save_config({"x": 1}) == local


def test_save_config_global(monkeypatch, tmp_path):
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", tmp_path / "local.json")
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", tmp_path / "global.json")
    path = save_config({"x": 1}, local=False)
    assert path == tmp_path / "global.json"
    assert path.exists()


def _mock_urlopen(response_data: dict) -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(response_data).encode()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    return mock_resp


def test_fetch_provider_models_401_raises_auth_error():
    exc = urllib.error.HTTPError(url="", code=401, msg="Unauthorized", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        with pytest.raises(ProviderAuthError):
            fetch_provider_models("openai", "bad-key")


def test_fetch_provider_models_403_raises_auth_error():
    exc = urllib.error.HTTPError(url="", code=403, msg="Forbidden", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        with pytest.raises(ProviderAuthError):
            fetch_provider_models("anthropic", "bad-key")


def test_fetch_provider_models_500_reraises():
    exc = urllib.error.HTTPError(url="", code=500, msg="Server Error", hdrs=None, fp=None)
    with patch("urllib.request.urlopen", side_effect=exc):
        with pytest.raises(urllib.error.HTTPError):
            fetch_provider_models("openai", "key")


def test_fetch_provider_models_openai_filters_non_chat():
    data = {
        "data": [
            {"id": "gpt-4o"},
            {"id": "gpt-4"},
            {"id": "text-embedding-ada-002"},
            {"id": "whisper-1"},
            {"id": "ft:gpt-4:custom"},
            {"id": "o1-preview"},
        ]
    }
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(data)):
        models = fetch_provider_models("openai", "sk-test")
    assert "openai/gpt-4o" in models
    assert "openai/gpt-4" in models
    assert "openai/o1-preview" in models
    assert not any("embedding" in m for m in models)
    assert not any("whisper" in m for m in models)
    assert not any("ft:" in m for m in models)


def test_fetch_provider_models_openai_prefixes():
    data = {"data": [{"id": "gpt-4o"}]}
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(data)):
        models = fetch_provider_models("openai", "sk-test")
    assert all(m.startswith("openai/") for m in models)


def test_fetch_provider_models_anthropic_prefixes():
    data = {"data": [{"id": "claude-3-5-sonnet-20241022"}, {"id": "claude-3-haiku-20240307"}]}
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(data)):
        models = fetch_provider_models("anthropic", "sk-ant-test")
    assert all(m.startswith("anthropic/") for m in models)
    assert len(models) == 2


def test_fetch_provider_models_gemini_filters_non_gemini():
    data = {
        "data": [
            {"id": "gemini-1.5-pro"},
            {"id": "gemini-embedding-exp"},
            {"id": "text-bison"},
        ]
    }
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(data)):
        models = fetch_provider_models("gemini", "key")
    assert "gemini/gemini-1.5-pro" in models
    assert not any("embedding" in m for m in models)
    assert not any("text-bison" in m for m in models)


def test_fetch_provider_models_unknown_provider_raises():
    with pytest.raises(ValueError, match="Unknown provider"):
        fetch_provider_models("deepseek", "key")


def test_providers_have_required_keys():
    for p in PROVIDERS:
        assert "name" in p
        assert "key" in p
        assert "env" in p


def test_provider_env_var_mapping():
    env_map = {p["key"]: p["env"] for p in PROVIDERS}
    assert env_map["anthropic"] == "ANTHROPIC_API_KEY"
    assert env_map["openai"] == "OPENAI_API_KEY"
    assert env_map["gemini"] == "GEMINI_API_KEY"
