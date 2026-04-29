import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ux_swarm.browser_agent import (
    ACTION_HISTORY_LIMIT,
    ELEMENT_CAP,
    _build_browser_user_prompt,
    _build_messages,
    _extract_interactive_elements,
    _follow_page_after_click,
    run_browser_agent,
)
from ux_swarm.models import UserType


# --- _build_browser_user_prompt ---

def test_prompt_contains_step_and_max():
    prompt = _build_browser_user_prompt(3, 8, "https://example.com", [], "")
    assert "Step 3/8" in prompt


def test_prompt_contains_current_url():
    prompt = _build_browser_user_prompt(1, 8, "https://example.com/login", [], "")
    assert "https://example.com/login" in prompt


def test_prompt_ends_with_question():
    prompt = _build_browser_user_prompt(1, 8, "https://example.com", [], "")
    assert prompt.strip().endswith("What do you do next?")


def test_prompt_includes_element_list():
    elements = "[0] BUTTON \"Sign Up\"\n[1] INPUT type=\"email\""
    prompt = _build_browser_user_prompt(1, 8, "https://example.com", [], elements)
    assert "Sign Up" in prompt
    assert "Interactive elements:" in prompt


def test_prompt_omits_element_section_when_empty():
    prompt = _build_browser_user_prompt(1, 8, "https://example.com", [], "")
    assert "Interactive elements:" not in prompt


def test_prompt_includes_recent_actions():
    actions = ["click: [0]", "type: hello"]
    prompt = _build_browser_user_prompt(3, 8, "https://example.com", actions, "")
    assert "Recent actions:" in prompt
    assert "click: [0]" in prompt


def test_prompt_limits_actions_to_history_limit():
    actions = [f"action_{i}" for i in range(ACTION_HISTORY_LIMIT + 3)]
    prompt = _build_browser_user_prompt(9, 10, "https://example.com", actions, "")
    # Tail of the list is present; head is not — confirms slice is from the end
    assert f"action_{ACTION_HISTORY_LIMIT + 2}" in prompt
    assert f"action_{ACTION_HISTORY_LIMIT - 1}" in prompt
    assert "action_0" not in prompt


def test_prompt_omits_recent_actions_when_none():
    prompt = _build_browser_user_prompt(1, 8, "https://example.com", [], "")
    assert "Recent actions:" not in prompt


# --- _build_messages ---

def test_messages_system_is_first():
    messages = _build_messages("sys", "user prompt", None)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "sys"


def test_messages_user_is_second():
    messages = _build_messages("sys", "user prompt", None)
    assert messages[1]["role"] == "user"


def test_messages_without_screenshot_has_one_content_item():
    messages = _build_messages("sys", "user prompt", None)
    content = messages[1]["content"]
    assert len(content) == 1
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "user prompt"


def test_messages_with_screenshot_has_two_content_items():
    messages = _build_messages("sys", "user prompt", "base64data==")
    content = messages[1]["content"]
    assert len(content) == 2
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"


def test_messages_screenshot_url_format():
    messages = _build_messages("sys", "prompt", "abc123")
    image_url = messages[1]["content"][1]["image_url"]["url"]
    assert image_url == "data:image/png;base64,abc123"


# --- helpers ---

_DEFAULT_USER = UserType(label="Default User", weight=1.0, description="Scans quickly.")
_SCREEN_READER_USER = UserType(label="SR User", weight=1.0, description="Uses screen reader.", accessibility="screen_reader")

_FAKE_ELEMENTS = [
    {"tag": "button", "role": "", "name": "Sign Up", "placeholder": "", "type_": "", "visible": True}
]


def _page_mock():
    """Minimal synchronous Page mock with async methods wired up."""
    page = MagicMock()
    page.url = "https://example.com"
    page.goto = AsyncMock()
    page.wait_for_load_state = AsyncMock()
    page.screenshot = AsyncMock(return_value=b"\x89PNG\r\n\x1a\n")
    page.keyboard = MagicMock()
    page.keyboard.press = AsyncMock()

    locator = MagicMock()
    locator.click = AsyncMock()
    locator.fill = AsyncMock()
    locator.hover = AsyncMock()
    locator.select_option = AsyncMock()
    page.locator.return_value = MagicMock()
    page.locator.return_value.nth.return_value = locator

    # Default evaluate: element extraction returns one visible element
    async def _evaluate(js, *args, **kwargs):
        if args:
            return list(_FAKE_ELEMENTS)
        return None

    page.evaluate = AsyncMock(side_effect=_evaluate)
    return page


def _browser_setup(page):
    """Return (browser, context) mocks wired to the given page."""
    context = MagicMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    context.pages = [page]

    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    return browser, context


def _step_response(action, element_index=None, text="", friction=None, success=None, thinking="ok"):
    """Build a fake LLM response containing one BrowserStep."""
    step = {
        "thinking": thinking,
        "action": action,
        "element_index": element_index,
        "text": text,
        "friction_observed": friction or [],
        "success": success,
    }
    r = MagicMock()
    r.choices[0].message.content = json.dumps(step)
    r.usage.prompt_tokens = 100
    r.usage.completion_tokens = 50
    return r


# --- _extract_interactive_elements ---

@pytest.mark.anyio
async def test_extract_interactive_elements_filters_invisible():
    page = _page_mock()
    page.evaluate = AsyncMock(return_value=[
        {"tag": "button", "role": "", "name": "Sign Up", "placeholder": "", "type_": "", "visible": True},
        {"tag": "input",  "role": "", "name": "",        "placeholder": "email", "type_": "email", "visible": False},
    ])
    element_list, locators = await _extract_interactive_elements(page)
    assert "Sign Up" in element_list
    assert len(locators) == 1


@pytest.mark.anyio
async def test_extract_interactive_elements_respects_cap():
    page = _page_mock()
    page.evaluate = AsyncMock(return_value=[
        {"tag": "button", "role": "", "name": f"btn{i}", "placeholder": "", "type_": "", "visible": True}
        for i in range(ELEMENT_CAP + 2)
    ])
    _, locators = await _extract_interactive_elements(page)
    assert len(locators) == ELEMENT_CAP


@pytest.mark.anyio
async def test_extract_interactive_elements_evaluate_exception_returns_empty():
    page = _page_mock()
    page.evaluate = AsyncMock(side_effect=Exception("js error"))
    element_list, locators = await _extract_interactive_elements(page)
    assert "no interactive elements" in element_list
    assert locators == []


# --- _follow_page_after_click ---

@pytest.mark.anyio
async def test_follow_page_after_click_returns_new_tab():
    original = AsyncMock()
    new_tab = AsyncMock()
    context = MagicMock()
    context.pages = [original, new_tab]
    result = await _follow_page_after_click(context, original, 1)
    assert result is new_tab


@pytest.mark.anyio
async def test_follow_page_after_click_no_new_tab():
    original = AsyncMock()
    context = MagicMock()
    context.pages = [original]
    result = await _follow_page_after_click(context, original, 1)
    assert result is original


# --- run_browser_agent ---

@pytest.mark.anyio
async def test_run_browser_agent_completed():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    done = _step_response("done", success=True, thinking="I found it")

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(return_value=done)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.01):
            result, in_tok, out_tok, cost = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert result.status == "completed"
    assert result.steps_taken == 1
    assert result.comment == "I found it"
    assert cost == 0.01


@pytest.mark.anyio
async def test_run_browser_agent_give_up():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    give_up = _step_response("give_up", success=False, thinking="too hard")

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(return_value=give_up)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert result.status == "abandoned"
    assert result.abandonment_reason == "too hard"


@pytest.mark.anyio
async def test_run_browser_agent_timeout():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    scroll = _step_response("scroll", text="300")

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(return_value=scroll)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=1,
                agent_index=0,
            )

    assert result.status == "timeout"
    assert result.steps_taken == 1


@pytest.mark.anyio
async def test_run_browser_agent_screen_reader_mode():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    done = _step_response("done", success=True, thinking="done")

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(return_value=done)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_SCREEN_READER_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert result.status == "completed"
    page.screenshot.assert_not_called()


@pytest.mark.anyio
async def test_run_browser_agent_friction_collected():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    step_with_friction = _step_response("scroll", text="300", friction=["confusing label"])
    done = _step_response("done", success=True)
    responses = iter([step_with_friction, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert "confusing label" in result.friction_points


@pytest.mark.anyio
async def test_run_browser_agent_cost_accumulated():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    scroll = _step_response("scroll", text="300")
    done = _step_response("done", success=True)
    responses = iter([scroll, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.05):
            _, _, _, cost = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert abs(cost - 0.10) < 1e-10


@pytest.mark.anyio
async def test_run_browser_agent_element_out_of_range():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    # element_index=99 is out of range for 1 element; agent should continue
    bad_click = _step_response("click", element_index=99)
    done = _step_response("done", success=True)
    responses = iter([bad_click, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert result.status == "completed"


@pytest.mark.anyio
async def test_run_browser_agent_null_element_for_element_action():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    bad_click = _step_response("click", element_index=None)
    done = _step_response("done", success=True)
    responses = iter([bad_click, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert result.status == "completed"


@pytest.mark.anyio
async def test_run_browser_agent_scroll_with_pixels():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    scroll = _step_response("scroll", text="300")
    done = _step_response("done", success=True)
    responses = iter([scroll, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    # Single-arg evaluate calls are scroll (two-arg ones are element extraction)
    scroll_calls = [c for c in page.evaluate.call_args_list if len(c.args) == 1]
    assert any("scrollBy(0, 300)" in str(c.args[0]) for c in scroll_calls)


@pytest.mark.anyio
async def test_run_browser_agent_scroll_empty_text():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    scroll = _step_response("scroll", text="")
    done = _step_response("done", success=True)
    responses = iter([scroll, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    scroll_calls = [c for c in page.evaluate.call_args_list if len(c.args) == 1]
    assert any("window.innerHeight" in str(c.args[0]) for c in scroll_calls)


@pytest.mark.anyio
async def test_run_browser_agent_cancelled_error_reraises():
    page = _page_mock()
    browser, _ = _browser_setup(page)

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=asyncio.CancelledError)):
        with pytest.raises(asyncio.CancelledError):
            await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )


@pytest.mark.anyio
async def test_run_browser_agent_type_action():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    type_step = _step_response("type", element_index=0, text="hello@example.com")
    done = _step_response("done", success=True)
    responses = iter([type_step, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="fill email",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert result.status == "completed"
    locator = page.locator.return_value.nth.return_value
    locator.fill.assert_called_once()


@pytest.mark.anyio
async def test_run_browser_agent_hover_action():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    hover_step = _step_response("hover", element_index=0)
    done = _step_response("done", success=True)
    responses = iter([hover_step, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="hover menu",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    assert result.status == "completed"
    locator = page.locator.return_value.nth.return_value
    locator.hover.assert_called_once()


@pytest.mark.anyio
async def test_run_browser_agent_press_key_action():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    press = _step_response("press_key", text="Enter")
    done = _step_response("done", success=True)
    responses = iter([press, done])

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(side_effect=responses)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="submit form",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    page.keyboard.press.assert_called_once_with("Enter")


@pytest.mark.anyio
async def test_run_browser_agent_unexpected_exception_completes_gracefully():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    # Simulate page.goto raising an unexpected error
    page.goto = AsyncMock(side_effect=RuntimeError("connection refused"))

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock()):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            result, _, _, _ = await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
            )

    # Agent returns a result even when an unexpected error occurs
    assert result.status in ("timeout", "completed", "abandoned", "error")


@pytest.mark.anyio
async def test_run_browser_agent_on_step_callback():
    page = _page_mock()
    browser, _ = _browser_setup(page)
    done = _step_response("done", success=True, thinking="done")
    step_calls: list[tuple[str, str, int]] = []

    with patch("ux_swarm.browser_agent._call_with_retry", new=AsyncMock(return_value=done)):
        with patch("ux_swarm.browser_agent.completion_cost", return_value=0.0):
            await run_browser_agent(
                browser=browser,
                url="https://example.com",
                task="find nav",
                user_type=_DEFAULT_USER,
                model="model",
                llm_semaphore=asyncio.Semaphore(5),
                max_steps=8,
                agent_index=0,
                on_step=lambda s, d, st: step_calls.append((s, d, st)),
            )

    assert any(s == "complete" for s, _, _ in step_calls)
