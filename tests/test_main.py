import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

import ux_swarm.main as main_mod
import ux_swarm.personas as personas_mod
from ux_swarm.main import cli
from ux_swarm.models import AgentResult, SwarmResult


# --- fixtures ---

@pytest.fixture
def runner():
    return CliRunner()


def _make_swarm_result(target="shot.png", task="find nav", mode="screenshot"):
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
        mode=mode,
        target=target,
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


_VALID_CONFIG = {"model": "anthropic/claude-3", "api_key": "sk-ant-test", "provider": "anthropic"}


# --- swarm run: error paths ---

def test_run_missing_model(runner):
    with patch("ux_swarm.main.load_config", return_value={"api_key": "key"}):
        result = runner.invoke(cli, ["run", "https://example.com", "find nav"])
    assert result.exit_code != 0
    assert "No model configured" in result.output


def test_run_missing_api_key(runner):
    with patch("ux_swarm.main.load_config", return_value={"model": "anthropic/claude-3"}):
        result = runner.invoke(cli, ["run", "https://example.com", "find nav"])
    assert result.exit_code != 0
    assert "No API key" in result.output


def test_run_image_not_found(runner, tmp_path):
    # Use an absolute path so it can't be confused with a bare domain by the URL regex
    missing = str(tmp_path / "nonexistent.png")
    with patch("ux_swarm.main.load_config", return_value=_VALID_CONFIG):
        result = runner.invoke(cli, ["run", missing, "find nav"])
    assert result.exit_code != 0
    assert "Image not found" in result.output


# --- swarm run: dispatch ---

def test_run_dispatches_to_screenshot(runner, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"")

    with patch("ux_swarm.main.load_config", return_value=_VALID_CONFIG):
        with patch("ux_swarm.main._run_screenshot") as mock_run:
            runner.invoke(cli, ["run", str(img), "find nav"])

    mock_run.assert_called_once()
    args = mock_run.call_args
    assert args[0][0] == str(img)  # target
    assert args[0][1] == "find nav"  # task


def test_run_dispatches_to_browser(runner):
    with patch("ux_swarm.main.load_config", return_value=_VALID_CONFIG):
        with patch("ux_swarm.main._run_browser") as mock_run:
            runner.invoke(cli, ["run", "https://example.com", "find nav"])

    mock_run.assert_called_once()


def test_run_screenshot_users_flag(runner, tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"")

    with patch("ux_swarm.main.load_config", return_value=_VALID_CONFIG):
        with patch("ux_swarm.main._run_screenshot") as mock_run:
            runner.invoke(cli, ["run", str(img), "find nav", "--users", "3"])

    assert mock_run.call_args[0][2] == 3  # users arg


# --- swarm users ---

def test_users_lists_default_personas(runner, tmp_path):
    with patch("ux_swarm.personas.USERS_JSON", tmp_path / "users.json"):
        result = runner.invoke(cli, ["users"])
    assert result.exit_code == 0
    assert "Default User" in result.output
    assert "Weight" in result.output


def test_users_edit_writes_file(runner, tmp_path):
    users_json = tmp_path / "users.json"
    with patch("ux_swarm.personas.USERS_JSON", users_json):
        with patch("ux_swarm.personas.LOCAL_DIR", tmp_path):
            result = runner.invoke(cli, ["users", "--edit"])
    assert result.exit_code == 0
    assert users_json.exists()


def test_users_edit_existing_file_reports_already_exists(runner, tmp_path):
    users_json = tmp_path / "users.json"
    users_json.write_text("[]")
    with patch("ux_swarm.personas.USERS_JSON", users_json):
        result = runner.invoke(cli, ["users", "--edit"])
    assert "already exists" in result.output


# --- swarm results ---

def test_results_no_file(runner, tmp_path):
    with patch("ux_swarm.main.RESULTS_JSON", tmp_path / "results.json"):
        result = runner.invoke(cli, ["results"])
    assert result.exit_code == 0
    assert "No results yet" in result.output


def test_results_shows_entries(runner, tmp_path):
    results_json = tmp_path / "results.json"
    results_json.write_text(json.dumps([_make_swarm_result().model_dump()]) + "\n")
    with patch("ux_swarm.main.RESULTS_JSON", results_json):
        result = runner.invoke(cli, ["results"])
    assert result.exit_code == 0
    assert "shot.png" in result.output
    assert "100%" in result.output


def test_results_number_flag_limits_output(runner, tmp_path):
    results_json = tmp_path / "results.json"
    entries = [_make_swarm_result(target=f"shot{i}.png").model_dump() for i in range(5)]
    results_json.write_text(json.dumps(entries) + "\n")
    with patch("ux_swarm.main.RESULTS_JSON", results_json):
        result = runner.invoke(cli, ["results", "--number", "2"])
    assert "shot4.png" in result.output
    assert "shot0.png" not in result.output


def test_results_empty_json_array(runner, tmp_path):
    results_json = tmp_path / "results.json"
    results_json.write_text("[]")
    with patch("ux_swarm.main.RESULTS_JSON", results_json):
        result = runner.invoke(cli, ["results"])
    assert result.exit_code == 0
    assert "No results yet" in result.output


def test_results_corrupt_file_raises_cli_error(runner, tmp_path):
    results_json = tmp_path / "results.json"
    results_json.write_text("{bad json{{")
    with patch("ux_swarm.main.RESULTS_JSON", results_json):
        result = runner.invoke(cli, ["results"])
    assert result.exit_code != 0


# --- swarm expand ---

def test_expand_no_file(runner, tmp_path):
    with patch("ux_swarm.main.RESULTS_JSON", tmp_path / "results.json"):
        result = runner.invoke(cli, ["expand"])
    assert result.exit_code == 0
    assert "No results yet" in result.output


def test_expand_renders_agent_breakdown(runner, tmp_path):
    results_json = tmp_path / "results.json"
    results_json.write_text(json.dumps([_make_swarm_result().model_dump()]) + "\n")
    with patch("ux_swarm.main.RESULTS_JSON", results_json):
        with patch("ux_swarm.personas.USERS_JSON", tmp_path / "users.json"):
            result = runner.invoke(cli, ["expand"])
    assert result.exit_code == 0
    assert "Default User" in result.output


def test_expand_empty_json_array(runner, tmp_path):
    results_json = tmp_path / "results.json"
    results_json.write_text("[]")
    with patch("ux_swarm.main.RESULTS_JSON", results_json):
        result = runner.invoke(cli, ["expand"])
    assert result.exit_code == 0
    assert "No results yet" in result.output


def test_expand_shows_friction_points(runner, tmp_path):
    results_json = tmp_path / "results.json"
    agent = AgentResult(
        agent_index=0,
        user_type="Default User",
        status="completed",
        abandonment_reason=None,
        friction_points=["confusing nav", "unclear CTA"],
        comment="done",
        steps_taken=1,
        input_tokens=10,
        output_tokens=5,
        cost=0.0,
    )
    swarm = SwarmResult(
        timestamp="2025-01-01T00:00:00+00:00",
        mode="screenshot",
        target="shot.png",
        task="find nav",
        model="claude-3",
        users=1,
        completion_rate=1.0,
        margin_of_error=0.0,
        user_breakdown={"Default User": 1.0},
        friction_points=["confusing nav", "unclear CTA"],
        total_cost=0.0,
        individual_results=[agent],
    )
    results_json.write_text(json.dumps([swarm.model_dump()]) + "\n")
    with patch("ux_swarm.main.RESULTS_JSON", results_json):
        with patch("ux_swarm.personas.USERS_JSON", tmp_path / "users.json"):
            result = runner.invoke(cli, ["expand"])
    assert "Pain points" in result.output
    assert "confusing nav" in result.output


def test_expand_corrupt_file(runner, tmp_path):
    results_json = tmp_path / "results.json"
    results_json.write_text("{bad}")
    with patch("ux_swarm.main.RESULTS_JSON", results_json):
        result = runner.invoke(cli, ["expand"])
    assert result.exit_code != 0


# --- swarm help ---

def test_help_command_valid_subcommand(runner):
    result = runner.invoke(cli, ["help", "run"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_help_command_unknown_subcommand(runner):
    result = runner.invoke(cli, ["help", "notacommand"])
    assert result.exit_code != 0
    assert "No such command" in result.output


def test_help_flag(runner):
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_run_help_flag(runner):
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "target" in result.output
    assert "task" in result.output
