import asyncio
import base64
import json
import random
import re
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, cast

import litellm
from litellm import ModelResponse, acompletion, completion_cost
from litellm.exceptions import RateLimitError
from litellm.types.utils import Usage
from playwright.async_api import Browser, BrowserContext, Locator, Page

from ux_swarm.errors import CliError
from ux_swarm.models import AgentResult, BrowserStep, ScreenshotDecision, UserType

litellm.suppress_debug_info = True


def _strip_code_fence(text: str) -> str:
    # Complete fence
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    # Truncated fence (no closing ```)
    match = re.match(r"```(?:json)?\s*\n?(.*)", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


# ── Screenshot agent ──────────────────────────────────────────────────────────

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_RETRY_DELAYS = (60, 120, 240)


def _media_type(path: Path) -> str:
    return _MIME_TYPES.get(path.suffix.lower(), "image/png")


def _load_image(target: str) -> tuple[str, str]:
    path = Path(target)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {target}")
    return base64.standard_b64encode(path.read_bytes()).decode("ascii"), _media_type(path)


def _build_system_prompt(user_type: UserType) -> str:
    schema = json.dumps(ScreenshotDecision.model_json_schema(), indent=2)
    return (
        "You are a synthetic user in a UX test.\n\n"
        f"You are playing the role of: {user_type.label}\n"
        f"{user_type.description}\n\n"
        "Important: if you feel confused or uncertain about what to do, that confusion is the finding — record it in friction_observed.\n\n"
        "Return a JSON object matching this schema exactly:\n"
        f"{schema}\n\n"
        "Return ONLY valid JSON. No markdown fences, no explanation, no preamble."
    )


# ── Shared ────────────────────────────────────────────────────────────────────


async def _call_with_retry(model: str, messages: list[dict]) -> ModelResponse:
    for delay in (*_RETRY_DELAYS, None):
        try:
            return cast(
                ModelResponse, await acompletion(
                    model=model,
                    messages=messages,
                    max_tokens=4096,
                    response_format={"type": "json_object"},
                ))
        except RateLimitError as exc:
            if delay is None:
                raise CliError(
                    f"Rate limited after {len(_RETRY_DELAYS) + 1} attempts. "
                    "Try again later or reduce --users.") from exc
            await asyncio.sleep(delay + random.uniform(0, delay * 0.5))
    raise AssertionError("unreachable")


async def run_screenshot_agent(
    target: str,
    task: str,
    user_type: UserType,
    model: str,
) -> tuple[ScreenshotDecision, int, int, float]:
    image_data, media_type = _load_image(target)
    messages = [
        {
            "role": "system",
            "content": _build_system_prompt(user_type)
        },
        {
            "role":
            "user",
            "content": [
                {
                    "type": "text",
                    "text": f"Task: {task}"
                },
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{image_data}"
                    }
                },
            ],
        },
    ]

    response = await _call_with_retry(model, messages)
    raw = response.choices[0].message.content or ""
    usage = cast(Usage, getattr(response, "usage"))

    try:
        cost = completion_cost(completion_response=response, model=model)
    except Exception:
        cost = 0.0

    try:
        decision = ScreenshotDecision.model_validate_json(_strip_code_fence(raw))
    except Exception:
        decision = ScreenshotDecision(
            target_element="unknown",
            reasoning=raw[:500],
            comment="Agent response was not valid JSON.",
            friction_observed=["Agent response was not valid JSON"],
            completed=False,
            abandoned=True,
            abandonment_reason="parse failure",
        )

    return decision, usage.prompt_tokens, usage.completion_tokens, cost


# ── Browser agent ─────────────────────────────────────────────────────────────

PAGE_LOAD_TIMEOUT_MS = 30_000
INITIAL_NETWORKIDLE_WAIT_MS = 2_000
POST_CLICK_WAIT_MS = 1_500
ELEMENT_TIMEOUT_MS = 5_000
NEW_TAB_TIMEOUT_MS = 10_000
NEW_TAB_DETECT_S = 2.0

VIEWPORT_HEIGHT_PX = 720

_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/124.0.0.0 Safari/537.36")
_WEBDRIVER_MASK = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"

BROWSER_ACTIONS = ("click", "type", "scroll", "hover", "press_key",
                   "select_option", "done", "give_up")
BROWSER_TERMINAL_ACTIONS = frozenset({"done", "give_up"})

SCREEN_READER_ACTIONS = ("press_key", "type", "done", "give_up")

ELEMENT_CAP = 50
ACTION_HISTORY_LIMIT = 5

INTERACTIVE_SELECTOR = (
    "a[href], button, input:not([type='hidden']), select, textarea, "
    "[role='button'], [role='link'], [role='checkbox'], [role='radio'], "
    "[role='menuitem'], [role='option'], [role='tab'], [role='combobox'], "
    "[contenteditable='true'], [tabindex]:not([tabindex='-1'])")

ELEMENT_ACTIONS = frozenset({"click", "type", "hover", "select_option"})

_BROWSER_SYSTEM = """\
You are a synthetic user in a UX test.

You are playing the role of: {label}
{description}
{accessibility_note}
You are browsing a real website. At each step you see {page_context} and a numbered list of interactive elements currently visible on it.
Your goal: {task}

Take ONE action per step. The moment the goal is satisfied — the target content is visible or you have arrived at the right page — respond with done immediately. Do not continue exploring after the task is complete.

Respond in JSON:
{{
    "thinking": "inner monologue in your persona's voice — observe and plan as your character would",
    "action": "{actions}",
    "element_index": "integer index from the element list for click/type/hover/select_option; null for scroll/press_key/done/give_up",
    "text": "text to type; key name for press_key (Enter Tab Escape ArrowDown Space); option value for select_option; pixels to scroll for scroll — any integer, positive scrolls down, negative scrolls up, no limit (e.g. '300', '5000', '-500'); empty scrolls one full viewport height; else empty",
    "friction_observed": ["list any confusion or friction you experienced this step, including anything that would be hard to navigate with a screen reader"],
    "success": "true or false for done/give_up; null for all other actions"
}}

element_index must be an integer from the list you were given, or null. Never invent a selector or index.
When an element might reveal a dropdown on hover, use hover before click.
Return ONLY valid JSON. No markdown, no preamble."""

_SCREEN_READER_NOTE = """\
You are using a screen reader and cannot see the page. You navigate entirely by keyboard: Tab to move between elements, Enter to activate, arrow keys for menus and lists. You have NO access to the visual layout — only the element list (ARIA labels, roles, and text). If an element has no meaningful label, flag it as a friction point."""


def _build_messages(system_prompt: str, user_prompt: str,
                    screenshot_b64: str | None) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": user_prompt}]
    if screenshot_b64:
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/png;base64,{screenshot_b64}"
            }
        })
    return [
        {
            "role": "system",
            "content": system_prompt
        },
        {
            "role": "user",
            "content": content
        },
    ]


def _build_browser_user_prompt(
    step: int,
    max_steps: int,
    current_url: str,
    actions_taken: list[str],
    element_list: str,
) -> str:
    parts = [f"Step {step}/{max_steps}. URL: {current_url}"]
    if element_list:
        parts.append(f"Interactive elements:\n{element_list}")
    if recent := actions_taken[-ACTION_HISTORY_LIMIT:]:
        parts.append("Recent actions:\n" + "\n".join(f"  {a}" for a in recent))
    parts.append("What do you do next?")
    return "\n\n".join(parts)


async def _extract_interactive_elements(
        page: Page) -> tuple[str, list[Locator], list[str]]:
    try:
        js_result: list[dict] = await page.evaluate(
            """(selector) => {
                const els = Array.from(document.querySelectorAll(selector));
                return els.map(el => {
                    const rect = el.getBoundingClientRect();
                    return {
                        tag:         el.tagName.toLowerCase(),
                        role:        el.getAttribute('role') || '',
                        name:        (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 80),
                        placeholder: el.getAttribute('placeholder') || '',
                        type_:       el.getAttribute('type') || '',
                        visible:     rect.width > 0 && rect.height > 0,
                    };
                });
            }""",
            INTERACTIVE_SELECTOR,
        )
    except Exception:
        return "(no interactive elements found)", [], []

    lines: list[str] = []
    locators: list[Locator] = []
    element_descs: list[str] = []
    logical_index = 0

    for raw_index, meta in enumerate(js_result):
        if logical_index >= ELEMENT_CAP:
            break
        if not meta["visible"]:
            continue

        locators.append(page.locator(INTERACTIVE_SELECTOR).nth(raw_index))

        tag = meta["tag"].upper()
        role = f" [{meta['role']}]" if meta["role"] else ""
        name = f' "{meta["name"]}"' if meta["name"] else ""
        ph = f' placeholder="{meta["placeholder"]}"' if meta[
            "placeholder"] and not meta["name"] else ""
        t = f' type="{meta["type_"]}"' if meta["type_"] and meta[
            "tag"] == "input" else ""
        desc = f"{tag}{role}{name}{ph}{t}"
        element_descs.append(desc)
        lines.append(f"[{logical_index}] {desc}")
        logical_index += 1

    element_list_str = "\n".join(
        lines) if lines else "(no interactive elements found)"
    return element_list_str, locators, element_descs


async def _follow_page_after_click(
    context: BrowserContext,
    original_page: Page,
    original_page_count: int,
) -> Page:
    if len(context.pages) > original_page_count:
        new_page = context.pages[-1]
        try:
            await new_page.wait_for_load_state("domcontentloaded",
                                               timeout=NEW_TAB_TIMEOUT_MS)
        except Exception:
            pass
        return new_page
    return original_page


async def _click_and_follow(
    browser_context: BrowserContext,
    page: Page,
    target: Locator,
) -> Page:
    try:
        async with browser_context.expect_page(
                timeout=int(NEW_TAB_DETECT_S * 1000)) as new_page_info:
            await target.click(timeout=ELEMENT_TIMEOUT_MS)
        new_page = await new_page_info.value
        try:
            await new_page.wait_for_load_state("domcontentloaded",
                                               timeout=NEW_TAB_TIMEOUT_MS)
        except Exception:
            pass
        return new_page
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded",
                                           timeout=POST_CLICK_WAIT_MS)
        except Exception:
            pass
        return page


async def _press_and_follow(
    browser_context: BrowserContext,
    page: Page,
    key: str,
) -> Page:
    if key not in ("Enter", "Space"):
        await page.keyboard.press(key)
        return page
    try:
        async with browser_context.expect_page(
                timeout=int(NEW_TAB_DETECT_S * 1000)) as new_page_info:
            await page.keyboard.press(key)
        new_page = await new_page_info.value
        try:
            await new_page.wait_for_load_state("domcontentloaded",
                                               timeout=NEW_TAB_TIMEOUT_MS)
        except Exception:
            pass
        return new_page
    except Exception:
        try:
            await page.wait_for_load_state("domcontentloaded",
                                           timeout=POST_CLICK_WAIT_MS)
        except Exception:
            pass
        return page


async def run_browser_agent(
    browser: Browser,
    url: str,
    task: str,
    user_type: UserType,
    model: str,
    llm_semaphore: asyncio.Semaphore | None,
    max_steps: int,
    agent_index: int,
    viewport: int = 1280,
    on_step: Callable[[str, str, int], None] | None = None,
    upload_screenshot: Callable[[int, bytes], Coroutine[Any, Any, str]] | None = None,
) -> tuple[AgentResult, int, int, float]:
    actions_taken: list[str] = []
    urls_visited: list[str] = []
    all_friction: list[str] = []
    step_screenshot_bytes: list[bytes] = []
    step_frictions: list[list[str]] = []
    step_thinking: list[str] = []
    status = "timeout"
    abandonment_reason: str | None = None
    comment = ""
    last_thinking = ""
    total_in_tok = 0
    total_out_tok = 0
    total_cost = 0.0
    steps_taken = 0
    start_time = time.monotonic()

    is_screen_reader = user_type.accessibility == "screen_reader"
    allowed_actions = SCREEN_READER_ACTIONS if is_screen_reader else BROWSER_ACTIONS

    system_prompt = _BROWSER_SYSTEM.format(
        label=user_type.label,
        description=user_type.description,
        accessibility_note=_SCREEN_READER_NOTE +
        "\n" if is_screen_reader else "",
        page_context="a numbered list of interactive elements"
        if is_screen_reader else "a screenshot of the current page",
        task=task,
        actions=" | ".join(allowed_actions),
    )
    browser_context = None

    try:
        browser_context = await browser.new_context(
            viewport={
                "width": viewport,
                "height": VIEWPORT_HEIGHT_PX
            },
            user_agent=_USER_AGENT,
        )
        await browser_context.add_init_script(_WEBDRIVER_MASK)
        page = await browser_context.new_page()
        if on_step: on_step("running", url, 0)
        await page.goto(url,
                        wait_until="domcontentloaded",
                        timeout=PAGE_LOAD_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle",
                                           timeout=INITIAL_NETWORKIDLE_WAIT_MS)
        except Exception:
            pass
        urls_visited.append(page.url)

        for steps_taken in range(1, max_steps + 1):
            if on_step:
                on_step("running",
                        last_thinking[:120] if last_thinking else page.url,
                        steps_taken)

            screenshot_b64: str | None = None
            if not is_screen_reader:
                screenshot_bytes = await page.screenshot(full_page=False)
                step_screenshot_bytes.append(screenshot_bytes)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
            element_list_str, locators, element_descs = await _extract_interactive_elements(
                page)

            user_prompt = _build_browser_user_prompt(
                step=steps_taken,
                max_steps=max_steps,
                current_url=page.url,
                actions_taken=actions_taken,
                element_list=element_list_str,
            )

            messages = _build_messages(system_prompt, user_prompt,
                                       screenshot_b64)

            if llm_semaphore:
                async with llm_semaphore:
                    response = await _call_with_retry(model, messages)
            else:
                response = await _call_with_retry(model, messages)

            usage: Usage = getattr(response, "usage")
            total_in_tok += usage.prompt_tokens
            total_out_tok += usage.completion_tokens
            try:
                total_cost += completion_cost(completion_response=response,
                                              model=model)
            except Exception:
                pass

            raw = response.choices[0].message.content or "{}"

            try:
                step_data = BrowserStep.model_validate_json(_strip_code_fence(raw))
            except Exception:
                step_frictions.append([])  # keep in sync with step_screenshot_bytes
                continue

            all_friction.extend(step_data.friction_observed)
            step_frictions.append(step_data.friction_observed)
            step_thinking.append(step_data.thinking)

            action = step_data.action
            element_index = step_data.element_index
            text_value = step_data.text
            thinking = step_data.thinking
            last_thinking = thinking

            if element_index is not None and element_index < len(element_descs):
                elem_desc = element_descs[element_index]
                detail = f"{elem_desc} → {text_value}" if text_value else elem_desc
            else:
                detail = text_value or ""
            actions_taken.append(f"{action}: {detail}" if detail else action)

            if action not in allowed_actions:
                continue

            if action in BROWSER_TERMINAL_ACTIONS:
                if action == "done":
                    status = "completed"
                    comment = thinking
                    if on_step:
                        on_step("complete", thinking[:120], steps_taken)
                else:
                    status = "abandoned"
                    abandonment_reason = comment = thinking
                    if on_step: on_step("failed", thinking[:120], steps_taken)
                break

            if on_step:
                on_step("running", thinking[:120] if thinking else "",
                        steps_taken)

            try:
                if action in ELEMENT_ACTIONS:
                    if element_index is None:
                        raise ValueError(
                            f"Action '{action}' requires element_index but got null"
                        )
                    if element_index < 0 or element_index >= len(locators):
                        raise ValueError(
                            f"element_index {element_index} out of range "
                            f"(page has {len(locators)} interactive elements)")
                    target = locators[element_index]

                    if action == "click":
                        prev_url = page.url
                        page = await _click_and_follow(browser_context, page,
                                                       target)
                        if page.url != prev_url:
                            actions_taken.append(f"→ navigated to: {page.url}")
                    elif action == "type":
                        await target.fill(text_value,
                                          timeout=ELEMENT_TIMEOUT_MS)
                    elif action == "hover":
                        await target.hover(timeout=ELEMENT_TIMEOUT_MS)
                    elif action == "select_option":
                        await target.select_option(text_value,
                                                   timeout=ELEMENT_TIMEOUT_MS)

                elif action == "scroll":
                    try:
                        px = int(text_value)
                    except (ValueError, TypeError):
                        px = None
                    if px is not None:
                        await page.evaluate(f"window.scrollBy(0, {px})")
                    else:
                        await page.evaluate(
                            "window.scrollBy(0, window.innerHeight)")

                elif action == "press_key":
                    prev_url = page.url
                    page = await _press_and_follow(browser_context, page,
                                                   text_value)
                    if page.url != prev_url:
                        actions_taken.append(f"→ navigated to: {page.url}")

            except Exception:
                pass

            current_url = page.url
            if current_url not in urls_visited:
                urls_visited.append(current_url)

        if status == "timeout" and on_step:
            on_step("timeout", last_thinking[:120] if last_thinking else "", steps_taken)

    except asyncio.CancelledError:
        if on_step: on_step("failed", "cancelled", steps_taken)
        raise
    except Exception as unexpected_error:
        status = "error"
        comment = str(unexpected_error)
        if on_step: on_step("failed", comment[:80], steps_taken)
    finally:
        if browser_context is not None:
            await browser_context.close()

    duration = round(time.monotonic() - start_time, 2)

    if status == "timeout":
        comment = last_thinking

    step_screenshots: list[str | None] | None = None
    if upload_screenshot and step_screenshot_bytes:
        results = await asyncio.gather(
            *[upload_screenshot(i, png) for i, png in enumerate(step_screenshot_bytes)],
            return_exceptions=True,
        )
        step_screenshots = [r if isinstance(r, str) else None for r in results]

    agent_result = AgentResult(
        agent_index=agent_index,
        user_type=user_type.label,
        status=status,
        abandonment_reason=abandonment_reason,
        friction_points=all_friction,
        comment=comment,
        steps_taken=steps_taken,
        input_tokens=total_in_tok,
        output_tokens=total_out_tok,
        cost=total_cost,
        actions_taken=actions_taken,
        urls_visited=urls_visited,
        step_screenshots=step_screenshots,
        step_frictions=step_frictions if step_frictions else None,
        step_thinking=step_thinking if step_thinking else None,
        duration=duration,
    )

    return agent_result, total_in_tok, total_out_tok, total_cost
