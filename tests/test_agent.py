import asyncio
import base64
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import json

from ux_swarm.agents import _build_system_prompt, _load_image, _media_type, _call_with_retry, run_screenshot_agent
from ux_swarm.errors import CliError
from ux_swarm.models import ScreenshotDecision, UserType


_USER = UserType(label="Default User", weight=1.0, description="Scans quickly, satisfices.")


def test_media_type_png(tmp_path):
    assert _media_type(tmp_path / "shot.png") == "image/png"


def test_media_type_jpg(tmp_path):
    assert _media_type(tmp_path / "shot.jpg") == "image/jpeg"


def test_media_type_jpeg(tmp_path):
    assert _media_type(tmp_path / "shot.jpeg") == "image/jpeg"


def test_media_type_webp(tmp_path):
    assert _media_type(tmp_path / "shot.webp") == "image/webp"


def test_media_type_gif(tmp_path):
    assert _media_type(tmp_path / "shot.gif") == "image/gif"


def test_media_type_unknown_defaults_to_png(tmp_path):
    assert _media_type(tmp_path / "shot.bmp") == "image/png"


def test_media_type_case_insensitive(tmp_path):
    assert _media_type(tmp_path / "shot.PNG") == "image/png"
    assert _media_type(tmp_path / "shot.JPG") == "image/jpeg"


def test_load_image_returns_base64_and_mime(tmp_path):
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")
    data, mime = _load_image(str(img))
    assert mime == "image/png"
    assert base64.standard_b64decode(data) == b"\x89PNG\r\n\x1a\n"


def test_load_image_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        _load_image(str(tmp_path / "nonexistent.png"))


def test_build_system_prompt():
    prompt = _build_system_prompt(_USER)
    assert "Default User" in prompt
    assert "Scans quickly, satisfices." in prompt
    assert "friction_observed" in prompt
    assert "completed" in prompt
    assert "abandoned" in prompt
    assert "Return ONLY valid JSON" in prompt


class _FakeRateLimitError(Exception):
    pass


@pytest.mark.anyio
async def test_call_with_retry_success():
    mock_response = MagicMock()
    with patch("ux_swarm.agents.acompletion", new=AsyncMock(return_value=mock_response)):
        result = await _call_with_retry("anthropic/claude-3", [])
    assert result is mock_response


@pytest.mark.anyio
async def test_call_with_retry_retries_on_rate_limit():
    mock_response = MagicMock()
    call_count = 0

    async def _side_effect(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise _FakeRateLimitError()
        return mock_response

    with patch("ux_swarm.agents.RateLimitError", _FakeRateLimitError):
        with patch("ux_swarm.agents.acompletion", side_effect=_side_effect):
            with patch("asyncio.sleep", new=AsyncMock()):
                result = await _call_with_retry("model", [])

    assert result is mock_response
    assert call_count == 3


@pytest.mark.anyio
async def test_call_with_retry_exhausted_raises_cli_error():
    async def _always_rate_limit(**kwargs):
        raise _FakeRateLimitError()

    with patch("ux_swarm.agents.RateLimitError", _FakeRateLimitError):
        with patch("ux_swarm.agents.acompletion", side_effect=_always_rate_limit):
            with patch("asyncio.sleep", new=AsyncMock()):
                with pytest.raises(CliError, match="Rate limited"):
                    await _call_with_retry("model", [])


_VALID_DECISION = {
    "target_element": "Sign Up button",
    "reasoning": "The button is prominent",
    "comment": "I clicked the sign up button",
    "friction_observed": [],
    "completed": True,
    "abandoned": False,
    "abandonment_reason": None,
}


def _mock_response(content: str, in_tok: int = 100, out_tok: int = 50) -> MagicMock:
    r = MagicMock()
    r.choices[0].message.content = content
    r.usage.prompt_tokens = in_tok
    r.usage.completion_tokens = out_tok
    return r


@pytest.mark.anyio
async def test_run_screenshot_agent_valid_response(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    with patch("ux_swarm.agents._call_with_retry", new=AsyncMock(return_value=_mock_response(json.dumps(_VALID_DECISION)))):
        with patch("ux_swarm.agents.completion_cost", return_value=0.01):
            decision, in_tok, out_tok, cost = await run_screenshot_agent(str(img), "find sign up", _USER, "model")

    assert decision.completed is True
    assert decision.target_element == "Sign Up button"
    assert in_tok == 100
    assert out_tok == 50
    assert cost == 0.01


@pytest.mark.anyio
async def test_run_screenshot_agent_invalid_json_falls_back(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    with patch("ux_swarm.agents._call_with_retry", new=AsyncMock(return_value=_mock_response("not valid json at all"))):
        with patch("ux_swarm.agents.completion_cost", return_value=0.0):
            decision, _, _, _ = await run_screenshot_agent(str(img), "task", _USER, "model")

    assert decision.abandoned is True
    assert decision.abandonment_reason == "parse failure"
    assert "not valid JSON" in decision.comment


@pytest.mark.anyio
async def test_run_screenshot_agent_missing_image_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        await run_screenshot_agent(str(tmp_path / "ghost.png"), "task", _USER, "model")


@pytest.mark.anyio
async def test_run_screenshot_agent_cost_exception_defaults_to_zero(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n")

    with patch("ux_swarm.agents._call_with_retry", new=AsyncMock(return_value=_mock_response(json.dumps(_VALID_DECISION)))):
        with patch("ux_swarm.agents.completion_cost", side_effect=Exception("unsupported model")):
            _, _, _, cost = await run_screenshot_agent(str(img), "task", _USER, "model")

    assert cost == 0.0
