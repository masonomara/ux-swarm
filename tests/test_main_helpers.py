import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import ux_swarm.cli as main_mod
from ux_swarm.cli import _inject_api_key, _resolve_target, _save_result
from ux_swarm.display import _make_agent_labels
from ux_swarm.models import AgentResult, SwarmResult, UserType


# --- _resolve_target ---

def test_resolve_target_https_passthrough():
    is_url, target = _resolve_target("https://example.com")
    assert is_url is True
    assert target == "https://example.com"


def test_resolve_target_http_passthrough():
    is_url, target = _resolve_target("http://localhost:3000")
    assert is_url is True
    assert target == "http://localhost:3000"


def test_resolve_target_bare_domain_prepends_https():
    is_url, target = _resolve_target("example.com")
    assert is_url is True
    assert target == "https://example.com"


def test_resolve_target_localhost_with_port():
    is_url, target = _resolve_target("localhost:3000")
    assert is_url is True
    assert target == "https://localhost:3000"


def test_resolve_target_existing_file(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"")
    is_url, target = _resolve_target(str(img))
    assert is_url is False
    assert target == str(img)


def test_resolve_target_missing_file_not_url():
    is_url, target = _resolve_target("nonexistent_file.png")
    assert is_url is False
    assert target == "nonexistent_file.png"


def test_resolve_target_bare_domain_with_path():
    is_url, target = _resolve_target("example.com/path/to/page")
    assert is_url is True
    assert target == "https://example.com/path/to/page"


# --- _inject_api_key ---

def test_inject_api_key_anthropic():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        _inject_api_key("anthropic", "sk-ant-test123")
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-test123"


def test_inject_api_key_openai():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OPENAI_API_KEY", None)
        _inject_api_key("openai", "sk-openai-test")
        assert os.environ["OPENAI_API_KEY"] == "sk-openai-test"


def test_inject_api_key_gemini():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("GEMINI_API_KEY", None)
        _inject_api_key("gemini", "gemini-key-test")
        assert os.environ["GEMINI_API_KEY"] == "gemini-key-test"


def test_inject_api_key_unknown_provider_does_not_raise():
    with patch.dict(os.environ, {}, clear=False):
        _inject_api_key("unknown_provider", "some-key")


def test_inject_api_key_empty_key_does_not_set():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        _inject_api_key("anthropic", "")
        assert "ANTHROPIC_API_KEY" not in os.environ


# --- _make_agent_labels ---

def _user(label="Default User", accessibility=None):
    return UserType(label=label, weight=1.0, description="test", accessibility=accessibility)


def test_make_agent_labels_length():
    users = [_user()]
    labels = _make_agent_labels(users, 5)
    assert len(labels) == 5


def test_make_agent_labels_sequential_numbering():
    users = [_user("Default User")]
    labels = _make_agent_labels(users, 3)
    values = list(labels.values())
    assert "Default User 1" in values
    assert "Default User 2" in values
    assert "Default User 3" in values


def test_make_agent_labels_keys_are_agent_indices():
    users = [_user()]
    labels = _make_agent_labels(users, 3)
    assert set(labels.keys()) == {0, 1, 2}


def test_make_agent_labels_a11y_tag():
    users = [_user(accessibility="screen_reader")]
    labels = _make_agent_labels(users, 1)
    assert "[blue]a11y[/]" in labels[0]


def test_make_agent_labels_no_a11y_tag_without_accessibility():
    users = [_user()]
    labels = _make_agent_labels(users, 1)
    assert "a11y" not in labels[0]


def test_make_agent_labels_multiple_types_numbered_independently():
    users = [_user("Power User"), _user("Casual User")]
    labels = _make_agent_labels(users, 4)
    values = list(labels.values())
    assert any("Power User 1" in v for v in values)
    assert any("Casual User 1" in v for v in values)


# --- _save_result ---

def _swarm_result(task: str = "find the nav") -> SwarmResult:
    agent = AgentResult(
        agent_index=0,
        user_type="Default User",
        status="completed",
        abandonment_reason=None,
        friction_points=[],
        comment="done",
        steps_taken=1,
        input_tokens=10,
        output_tokens=5,
        cost=0.0,
    )
    return SwarmResult(
        timestamp="2025-01-01T00:00:00+00:00",
        mode="screenshot",
        target="shot.png",
        task=task,
        model="claude-3",
        users=1,
        completion_rate=1.0,
        margin_of_error=0.0,
        user_breakdown={"Default User": 1.0},
        friction_points=[],
        total_cost=0.0,
        individual_results=[agent],
    )


def test_save_result_creates_file(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main_mod, "RESULTS_JSON", tmp_path / "results.json")
    _save_result(_swarm_result())
    assert (tmp_path / "results.json").exists()


def test_save_result_valid_json(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "LOCAL_DIR", tmp_path)
    results_json = tmp_path / "results.json"
    monkeypatch.setattr(main_mod, "RESULTS_JSON", results_json)
    _save_result(_swarm_result(task="find the nav"))
    data = json.loads(results_json.read_text())
    assert isinstance(data, list)
    assert data[0]["task"] == "find the nav"


def test_save_result_appends_to_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "LOCAL_DIR", tmp_path)
    results_json = tmp_path / "results.json"
    monkeypatch.setattr(main_mod, "RESULTS_JSON", results_json)
    _save_result(_swarm_result())
    _save_result(_swarm_result())
    data = json.loads(results_json.read_text())
    assert len(data) == 2


def test_save_result_trailing_newline(monkeypatch, tmp_path):
    monkeypatch.setattr(main_mod, "LOCAL_DIR", tmp_path)
    results_json = tmp_path / "results.json"
    monkeypatch.setattr(main_mod, "RESULTS_JSON", results_json)
    _save_result(_swarm_result())
    assert results_json.read_text().endswith("\n")
