import json
import math
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ux_swarm.cli import CliError
from ux_swarm.models import AgentResult, ScreenshotDecision, UserType
from ux_swarm.swarm import _aggregate, _consolidate_friction_points, run_browser_swarm, run_screenshot_swarm


def _result(
    agent_index: int = 0,
    user_type: str = "Default User",
    status: Literal["completed", "abandoned", "timeout"] = "completed",
    friction_points: list[str] | None = None,
    steps_taken: int = 3,
    cost: float = 0.0,
):
    return AgentResult(
        agent_index=agent_index,
        user_type=user_type,
        status=status,
        abandonment_reason=None,
        friction_points=friction_points or [],
        comment="",
        steps_taken=steps_taken,
        input_tokens=100,
        output_tokens=50,
        cost=cost,
    )


def test_aggregate_empty_results_raises_screenshot_hint():
    with pytest.raises(CliError, match="API key"):
        _aggregate([], "target.png", "task", "model", 5, "screenshot")


def test_aggregate_empty_results_raises_browser_hint():
    with pytest.raises(CliError, match="Chromium"):
        _aggregate([], "https://example.com", "task", "model", 5, "browser")


def test_aggregate_completion_rate():
    results = [
        _result(0, status="completed"),
        _result(1, status="completed"),
        _result(2, status="abandoned"),
        _result(3, status="abandoned"),
    ]
    swarm = _aggregate(results, "t", "task", "model", 4, "screenshot")
    assert swarm.completion_rate == 0.5


def test_aggregate_all_completed_rate():
    results = [_result(i) for i in range(10)]
    swarm = _aggregate(results, "t", "task", "model", 10, "screenshot")
    assert swarm.completion_rate == 1.0


def test_aggregate_margin_of_error():
    results = [_result(i, status="completed") for i in range(80)] + \
              [_result(i + 80, status="abandoned") for i in range(20)]
    swarm = _aggregate(results, "t", "task", "model", 100, "screenshot")
    expected_moe = 1.96 * math.sqrt(0.8 * 0.2 / 100)
    assert abs(swarm.margin_of_error - expected_moe) < 1e-10


def test_aggregate_moe_zero_for_single_result():
    swarm = _aggregate([_result()], "t", "task", "model", 1, "screenshot")
    assert swarm.margin_of_error == 0.0


def test_aggregate_user_breakdown():
    results = [
        _result(0, user_type="Power User", status="completed"),
        _result(1, user_type="Power User", status="completed"),
        _result(2, user_type="Casual User", status="abandoned"),
        _result(3, user_type="Casual User", status="abandoned"),
    ]
    swarm = _aggregate(results, "t", "task", "model", 4, "screenshot")
    assert swarm.user_breakdown["Power User"] == 1.0
    assert swarm.user_breakdown["Casual User"] == 0.0


def test_aggregate_model_id_strips_prefix():
    swarm = _aggregate([_result()], "t", "task", "anthropic/claude-3-sonnet", 1, "screenshot")
    assert swarm.model == "claude-3-sonnet"


def test_aggregate_model_id_no_slash_unchanged():
    swarm = _aggregate([_result()], "t", "task", "gpt-4o", 1, "screenshot")
    assert swarm.model == "gpt-4o"


def test_aggregate_extra_cost_added():
    results = [_result(cost=0.05), _result(1, cost=0.03)]
    swarm = _aggregate(results, "t", "task", "model", 2, "screenshot", extra_cost=0.02)
    assert abs(swarm.total_cost - 0.10) < 1e-10


def test_aggregate_avg_steps_browser_completed_only():
    results = [
        _result(0, status="completed", steps_taken=4),
        _result(1, status="completed", steps_taken=6),
        _result(2, status="abandoned", steps_taken=8),
    ]
    swarm = _aggregate(results, "t", "task", "model", 3, "browser")
    assert swarm.avg_steps_to_completion == 5.0


def test_aggregate_avg_steps_zero_when_none_completed():
    results = [_result(status="abandoned")]
    swarm = _aggregate(results, "t", "task", "model", 1, "browser")
    assert swarm.avg_steps_to_completion == 0.0


def test_aggregate_avg_steps_zero_in_screenshot_mode():
    results = [_result(steps_taken=1)]
    swarm = _aggregate(results, "t", "task", "model", 1, "screenshot")
    assert swarm.avg_steps_to_completion == 0.0


def test_aggregate_friction_passed_through():
    results = [_result(friction_points=["confusing nav", "unclear CTA"])]
    swarm = _aggregate(results, "t", "task", "model", 1, "screenshot",
                       friction_points=["confusing nav", "unclear CTA"])
    assert "confusing nav" in swarm.friction_points


def test_aggregate_users_field_is_result_count():
    results = [_result(i) for i in range(7)]
    swarm = _aggregate(results, "t", "task", "model", 10, "screenshot")
    assert swarm.users == 7


@pytest.mark.anyio
async def test_consolidate_friction_empty_returns_immediately():
    result, cost = await _consolidate_friction_points([], "model")
    assert result == []
    assert cost == 0.0


@pytest.mark.anyio
async def test_consolidate_friction_success():
    raw = ["Nav is confusing", "Confusing navigation", "CTA unclear"]
    canonical = ["Confusing navigation", "Confusing navigation", "CTA unclear"]
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({"canonical": canonical})

    with patch("ux_swarm.swarm.acompletion", new=AsyncMock(return_value=mock_response)):
        with patch("ux_swarm.swarm.completion_cost", return_value=0.01):
            result, cost = await _consolidate_friction_points(raw, "model")

    assert result == canonical
    assert cost == 0.01


@pytest.mark.anyio
async def test_consolidate_friction_wrong_length_falls_back():
    raw = ["a", "b", "c"]
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({"canonical": ["a", "b"]})

    with patch("ux_swarm.swarm.acompletion", new=AsyncMock(return_value=mock_response)):
        result, cost = await _consolidate_friction_points(raw, "model")

    assert result == raw
    assert cost == 0.0


@pytest.mark.anyio
async def test_consolidate_friction_cost_exception_defaults_to_zero():
    raw = ["confusing nav"]
    canonical = ["confusing nav"]
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps({"canonical": canonical})

    with patch("ux_swarm.swarm.acompletion", new=AsyncMock(return_value=mock_response)):
        with patch("ux_swarm.swarm.completion_cost", side_effect=Exception("unsupported model")):
            result, cost = await _consolidate_friction_points(raw, "model")

    assert result == canonical
    assert cost == 0.0


@pytest.mark.anyio
async def test_consolidate_friction_exception_falls_back():
    raw = ["friction point"]
    with patch("ux_swarm.swarm.acompletion", side_effect=Exception("network error")):
        result, cost = await _consolidate_friction_points(raw, "model")

    assert result == raw
    assert cost == 0.0


_COMPLETED_DECISION = ScreenshotDecision(
    target_element="button",
    reasoning="clear",
    comment="found it",
    friction_observed=["minor confusion"],
    completed=True,
    abandoned=False,
    abandonment_reason=None,
)


@pytest.mark.anyio
async def test_run_screenshot_swarm_aggregates_results():
    async def _mock_agent(target, task, user_type, model):
        return _COMPLETED_DECISION, 100, 50, 0.01

    users = [UserType(label="Default User", weight=1.0, description="test")]

    with patch("ux_swarm.swarm.run_screenshot_agent", side_effect=_mock_agent):
        with patch("ux_swarm.swarm._consolidate_friction_points", new=AsyncMock(return_value=(["minor confusion"], 0.0))):
            result = await run_screenshot_swarm(
                target="shot.png",
                task="find nav",
                users=users,
                num_agents=3,
                model="model",
                max_concurrent=5,
            )

    assert result.completion_rate == 1.0
    assert result.users == 3
    assert result.mode == "screenshot"
    assert "minor confusion" in result.friction_points


@pytest.mark.anyio
async def test_run_screenshot_swarm_on_agent_done_callback():
    async def _mock_agent(target, task, user_type, model):
        return _COMPLETED_DECISION, 10, 5, 0.0

    users = [UserType(label="Default User", weight=1.0, description="test")]
    done_calls: list[tuple[int, int]] = []

    def on_done(done, total, result):
        done_calls.append((done, total))

    with patch("ux_swarm.swarm.run_screenshot_agent", side_effect=_mock_agent):
        with patch("ux_swarm.swarm._consolidate_friction_points", new=AsyncMock(return_value=([], 0.0))):
            await run_screenshot_swarm(
                target="shot.png",
                task="task",
                users=users,
                num_agents=4,
                model="model",
                max_concurrent=5,
                on_agent_done=on_done,
            )

    assert len(done_calls) == 4
    totals = [t for _, t in done_calls]
    assert all(t == 4 for t in totals)
    dones = sorted(d for d, _ in done_calls)
    assert dones == [1, 2, 3, 4]


@pytest.mark.anyio
async def test_run_screenshot_swarm_agent_exception_dropped():
    """When an agent throws, it is silently dropped; _aggregate raises CliError if all fail."""
    users = [UserType(label="Default User", weight=1.0, description="test")]

    async def _failing_agent(target, task, user_type, model):
        raise Exception("network failure")

    with patch("ux_swarm.swarm.run_screenshot_agent", side_effect=_failing_agent):
        with patch("ux_swarm.swarm._consolidate_friction_points", new=AsyncMock(return_value=([], 0.0))):
            with pytest.raises(CliError, match="All 1 agents failed"):
                await run_screenshot_swarm(
                    target="shot.png",
                    task="find nav",
                    users=users,
                    num_agents=1,
                    model="model",
                    max_concurrent=5,
                )


@pytest.mark.anyio
async def test_run_screenshot_swarm_on_done_called_with_none_on_agent_failure():
    """on_agent_done receives None as the result when an agent throws."""
    users = [UserType(label="Default User", weight=1.0, description="test")]
    done_calls: list[tuple[int, int, object]] = []

    async def _failing_agent(target, task, user_type, model):
        raise Exception("fail")

    def on_done(done, total, result):
        done_calls.append((done, total, result))

    with patch("ux_swarm.swarm.run_screenshot_agent", side_effect=_failing_agent):
        with patch("ux_swarm.swarm._consolidate_friction_points", new=AsyncMock(return_value=([], 0.0))):
            with pytest.raises(CliError):
                await run_screenshot_swarm(
                    target="shot.png",
                    task="task",
                    users=users,
                    num_agents=1,
                    model="model",
                    max_concurrent=5,
                    on_agent_done=on_done,
                )

    assert done_calls == [(1, 1, None)]


@pytest.mark.anyio
async def test_run_browser_swarm_aggregates_results():
    users = [UserType(label="Default User", weight=1.0, description="test")]

    mock_agent_result = AgentResult(
        agent_index=0,
        user_type="Default User",
        status="completed",
        abandonment_reason=None,
        friction_points=[],
        comment="done",
        steps_taken=3,
        input_tokens=100,
        output_tokens=50,
        cost=0.01,
    )

    async def _mock_browser_agent(browser, url, task, user_type, model,
                                   llm_semaphore, max_steps, agent_index,
                                   viewport=1280, on_step=None):
        return mock_agent_result, 100, 50, 0.01

    mock_browser = MagicMock()
    mock_browser.close = AsyncMock()
    mock_playwright = MagicMock()
    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

    mock_pw_cm = MagicMock()
    mock_pw_cm.__aenter__ = AsyncMock(return_value=mock_playwright)
    mock_pw_cm.__aexit__ = AsyncMock(return_value=False)

    with patch("ux_swarm.swarm.run_browser_agent", side_effect=_mock_browser_agent):
        with patch("ux_swarm.swarm.async_playwright", return_value=mock_pw_cm):
            with patch("ux_swarm.swarm._consolidate_friction_points", new=AsyncMock(return_value=([], 0.0))):
                result = await run_browser_swarm(
                    url="https://example.com",
                    task="find nav",
                    users=users,
                    num_agents=1,
                    model="model",
                    max_concurrent=5,
                    max_steps=8,
                )

    assert result.completion_rate == 1.0
    assert result.mode == "browser"
    assert result.users == 1
