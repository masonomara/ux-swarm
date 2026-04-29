import asyncio
import base64
import time
from collections.abc import Callable

from playwright.async_api import Browser, BrowserContext, Locator, Page
from litellm import completion_cost
from litellm.types.utils import Usage

from ux_swarm.agent import _call_with_retry
from ux_swarm.models import AgentResult, BrowserStep, UserType

PAGE_LOAD_TIMEOUT_MS = 30_000  # Playwright navigation limit; not user-perceived wait
INITIAL_NETWORKIDLE_WAIT_MS = 2_000  # cap for JS-heavy pages to settle; not a forced delay
POST_CLICK_WAIT_MS = 1_500  # wait for navigation after a click
ELEMENT_TIMEOUT_MS = 5_000  # time to locate and interact with an element
NEW_TAB_TIMEOUT_MS = 10_000  # budget for tabs opened by the page itself

VIEWPORT_HEIGHT_PX = 720

BROWSER_ACTIONS: tuple[str,
                       ...] = ("click", "type", "scroll", "hover", "press_key",
                               "select_option", "done", "give_up")
BROWSER_TERMINAL_ACTIONS: frozenset[str] = frozenset({"done", "give_up"})

SCREEN_READER_ACTIONS: tuple[str, ...] = ("press_key", "type", "done", "give_up")

ELEMENT_CAP = 50
ACTION_HISTORY_LIMIT = 5
MAX_CONCURRENT_LLM_CALLS = 5

INTERACTIVE_SELECTOR = (
    "a[href], button, input:not([type='hidden']), select, textarea, "
    "[role='button'], [role='link'], [role='checkbox'], [role='radio'], "
    "[role='menuitem'], [role='option'], [role='tab'], [role='combobox'], "
    "[contenteditable='true']")

ELEMENT_ACTIONS: frozenset[str] = frozenset(
    {"click", "type", "hover", "select_option"})

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
    "thinking": "what you see and what to do next",
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
            "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}
        })
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": content},
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

    recent = actions_taken[-ACTION_HISTORY_LIMIT:]
    if recent:
        history = "\n".join(f"  {a}" for a in recent)
        parts.append(f"Recent actions:\n{history}")

    parts.append("What do you do next?")
    return "\n\n".join(parts)


async def _extract_interactive_elements(
        page: Page) -> tuple[str, list[Locator]]:
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
        return "(no interactive elements found)", []

    lines: list[str] = []
    locators: list[Locator] = []
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
        lines.append(f"[{logical_index}] {tag}{role}{name}{ph}{t}")
        logical_index += 1

    element_list_str = "\n".join(
        lines) if lines else "(no interactive elements found)"
    return element_list_str, locators


async def _follow_page_after_click(
    browser_context: BrowserContext,
    page: Page,
    page_count_before: int,
) -> Page:
    try:
        await page.wait_for_load_state("domcontentloaded",
                                       timeout=POST_CLICK_WAIT_MS)
    except Exception:
        pass

    pages_after = browser_context.pages
    if len(pages_after) > page_count_before:
        new_tab = pages_after[-1]
        try:
            await new_tab.wait_for_load_state("domcontentloaded",
                                              timeout=NEW_TAB_TIMEOUT_MS)
        except Exception:
            pass
        return new_tab

    return page


async def run_browser_agent(
    browser: Browser,
    url: str,
    task: str,
    user_type: UserType,
    model: str,
    llm_semaphore: asyncio.Semaphore,
    max_steps: int,
    agent_index: int,
    viewport: int = 1280,
    on_step: Callable[[str, str, int], None] | None = None,
) -> tuple[AgentResult, int, int, float]:
    """Run one browser agent. Returns (AgentResult, total_input_tokens, total_output_tokens, total_cost)."""
    actions_taken: list[str] = []
    urls_visited: list[str] = []
    all_friction: list[str] = []
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
        accessibility_note=_SCREEN_READER_NOTE + "\n" if is_screen_reader else "",
        page_context="a numbered list of interactive elements" if is_screen_reader else "a screenshot of the current page",
        task=task,
        actions=" | ".join(allowed_actions),
    )
    browser_context = None

    try:
        browser_context = await browser.new_context(
            viewport={
                "width": viewport,
                "height": VIEWPORT_HEIGHT_PX
            })
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

        for step_index in range(max_steps):
            steps_taken = step_index + 1
            if on_step:
                on_step("running", last_thinking[:120] if last_thinking else page.url, steps_taken)

            screenshot_b64: str | None = None
            if not is_screen_reader:
                screenshot_bytes = await page.screenshot(full_page=False)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
            element_list_str, locators = await _extract_interactive_elements(
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

            async with llm_semaphore:
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
                step_data = BrowserStep.model_validate_json(raw)
            except Exception:
                continue

            all_friction.extend(step_data.friction_observed)

            action = step_data.action
            element_index = step_data.element_index
            text_value = step_data.text
            thinking = step_data.thinking
            last_thinking = thinking

            index_str = str(element_index) if element_index is not None else ""
            detail = f"[{index_str}] {text_value or ''}".strip()
            action_label = f"{action}: {detail}"
            actions_taken.append(action_label)

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
                    abandonment_reason = thinking
                    comment = thinking
                    if on_step: on_step("failed", thinking[:120], steps_taken)
                break

            if on_step: on_step("running", thinking[:120] if thinking else "", steps_taken)

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
                        page_count_before = len(browser_context.pages)
                        await target.click(timeout=ELEMENT_TIMEOUT_MS)
                        page = await _follow_page_after_click(
                            browser_context, page, page_count_before)
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
                    await page.keyboard.press(text_value)

            except Exception:
                pass

            current_url = page.url
            if current_url not in urls_visited:
                urls_visited.append(current_url)

    except asyncio.CancelledError:
        if on_step: on_step("failed", "cancelled", steps_taken)
        raise
    except Exception as unexpected_error:
        if on_step: on_step("failed", str(unexpected_error)[:80], steps_taken)
    finally:
        if browser_context is not None:
            await browser_context.close()

    duration = round(time.monotonic() - start_time, 2)

    if status == "timeout":
        comment = last_thinking

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
        duration=duration,
    )

    return agent_result, total_in_tok, total_out_tok, total_cost
