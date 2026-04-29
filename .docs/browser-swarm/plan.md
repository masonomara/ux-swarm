# Plan: Browser Swarm

_Based on: beta research, source audit of `src/ux_swarm/`, design analysis_

---

## Scope

### In Scope

- Replace the URL `CliError` stub with a real browser swarm
- New `src/ux_swarm/browser_agent.py` — full Playwright loop per agent
- Expanded action set: `click`, `type`, `scroll`, `hover`, `press_key`, `select_option`, `done`, `give_up`
- Accessibility tree passed alongside every screenshot — reliable selector generation
- CSS selector interface (not pixel coordinates) — revises `BrowserAction` in `models.py`
- `run_browser_swarm` in `swarm.py` — TaskGroup, two semaphores, staggered launch, friction consolidation
- Per-agent live progress table in `main.py` — status/step/detail per row
- `--headed` flag — show browser window during run (debugging)
- Raise `max_steps` default from 3 to 8
- `avg_steps_to_completion` added to `SwarmResult`
- `swarm expand` updated to print action sequences for browser results
- `SwarmResult` persisted to `results.json` (same as screenshot mode)

### Not In Scope

- `swarm compare` — diff two results (post-alpha)
- `select_option` edge case coverage (add action, validate at agent level)
- Headful multi-agent display (`--headed` is single-agent debugging)
- DeepSeek (text-only, no vision)

---

## Files

### Create

| File                            | Role                                                                                   |
| ------------------------------- | -------------------------------------------------------------------------------------- |
| `src/ux_swarm/browser_agent.py` | Full browser agent: prompts, accessibility snapshot, Playwright loop, action execution |

### Modify

| File                     | What Changes                                                                                                                                               |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `src/ux_swarm/models.py` | Replace `BrowserAction`/`BrowserDecision` with `BrowserStep`; add optional browser fields to `AgentResult`; add `avg_steps_to_completion` to `SwarmResult` |
| `src/ux_swarm/swarm.py`  | Add `run_browser_swarm` and `_aggregate_browser`                                                                                                           |
| `src/ux_swarm/main.py`   | Route URLs to browser swarm; `--headed` flag; browser progress display; update `expand` for action sequences; raise `max_steps` default                    |

### Untouched

`agent.py`, `cli.py`, `config.py`, `menu.py`, `personas.py`, `__init__.py`

---

## Phase 1 — `src/ux_swarm/models.py`

### Replace `BrowserAction` and `BrowserDecision` with `BrowserStep`

The current `BrowserAction` uses pixel coordinates (`x: int | None`, `y: int | None`). LLMs cannot reliably produce accurate pixel coordinates from compressed screenshots, and coordinates break under responsive layouts. CSS selectors are what Playwright is designed for and what LLMs generate reliably from accessibility trees. `BrowserDecision` wraps `BrowserAction` with completed/abandoned flags — collapse these into a single model using `done`/`give_up` actions as terminal signals, matching the beta's pattern.

**Remove:**

```python
class BrowserAction(BaseModel): ...  # delete entirely
class BrowserDecision(BaseModel): ...  # delete entirely
```

**Add:**

```python
class BrowserStep(BaseModel):
    """LLM response for one step of a browser agent."""
    thinking: str           # chain-of-thought before committing to an action
    action: str             # expected: click | type | scroll | hover | press_key | select_option | done | give_up
    selector: str           # CSS or text= selector; empty for scroll, press_key, done, give_up
    text: str               # type: text to type; press_key: key name; select_option: option value; else empty
    friction_observed: list[str]  # UX friction observations this step
    success: bool | None    # True=done success, False=give_up; None for all other actions
```

`action` is `str`, not `Literal[...]`. The valid action set is enforced at runtime in the agent loop via `ALL_BROWSER_ACTIONS`, not at the model layer — this keeps the model open as the action set evolves.

### Update `AgentResult`

Add optional browser-mode fields. All default to `None` so screenshot results are unaffected.

```python
class AgentResult(BaseModel):
    """Output of one agent, shared by both modes."""
    agent_index: int
    user_type: str
    completed: bool
    abandoned: bool
    abandonment_reason: str | None
    friction_points: list[str]
    comment: str
    target_element: str | None = None  # screenshot mode only
    reasoning: str | None = None       # screenshot mode only
    steps_taken: int
    input_tokens: int
    output_tokens: int
    cost: float
    actions_taken: list[str] | None = None  # browser mode: ["click: text=Sign Up", ...]
    urls_visited: list[str] | None = None   # browser mode: pages navigated to
    duration: float | None = None           # browser mode: wall-clock seconds
```

### Update `SwarmResult`

Add `avg_steps_to_completion` with a default of `0.0` so screenshot results round-trip correctly.

```python
class SwarmResult(BaseModel):
    """Full output written to results.json."""
    timestamp: str
    mode: Literal["screenshot", "browser"]
    target: str
    task: str
    model: str
    users: int
    completion_rate: float
    margin_of_error: float
    user_breakdown: dict[str, float]
    friction_points: list[str]
    total_cost: float
    individual_results: list[AgentResult]
    avg_steps_to_completion: float = 0.0  # browser mode; 0.0 in screenshot mode
```

---

## Phase 2 — `src/ux_swarm/browser_agent.py` (new file)

### Constants

```python
# Playwright navigation timeouts — in milliseconds
# PAGE_LOAD_TIMEOUT_MS: how long Playwright waits for domcontentloaded before
# raising a navigation error. 30s is a Playwright navigation limit, not a user-perceived
# wait — it sets the upper bound on broken/unreachable pages, not on slow-but-real ones.
PAGE_LOAD_TIMEOUT_MS = 30_000
INITIAL_NETWORKIDLE_WAIT_MS = 2_000   # cap for JS-heavy pages to settle; not a forced delay
POST_CLICK_WAIT_MS = 1_500            # wait for navigation after a click
ELEMENT_TIMEOUT_MS = 5_000            # time to locate and interact with an element
NEW_TAB_TIMEOUT_MS = 10_000           # budget for tabs opened by the page itself

VIEWPORT_HEIGHT_PX = 720

BROWSER_ACTIONS: tuple[str, ...] = (
    "click", "type", "scroll", "hover", "press_key", "select_option", "done", "give_up"
)
BROWSER_TERMINAL_ACTIONS: frozenset[str] = frozenset({"done", "give_up"})
ALL_BROWSER_ACTIONS: frozenset[str] = frozenset(BROWSER_ACTIONS)

MAX_CONCURRENT_LLM_CALLS = 3    # cross-agent LLM cap; independent of browser concurrency
ACCESSIBILITY_TREE_CHAR_LIMIT = 3_000
ACTION_HISTORY_LIMIT = 5
```

**Timeout rationale:** `PAGE_LOAD_TIMEOUT_MS = 30_000` is Playwright's navigation timeout — the upper bound before it gives up and raises an error. A page that genuinely takes 30 seconds to load has a real problem; a shorter timeout would cause false failures on slow-but-functional pages. `ELEMENT_TIMEOUT_MS = 5_000` is how long Playwright waits to locate a selector before giving up — tight enough to detect broken selectors within a step, generous enough for elements that animate into view.

### `_build_browser_system_prompt(user_type: UserType, task: str) -> str`

The browser system prompt has three jobs: inject the persona, embed the task, and specify the response format. The persona description already carries the UX behavioral heuristics — adding a separate Nielsen rules block would be redundant. The one unique instruction for browser mode is "respond with `done` the moment the goal is satisfied" — agents need explicit permission to stop, otherwise they keep exploring past completion.

```python
_BROWSER_SYSTEM = """\
You are a synthetic user in a UX test.

You are playing the role of: {label}
{description}

You are browsing a real website. At each step you see a screenshot of the current page and its accessibility tree.
Your goal: {task}

Take ONE action per step. The moment the goal is satisfied — the target content is visible or you have arrived at the right page — respond with done immediately. Do not continue exploring after the task is complete.

Respond in JSON:
{{
    "thinking": "what you see and what to do next",
    "action": "{actions}",
    "selector": "text= or CSS selector for click/type/hover/select_option; empty for scroll/press_key/done/give_up",
    "text": "text to type; key name for press_key (Enter Tab Escape ArrowDown Space); option value for select_option; pixels to scroll for scroll (e.g. '500', '1000', '-300'); empty scrolls one full viewport height; else empty",
    "friction_observed": ["list any confusion or friction you experienced this step"],
    "success": "true or false for done/give_up; null for all other actions"
}}

Prefer text= selectors (e.g. text=Sign Up) over CSS class selectors — they survive layout changes.
When an element might reveal a dropdown on hover, use hover before click.
Return ONLY valid JSON. No markdown, no preamble."""
```

```python
def _build_browser_system_prompt(user_type: UserType, task: str) -> str:
    actions_str = " | ".join(BROWSER_ACTIONS)
    return _BROWSER_SYSTEM.format(
        label=user_type.label,
        description=user_type.description,
        task=task,
        actions=actions_str,
    )
```

### `_build_browser_user_prompt(step, max_steps, current_url, actions_taken, accessibility_tree) -> str`

The per-step user message carries everything that changes between steps: step counter, current URL, rolling action history (last `ACTION_HISTORY_LIMIT` actions), and the accessibility tree. The task does not appear here — it lives in the system prompt.

```python
def _build_browser_user_prompt(
    step: int,
    max_steps: int,
    current_url: str,
    actions_taken: list[str],
    accessibility_tree: str,
) -> str:
    parts = [f"Step {step}/{max_steps}. URL: {current_url}"]

    if accessibility_tree:
        parts.append(f"Accessibility tree:\n{accessibility_tree}")

    recent = actions_taken[-ACTION_HISTORY_LIMIT:]
    if recent:
        history = "\n".join(f"  {a}" for a in recent)
        parts.append(f"Recent actions:\n{history}")

    parts.append("What do you do next?")
    return "\n\n".join(parts)
```

### `_get_accessibility_snapshot(page: Page) -> str`

`page.accessibility.snapshot()` returns the browser's full accessibility tree as a nested dict. Serialized as-is it easily exceeds 20k characters. Extract only interactive elements and truncate.

```python
async def _get_accessibility_snapshot(page: Page) -> str:
    _INTERACTIVE_ROLES = {
        "button", "link", "textbox", "combobox", "checkbox", "radio",
        "menuitem", "tab", "option", "searchbox", "spinbutton",
    }

    def _extract_interactive(node: dict, depth: int = 0) -> list[str]:
        if depth > 8:
            return []
        lines: list[str] = []
        role = node.get("role", "")
        name = node.get("name", "")
        if role in _INTERACTIVE_ROLES and name:
            lines.append(f"{role}: {name!r}")
        for child in node.get("children", []):
            lines.extend(_extract_interactive(child, depth + 1))
        return lines

    try:
        snapshot = await page.accessibility.snapshot()
        if not snapshot:
            return ""
        lines = _extract_interactive(snapshot)
        text = "\n".join(lines)
        if len(text) > ACCESSIBILITY_TREE_CHAR_LIMIT:
            text = text[:ACCESSIBILITY_TREE_CHAR_LIMIT] + "\n… (truncated)"
        return text
    except Exception:
        return ""
```

### `_follow_page_after_click(browser_context, page, page_count_before) -> Page`

Detects new tabs opened by a click and switches focus to the new tab. Without this, the agent keeps acting on the original tab after a link opens in a new window.

```python
async def _follow_page_after_click(
    browser_context: BrowserContext,
    page: Page,
    page_count_before: int,
) -> Page:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=POST_CLICK_WAIT_MS)
    except Exception:
        pass

    pages_after = browser_context.pages
    if len(pages_after) > page_count_before:
        new_tab = pages_after[-1]
        try:
            await new_tab.wait_for_load_state("domcontentloaded", timeout=NEW_TAB_TIMEOUT_MS)
        except Exception:
            pass
        return new_tab

    try:
        await page.wait_for_load_state("domcontentloaded", timeout=ELEMENT_TIMEOUT_MS)
    except Exception:
        pass
    return page
```

### `run_browser_agent`

Public entry point. One agent, one browser context, N steps.

Signature:

```python
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
```

`llm_semaphore` comes from the orchestrator. `on_step(status, detail, step_number)` handles display updates — the agent never touches `Live` or any display object.

Full implementation:

```python
async def run_browser_agent(...) -> tuple[AgentResult, int, int, float]:
    actions_taken: list[str] = []
    urls_visited: list[str] = []
    all_friction: list[str] = []
    completed = False
    abandoned = False
    abandonment_reason: str | None = None
    comment = ""
    total_in_tok = 0
    total_out_tok = 0
    total_cost = 0.0
    steps_taken = 0
    start_time = time.monotonic()

    def _update(status: str, detail: str, step: int) -> None:
        if on_step:
            on_step(status, detail, step)

    system_prompt = _build_browser_system_prompt(user_type, task)
    browser_context = await browser.new_context(
        viewport={"width": viewport, "height": VIEWPORT_HEIGHT_PX}
    )

    try:
        page = await browser_context.new_page()
        _update("navigating", url, 0)
        await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)
        try:
            await page.wait_for_load_state("networkidle", timeout=INITIAL_NETWORKIDLE_WAIT_MS)
        except Exception:
            pass
        urls_visited.append(page.url)

        for step_index in range(max_steps):
            steps_taken = step_index + 1
            _update("scanning", f"step {steps_taken}/{max_steps}", steps_taken)

            screenshot_bytes = await page.screenshot(full_page=False)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
            accessibility_tree = await _get_accessibility_snapshot(page)

            user_prompt = _build_browser_user_prompt(
                step=steps_taken,
                max_steps=max_steps,
                current_url=page.url,
                actions_taken=actions_taken,
                accessibility_tree=accessibility_tree,
            )

            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"},
                        },
                    ],
                },
            ]

            async with llm_semaphore:
                response = await _call_with_retry(model, messages)

            usage = cast(Usage, getattr(response, "usage"))
            total_in_tok += usage.prompt_tokens
            total_out_tok += usage.completion_tokens
            try:
                total_cost += completion_cost(completion_response=response, model=model)
            except Exception:
                pass

            raw = response.choices[0].message.content or "{}"

            try:
                step_data = BrowserStep.model_validate_json(raw)
            except Exception:
                all_friction.append("Agent response was not valid JSON")
                continue

            all_friction.extend(step_data.friction_observed)

            action = step_data.action
            selector = step_data.selector
            text_value = step_data.text
            thinking = step_data.thinking

            action_label = f"{action}: {selector or text_value or '(none)'}"
            actions_taken.append(action_label)

            if action not in ALL_BROWSER_ACTIONS:
                all_friction.append(f"Unrecognised action: {action!r}")
                continue

            if action in BROWSER_TERMINAL_ACTIONS:
                if action == "done" or step_data.success is True:
                    completed = True
                    comment = thinking
                    _update("complete", thinking[:120], steps_taken)
                else:
                    abandoned = True
                    abandonment_reason = thinking
                    comment = thinking
                    _update("failed", thinking[:120], steps_taken)
                break

            _update("acting", action_label, steps_taken)

            try:
                if action == "click":
                    page_count_before = len(browser_context.pages)
                    try:
                        await page.locator(selector).first.click(timeout=ELEMENT_TIMEOUT_MS)
                    except Exception:
                        await page.click(selector, timeout=ELEMENT_TIMEOUT_MS)
                    page = await _follow_page_after_click(browser_context, page, page_count_before)

                elif action == "type":
                    await page.locator(selector).first.fill(text_value)

                elif action == "scroll":
                    try:
                        px = int(text_value)
                    except (ValueError, TypeError):
                        px = None
                    if px is not None:
                        await page.evaluate(f"window.scrollBy(0, {px})")
                    else:
                        await page.evaluate("window.scrollBy(0, window.innerHeight)")

                elif action == "hover":
                    await page.locator(selector).first.hover(timeout=ELEMENT_TIMEOUT_MS)

                elif action == "press_key":
                    await page.keyboard.press(text_value)

                elif action == "select_option":
                    await page.locator(selector).first.select_option(text_value)

            except Exception as action_error:
                all_friction.append(f"Action failed: {action} {selector!r} — {action_error}")

            current_url = page.url
            if current_url not in urls_visited:
                urls_visited.append(current_url)

    except asyncio.CancelledError:
        _update("failed", "cancelled", steps_taken)
        raise
    except Exception as unexpected_error:
        all_friction.append(str(unexpected_error))
        _update("failed", str(unexpected_error)[:80], steps_taken)
    finally:
        await browser_context.close()

    duration = round(time.monotonic() - start_time, 2)

    agent_result = AgentResult(
        agent_index=agent_index,
        user_type=user_type.label,
        completed=completed,
        abandoned=abandoned,
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
```

**Key details:**

- `llm_semaphore` is held only during the `acompletion` call — agents navigate and execute actions concurrently.
- `CancelledError` is always re-raised.
- Action failures append to `all_friction` and continue to the next step.
- `_call_with_retry` from `agent.py` handles `RateLimitError` with exponential backoff and jitter — no duplication.
- `browser_context.close()` is in `finally` — always runs.
- `comment` is set to the terminal step's `thinking` — the agent's explanation of why it succeeded or gave up.

---

## Phase 3 — `src/ux_swarm/swarm.py`

### Add `run_browser_swarm`

```python
async def run_browser_swarm(
    url: str,
    task: str,
    users: list[UserType],
    num_agents: int,
    model: str,
    max_concurrent: int,
    max_steps: int,
    viewport: int = 1280,
    headed: bool = False,
    on_agent_done: Callable[[int, int, AgentResult | None], None] | None = None,
    on_agent_step: Callable[[int, str, str, int], None] | None = None,
) -> SwarmResult:
    """Run N concurrent browser agents and return aggregated results."""
    assigned = distribute_users(users, num_agents)

    browser_sem = asyncio.Semaphore(max_concurrent)
    llm_sem = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)

    results: list[AgentResult] = []
    completed_count = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=not headed)

        async def _run_agent(idx: int, user_type: UserType) -> None:
            nonlocal completed_count
            agent_result: AgentResult | None = None
            try:
                def on_step(status: str, detail: str, step: int) -> None:
                    if on_agent_step:
                        on_agent_step(idx, status, detail, step)

                async with browser_sem:
                    agent_result, _, _, _ = await run_browser_agent(
                        browser=browser,
                        url=url,
                        task=task,
                        user_type=user_type,
                        model=model,
                        llm_semaphore=llm_sem,
                        max_steps=max_steps,
                        agent_index=idx,
                        viewport=viewport,
                        on_step=on_step,
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            finally:
                completed_count += 1
                if on_agent_done:
                    on_agent_done(completed_count, num_agents, agent_result)
                if agent_result is not None:
                    results.append(agent_result)

        async with asyncio.TaskGroup() as tg:
            for idx, user_type in enumerate(assigned):
                tg.create_task(_run_agent(idx, user_type))
                if idx < max_concurrent:
                    await asyncio.sleep(0.3)

        await browser.close()

    raw_friction = [fp for r in results for fp in r.friction_points]
    consolidated_friction, consolidation_cost = await _consolidate_friction_points(
        raw_friction, model
    )
    return _aggregate_browser(
        results, url, task, model, num_agents, consolidated_friction, consolidation_cost
    )
```

**Two semaphores:** `browser_sem` limits open Playwright contexts (memory bound). `llm_sem` limits concurrent LLM API calls (rate limit bound). An agent holds a browser context for its full lifetime but holds the LLM slot only during a single `acompletion` call per step — the limits are independent.

**TaskGroup over gather:** `_run_agent` catches all `Exception` except `CancelledError`, so individual agent failures are dropped silently and the TaskGroup never sees an unhandled exception. Programming errors still propagate and cancel siblings.

**Staggered launch:** The first `max_concurrent` task creations are separated by `asyncio.sleep(0.3)`, spreading the initial page-load + first LLM call burst across ~1.5 seconds.

### Add `_aggregate_browser`

```python
def _aggregate_browser(
    results: list[AgentResult],
    target: str,
    task: str,
    model: str,
    num_agents: int,
    friction_points: list[str] | None = None,
    extra_cost: float = 0.0,
) -> SwarmResult:
    n = len(results)

    if n == 0:
        raise CliError(
            f"All {num_agents} agents failed. Check your API key, model, and whether Chromium is installed."
        )

    completion_rate = sum(1 for r in results if r.completed) / n
    moe = 1.96 * math.sqrt(completion_rate * (1 - completion_rate) / n) if n > 1 else 0.0

    by_label: dict[str, list[bool]] = {}
    for r in results:
        by_label.setdefault(r.user_type, []).append(r.completed)
    user_breakdown = {
        label: sum(outcomes) / len(outcomes)
        for label, outcomes in by_label.items()
    }

    successful_steps = [r.steps_taken for r in results if r.completed]
    avg_steps = sum(successful_steps) / len(successful_steps) if successful_steps else 0.0

    if friction_points is None:
        friction_points = [fp for r in results for fp in r.friction_points]

    total_cost = sum(r.cost for r in results) + extra_cost
    model_id = model.split("/", 1)[-1] if "/" in model else model

    return SwarmResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        mode="browser",
        target=target,
        task=task,
        model=model_id,
        users=n,
        completion_rate=completion_rate,
        margin_of_error=moe,
        user_breakdown=user_breakdown,
        friction_points=friction_points,
        total_cost=total_cost,
        individual_results=results,
        avg_steps_to_completion=avg_steps,
    )
```

`avg_steps_to_completion` uses only successful agents — failed agents exhaust `max_steps`, which would drag the average down and misrepresent actual navigation complexity.

---

## Phase 4 — `src/ux_swarm/main.py`

### Update `RUN_DEFAULTS`

```python
RUN_DEFAULTS: dict[str, int] = {
    "default_users": 20,
    "max_steps": 8,             # raised from 3; 3 exhausts before most real flows complete
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 5,
}
```

`max_steps` raised from 3 to 8. At 3 steps, a sign-up flow (landing → click sign up → fill email → fill password → submit) exhausts the budget before completion. Completion rate at `max_steps=3` measures the number of clicks available, not UX quality. 8 covers the majority of real tasks.

### Add imports

```python
from ux_swarm.swarm import run_browser_swarm, run_screenshot_swarm
```

### Add `--headed` flag to `run` command

```python
@cli.command(hidden=True)
@click.argument("target")
@click.argument("task")
@click.option("--users", ...)
@click.option("--max-steps", ...)
@click.option("--viewport", ...)
@click.option("--verbose", is_flag=True, ...)
@click.option("--headed", is_flag=True, help="Show browser window during run (browser mode only)")
@click.pass_context
def run(ctx, target, task, users, max_steps, viewport, verbose, headed):
```

### Route URLs to browser swarm

Replace the current `CliError` stub for URL targets:

```python
def run(ctx, target, task, users, max_steps, viewport, verbose, headed):
    """Run a swarm of simulated users against a URL or screenshot image."""
    is_url = target.startswith("http://") or target.startswith("https://")

    if not is_url and not Path(target).exists():
        raise CliError(f"Image not found: {target}")

    config = load_config()
    model_full = config.get("model", "")
    api_key = config.get("api_key", "")
    provider = config.get("provider", "")

    if not model_full:
        raise CliError("No model configured — run `swarm config` to set one.")
    if not api_key:
        raise CliError("No API key configured — run `swarm config` to set one.")

    _inject_api_key(provider, api_key)

    if is_url:
        _run_browser(target, task, users, max_steps, viewport, headed, verbose, model_full)
    else:
        _run_screenshot(target, task, users, verbose, model_full)
```

Extract screenshot logic into `_run_screenshot` and browser logic into `_run_browser`. `run()` stays a thin router.

### `_build_display` — shared display component

Used by both modes. When `max_steps` is `None`, the step counter column is omitted (screenshot mode). When it is an `int`, the column shows `{step}/{max_steps}` for in-progress agents and is blank for waiting/complete/failed.

```python
_STATUS_COLORS = {
    "waiting": "dim", "navigating": "cyan", "scanning": "yellow",
    "acting": "blue", "complete": "green", "failed": "red",
}

def _build_display(
    agent_labels: dict[int, str],
    agent_states: dict[int, tuple[str, int, str]],
    done_count: int,
    num_agents: int,
    max_steps: int | None = None,
) -> Group:
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column(width=20)
    table.add_column(width=12)
    if max_steps is not None:
        table.add_column(width=6, justify="right")
    table.add_column()

    for agent_id in sorted(agent_labels):
        label = agent_labels[agent_id]
        status, step, detail = agent_states.get(agent_id, ("waiting", 0, ""))
        color = _STATUS_COLORS.get(status, "white")
        is_active = status not in ("waiting", "complete", "failed")
        max_detail = max(_console.width - (42 if max_steps is not None else 36), 10)
        detail_display = detail if len(detail) <= max_detail else detail[:max_detail - 1] + "…"
        row = [
            Text.from_markup(f"[bold]{label}[/]"),
            Text(status, style=color),
        ]
        if max_steps is not None:
            row.append(f"{step}/{max_steps}" if is_active else "")
        row.append(Text(detail_display, style="dim"))
        table.add_row(*row)

    return Group(
        table,
        Text(""),
        Text.from_markup(f"[dim]{done_count}/{num_agents} agents complete[/]"),
        Text(""),
    )
```

### `_run_browser` — browser swarm execution and display

```python
def _run_browser(
    url: str,
    task: str,
    users: int | None,
    max_steps: int | None,
    viewport: int | None,
    headed: bool,
    verbose: bool,
    model_full: str,
) -> None:
    from ux_swarm.config import playwright_state
    _, chromium_ok = playwright_state()
    if not chromium_ok:
        raise CliError("Chromium is not installed — run `swarm config` to install it.")

    num_agents = users or RUN_DEFAULTS["default_users"]
    steps = max_steps or RUN_DEFAULTS["max_steps"]
    vp = viewport or RUN_DEFAULTS["viewport_width"]
    max_concurrent = RUN_DEFAULTS["max_concurrent_browser"]

    user_types = load_users()

    # Pre-assign labels before the swarm runs so on_step callbacks have them immediately.
    assigned = distribute_users(user_types, num_agents)
    label_counts: Counter[str] = Counter()
    agent_labels: dict[int, str] = {}
    for idx, ut in enumerate(assigned):
        label_counts[ut.label] += 1
        agent_labels[idx] = f"{ut.label} {label_counts[ut.label]}"
    label_counts.clear()

    _console.print()
    _console.print(f"{url}: {task}", highlight=False)
    _console.print()
    _console.print("---", highlight=False)
    _console.print()

    agent_states: dict[int, tuple[str, int, str]] = {}  # id -> (status, step, detail)
    done_count = 0

    try:
        with Live(
            _build_display(agent_labels, agent_states, done_count, num_agents, max_steps=steps),
            console=_console,
            refresh_per_second=4,
            transient=True,
        ) as live:
            def on_step(agent_id: int, status: str, detail: str, step: int) -> None:
                agent_states[agent_id] = (status, step, detail)
                live.update(_build_display(agent_labels, agent_states, done_count, num_agents, max_steps=steps))

            def on_agent_done(done: int, total: int, agent_result: AgentResult | None) -> None:
                nonlocal done_count
                done_count = done
                live.update(_build_display(agent_labels, agent_states, done_count, num_agents, max_steps=steps))

            result = asyncio.run(
                run_browser_swarm(
                    url=url,
                    task=task,
                    users=user_types,
                    num_agents=num_agents,
                    model=model_full,
                    max_concurrent=max_concurrent,
                    max_steps=steps,
                    viewport=vp,
                    headed=headed,
                    on_agent_done=on_agent_done,
                    on_agent_step=on_step,
                )
            )
    except click.ClickException:
        raise
    except Exception as exc:
        if verbose:
            raise
        raise CliError(str(exc)) from exc

    _save_result(result)
    _print_swarm_result(result)
```

**Display design:** Both modes use the same per-agent table. One row per agent: label, status (color-coded), step counter (browser only; omitted for screenshot), and the last action or comment. Screenshot agents transition from "scanning" to "complete"/"failed" in one step; browser agents cycle through "navigating" → "scanning" → "acting" over many steps. The table handles both naturally.

`_build_display(agent_labels, agent_states, done_count, num_agents, max_steps=None) -> Group` — shared by both modes. `max_steps=None` omits the step counter column (screenshot mode); an `int` shows `{step}/{max_steps}` for in-progress agents (browser mode).

Labels are pre-assigned from `distribute_users` before the swarm starts, so `on_step` callbacks have them from the first call.

### Extract `_save_result` helper

```python
def _save_result(result: SwarmResult) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else []
        existing.append(result.model_dump())
        RESULTS_JSON.write_text(json.dumps(existing, indent=2) + "\n")
    except (OSError, json.JSONDecodeError) as exc:
        _console.print(f"[dim]Warning: could not save results: {exc}[/]")
```

Both `_run_browser` and `_run_screenshot` call `_save_result(result)`.

### Extract `_run_screenshot`

Move the existing `run()` screenshot body into `_run_screenshot(...)` so `run()` stays a thin router. Replace the existing rolling deque display with the shared `_build_display(max_steps=None)` call — screenshot agents pre-assign labels the same way and fire `on_agent_done` with their `AgentResult`.

### Update `_print_swarm_result` for browser mode

Add `avg_steps_to_completion` display when `result.avg_steps_to_completion > 0`:

```python
if result.avg_steps_to_completion > 0:
    _console.print(
        f"Avg steps to completion: {result.avg_steps_to_completion:.1f}",
        highlight=False,
    )
```

This line appears after the completion rate line.

### Update `expand` command for browser mode

For browser results, print the action sequence if `actions_taken` is present:

```python
if r.actions_taken:
    for action in r.actions_taken:
        _console.print(f"[dim]  → {action}[/]", highlight=False)
```

---

## Phase 5 — Validate

### Single-agent baseline

- [ ] Run `swarm https://example.com "find the about page" --users 1` — one browser opens, navigates, produces a result
- [ ] Confirm `results.json` entry has `mode="browser"`, `individual_results[0].actions_taken` populated, `urls_visited` populated
- [ ] Confirm `swarm expand` shows action sequence

### Multi-agent run

- [ ] Run with `--users 5` — table shows 5 rows, statuses update during run
- [ ] Confirm at most 5 browser contexts open simultaneously
- [ ] Confirm `SwarmResult.users` is the count of successful agents, not `num_agents`

### New actions

- [ ] Test a site with a dropdown nav — confirm `hover` fires before `click` on menu items
- [ ] Test a form — confirm `type` fills the field correctly
- [ ] Test a native `<select>` — confirm `select_option` works
- [ ] Test a modal — confirm `press_key` with `Escape` dismisses it

### Accessibility tree

- [ ] Inspect messages sent to LLM — confirm accessibility tree appears in user turn
- [ ] Run against a JS-heavy SPA — confirm snapshot degrades gracefully (empty string, not crash)

### Step budget

- [ ] Run a multi-step flow with `--max-steps 8` — agent reaches terminal action before exhausting budget on typical tasks
- [ ] Run with `--max-steps 1` — agent takes one action and exits

### Display

- [ ] Per-agent table updates during run
- [ ] `avg_steps_to_completion` appears in final output when agents succeed
- [ ] `avg_steps_to_completion` is omitted when no agents succeed

### `--headed` flag

- [ ] Run `swarm https://example.com "..." --users 1 --headed` — browser window opens visually
- [ ] Confirm `--headed` with `--users 5` opens up to `max_concurrent` visible windows

### Error paths

- [ ] Chromium not installed → `CliError` pointing to `swarm config`
- [ ] Invalid URL (404 on navigation) → friction recorded, agent gives up gracefully
- [ ] Selector miss (LLM generates bad selector) → friction recorded, agent continues to next step
- [ ] All agents fail → `CliError` with hint about API key / Chromium
- [ ] `--verbose` on a real failure → full traceback

---

## Design Decisions

**CSS selectors over pixel coordinates.** The current `BrowserAction` uses `x: int | None, y: int | None`. LLMs cannot reliably produce accurate pixel coordinates from compressed screenshots. CSS selectors (`text=Sign Up`, `button[type=submit]`, `#nav-login`) are what Playwright is designed for, and LLMs generate them reliably from accessibility trees.

**`action: str`, not `Literal[...]`.** The valid action set is enforced at runtime in the agent loop via `ALL_BROWSER_ACTIONS`, not at the Pydantic model layer. This keeps `BrowserStep` open — if the action set evolves (new Playwright capabilities, browser-use patterns, computer-use agents), the model doesn't need to change.

**Accessibility tree + screenshot.** The screenshot tells the LLM what the page looks like; the accessibility tree tells it what's interactive and what the real names/roles are. Together they produce selector accuracy that screenshot-only agents cannot achieve.

**Browser system prompt structure.** The persona description already carries the UX behavioral heuristics — adding a separate Nielsen rules block would be redundant. The browser system prompt uniquely adds: task embedding, action mechanics, JSON response format, and the explicit "respond with `done` the moment the goal is satisfied" instruction.

**`max_steps` raised to 8.** At 3 steps, a sign-up flow exhausts the budget before completion. `completion_rate` at `max_steps=3` measures the number of clicks available, not UX quality.

**`thinking` before `action` in JSON schema.** LLMs generate JSON left-to-right. Placing `thinking` first means the model reasons through what it sees before committing to an action — chain-of-thought that produces more coherent decisions and better `friction_observed` entries.

**Friction from LLM decisions, not action errors.** The beta's `failure_reasons` collects raw Python exception strings (`click failed — timeout 5000ms`). These are technical errors, not UX observations. `BrowserStep.friction_observed` captures what the agent actually experienced: "no back button visible", "modal appeared without a close button", "three different login links with different labels". These feed into `_consolidate_friction_points` for semantic deduplication.

**`_consolidate_friction_points` reused unchanged.** The friction consolidation pass already exists in `swarm.py` and is called by `run_screenshot_swarm`. `run_browser_swarm` calls it the same way on the collected per-step `friction_observed` entries.

**Two-semaphore design.** `browser_sem` limits open Playwright contexts (memory). `llm_sem` limits concurrent API calls (rate limits). They are independent: an agent holds a browser context for its full lifetime but only holds the LLM slot for the duration of one `acompletion` call per step.

**`_call_with_retry` reused from `agent.py`.** The existing retry function handles `RateLimitError` with exponential backoff (60 → 120 → 240) and jitter. `browser_agent.py` imports and calls it directly.

**Label pre-assignment.** Agent labels are assigned before the swarm runs from the pre-computed `distribute_users` output. `on_step` callbacks fire from the first step of each agent — labels must be available before the swarm starts.

**Flexible scroll distance.** The `scroll` action uses `text_value` as pixels when provided, falling back to `window.scrollBy(0, window.innerHeight)` when empty. A fixed constant cannot cover all tasks — a long article needs larger scrolls than a modal; the LLM knows from context how far it needs to go.

**Unified display.** Both modes use the same per-agent table via `_build_display()`. The `max_steps` parameter controls whether the step counter column appears — `None` for screenshot mode, an `int` for browser mode. One implementation, two modes.

---

## Todo

### Phase 1 — `src/ux_swarm/models.py`

**Delete:**

- [ ] Delete `BrowserAction` class (currently lines 23–30)
- [ ] Delete `BrowserDecision` class (currently lines 32–39)

**Add `BrowserStep` (insert between `ScreenshotDecision` and `AgentResult`):**

- [ ] `class BrowserStep(BaseModel):` with docstring `"LLM response for one step of a browser agent."`
- [ ] `thinking: str` — comment: `# chain-of-thought before committing to an action`
- [ ] `action: str` — comment: `# expected: click | type | scroll | hover | press_key | select_option | done | give_up`
- [ ] `selector: str` — comment: `# CSS or text= selector; empty for scroll, press_key, done, give_up`
- [ ] `text: str` — comment: `# type: text to type; press_key: key name; select_option: option; scroll: px (e.g. "500"); else empty`
- [ ] `friction_observed: list[str]`
- [ ] `success: bool | None` — comment: `# True=done success, False=give_up; None for all other actions`

**Update `AgentResult` (append after `cost: float`):**

- [ ] `actions_taken: list[str] | None = None` — comment: `# browser mode: ["click: text=Sign Up", ...]`
- [ ] `urls_visited: list[str] | None = None` — comment: `# browser mode: pages navigated to`
- [ ] `duration: float | None = None` — comment: `# browser mode: wall-clock seconds`

**Update `SwarmResult` (append at end of class):**

- [ ] `avg_steps_to_completion: float = 0.0` — comment: `# browser mode; 0.0 in screenshot mode`

---

### Phase 2 — `src/ux_swarm/browser_agent.py` (new file)

**Imports:**

- [ ] `import asyncio`
- [ ] `import base64`
- [ ] `import time`
- [ ] `from collections.abc import Callable`
- [ ] `from typing import cast`
- [ ] `from playwright.async_api import Browser, BrowserContext, Page`
- [ ] `from litellm import completion_cost`
- [ ] `from litellm.types.utils import Usage`
- [ ] `from ux_swarm.agent import _call_with_retry`
- [ ] `from ux_swarm.models import AgentResult, BrowserStep, UserType`

**Constants:**

- [ ] `PAGE_LOAD_TIMEOUT_MS = 30_000` — comment: `# Playwright navigation limit; not user-perceived wait`
- [ ] `INITIAL_NETWORKIDLE_WAIT_MS = 2_000` — comment: `# cap for JS-heavy pages to settle; not a forced delay`
- [ ] `POST_CLICK_WAIT_MS = 1_500` — comment: `# wait for navigation after a click`
- [ ] `ELEMENT_TIMEOUT_MS = 5_000` — comment: `# time to locate and interact with an element`
- [ ] `NEW_TAB_TIMEOUT_MS = 10_000` — comment: `# budget for tabs opened by the page itself`
- [ ] `VIEWPORT_HEIGHT_PX = 720`
- [ ] `BROWSER_ACTIONS: tuple[str, ...] = ("click", "type", "scroll", "hover", "press_key", "select_option", "done", "give_up")`
- [ ] `BROWSER_TERMINAL_ACTIONS: frozenset[str] = frozenset({"done", "give_up"})`
- [ ] `ALL_BROWSER_ACTIONS: frozenset[str] = frozenset(BROWSER_ACTIONS)`
- [ ] `MAX_CONCURRENT_LLM_CALLS = 3` — comment: `# cross-agent LLM cap; independent of browser concurrency`
- [ ] `ACCESSIBILITY_TREE_CHAR_LIMIT = 3_000`
- [ ] `ACTION_HISTORY_LIMIT = 5`

**`_BROWSER_SYSTEM` (module-level template string):**

- [ ] Persona block: `"You are a synthetic user in a UX test.\n\nYou are playing the role of: {label}\n{description}"`
- [ ] Task block: `"\nYou are browsing a real website. At each step you see a screenshot of the current page and its accessibility tree.\nYour goal: {task}"`
- [ ] Stop instruction: `"\n\nTake ONE action per step. The moment the goal is satisfied … respond with done immediately. Do not continue exploring after the task is complete."`
- [ ] JSON block with all 6 fields: `thinking`, `action` (`{actions}`), `selector` (with empty-for guidance), `text` (with scroll pixel note), `friction_observed`, `success` (true/false/null)
- [ ] `"Prefer text= selectors (e.g. text=Sign Up) over CSS class selectors — they survive layout changes."`
- [ ] `"When an element might reveal a dropdown on hover, use hover before click."`
- [ ] `"Return ONLY valid JSON. No markdown, no preamble."`

**`_build_browser_system_prompt(user_type: UserType, task: str) -> str`:**

- [ ] `actions_str = " | ".join(BROWSER_ACTIONS)`
- [ ] Return `_BROWSER_SYSTEM.format(label=user_type.label, description=user_type.description, task=task, actions=actions_str)`

**`_build_browser_user_prompt(step, max_steps, current_url, actions_taken, accessibility_tree) -> str`:**

- [ ] `parts = [f"Step {step}/{max_steps}. URL: {current_url}"]`
- [ ] If `accessibility_tree`: `parts.append(f"Accessibility tree:\n{accessibility_tree}")`
- [ ] If `actions_taken[-ACTION_HISTORY_LIMIT:]` non-empty: `parts.append("Recent actions:\n" + "\n".join(f"  {a}" for a in recent))`
- [ ] `parts.append("What do you do next?")`
- [ ] Return `"\n\n".join(parts)`

**`_INTERACTIVE_ROLES` (module-level set):**

- [ ] `_INTERACTIVE_ROLES = {"button", "link", "textbox", "combobox", "checkbox", "radio", "menuitem", "tab", "option", "searchbox", "spinbutton"}`

**`_extract_interactive(node: dict, depth: int = 0) -> list[str]` (module-level):**

- [ ] Guard `if depth > 8: return []`
- [ ] `lines: list[str] = []`
- [ ] `role = node.get("role", "")`, `name = node.get("name", "")`
- [ ] If `role in _INTERACTIVE_ROLES and name`: `lines.append(f"{role}: {name!r}")`
- [ ] Recurse: `for child in node.get("children", []): lines.extend(_extract_interactive(child, depth + 1))`
- [ ] Return `lines`

**`_get_accessibility_snapshot(page: Page) -> str` (async):**

- [ ] Whole body in `try/except Exception: return ""`
- [ ] `snapshot = await page.accessibility.snapshot()`; if falsy return `""`
- [ ] `lines = _extract_interactive(snapshot)`
- [ ] `text = "\n".join(lines)`
- [ ] If `len(text) > ACCESSIBILITY_TREE_CHAR_LIMIT`: `text = text[:ACCESSIBILITY_TREE_CHAR_LIMIT] + "\n… (truncated)"`
- [ ] Return `text`

**`_follow_page_after_click(browser_context, page, page_count_before) -> Page` (async):**

- [ ] `try: await page.wait_for_load_state("domcontentloaded", timeout=POST_CLICK_WAIT_MS)` — `except Exception: pass`
- [ ] `pages_after = browser_context.pages`
- [ ] If `len(pages_after) > page_count_before`:
  - [ ] `new_tab = pages_after[-1]`
  - [ ] `try: await new_tab.wait_for_load_state("domcontentloaded", timeout=NEW_TAB_TIMEOUT_MS)` — `except Exception: pass`
  - [ ] Return `new_tab`
- [ ] Else: `try: await page.wait_for_load_state("domcontentloaded", timeout=ELEMENT_TIMEOUT_MS)` — `except Exception: pass`; return `page`

**`run_browser_agent(browser, url, task, user_type, model, llm_semaphore, max_steps, agent_index, viewport=1280, on_step=None) -> tuple[AgentResult, int, int, float]` (async):**

_Initialization:_

- [ ] `actions_taken: list[str] = []`, `urls_visited: list[str] = []`, `all_friction: list[str] = []`
- [ ] `completed = False`, `abandoned = False`, `abandonment_reason: str | None = None`, `comment = ""`
- [ ] `total_in_tok = 0`, `total_out_tok = 0`, `total_cost = 0.0`
- [ ] `steps_taken = 0`, `start_time = time.monotonic()`
- [ ] Inner `_update(status: str, detail: str, step: int) -> None`: calls `on_step(status, detail, step)` if `on_step` is set
- [ ] `system_prompt = _build_browser_system_prompt(user_type, task)` — built once before the loop

_Context setup (everything below wrapped in `try/except/finally`):_

- [ ] `browser_context = await browser.new_context(viewport={"width": viewport, "height": VIEWPORT_HEIGHT_PX})`

_Navigation:_

- [ ] `page = await browser_context.new_page()`
- [ ] `_update("navigating", url, 0)`
- [ ] `await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS)`
- [ ] `try: await page.wait_for_load_state("networkidle", timeout=INITIAL_NETWORKIDLE_WAIT_MS)` — `except Exception: pass`
- [ ] `urls_visited.append(page.url)`

_Step loop `for step_index in range(max_steps)`:_

- [ ] `steps_taken = step_index + 1`
- [ ] `_update("scanning", f"step {steps_taken}/{max_steps}", steps_taken)`
- [ ] `screenshot_bytes = await page.screenshot(full_page=False)`
- [ ] `screenshot_b64 = base64.b64encode(screenshot_bytes).decode()`
- [ ] `accessibility_tree = await _get_accessibility_snapshot(page)`
- [ ] `user_prompt = _build_browser_user_prompt(steps_taken, max_steps, page.url, actions_taken, accessibility_tree)`
- [ ] Build `messages`: `[{"role": "system", "content": system_prompt}, {"role": "user", "content": [{"type": "text", ...}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}}]}]`
- [ ] `async with llm_semaphore: response = await _call_with_retry(model, messages)`
- [ ] `usage = cast(Usage, getattr(response, "usage"))`; `total_in_tok += usage.prompt_tokens`; `total_out_tok += usage.completion_tokens`
- [ ] `try: total_cost += completion_cost(completion_response=response, model=model)` — `except Exception: pass`
- [ ] `raw = response.choices[0].message.content or "{}"`
- [ ] `try: step_data = BrowserStep.model_validate_json(raw)` — `except Exception: all_friction.append("Agent response was not valid JSON"); continue`
- [ ] `all_friction.extend(step_data.friction_observed)`
- [ ] Extract `action = step_data.action`, `selector = step_data.selector`, `text_value = step_data.text`, `thinking = step_data.thinking`
- [ ] `action_label = f"{action}: {selector or text_value or '(none)'}"`; `actions_taken.append(action_label)`
- [ ] If `action not in ALL_BROWSER_ACTIONS`: `all_friction.append(f"Unrecognised action: {action!r}")`; `continue`
- [ ] If `action in BROWSER_TERMINAL_ACTIONS`:
  - [ ] If `action == "done"` or `step_data.success is True`: `completed = True; comment = thinking; _update("complete", thinking[:120], steps_taken)`
  - [ ] Else: `abandoned = True; abandonment_reason = thinking; comment = thinking; _update("failed", thinking[:120], steps_taken)`
  - [ ] `break`
- [ ] `_update("acting", action_label, steps_taken)`
- [ ] Execute action in `try/except Exception as action_error` — on exception: `all_friction.append(f"Action failed: {action} {selector!r} — {action_error}")`
  - [ ] `click`: capture `page_count_before = len(browser_context.pages)`; `try: await page.locator(selector).first.click(timeout=ELEMENT_TIMEOUT_MS)` with fallback `await page.click(selector, timeout=ELEMENT_TIMEOUT_MS)`; then `page = await _follow_page_after_click(browser_context, page, page_count_before)`
  - [ ] `type`: `await page.locator(selector).first.fill(text_value)`
  - [ ] `scroll`: `try: px = int(text_value)` — `except (ValueError, TypeError): px = None`; if `px is not None`: `await page.evaluate(f"window.scrollBy(0, {px})")` else `await page.evaluate("window.scrollBy(0, window.innerHeight)")`
  - [ ] `hover`: `await page.locator(selector).first.hover(timeout=ELEMENT_TIMEOUT_MS)`
  - [ ] `press_key`: `await page.keyboard.press(text_value)`
  - [ ] `select_option`: `await page.locator(selector).first.select_option(text_value)`
- [ ] After action block (outside `try/except`): `current_url = page.url`; if `current_url not in urls_visited: urls_visited.append(current_url)`

_Error handling:_

- [ ] `except asyncio.CancelledError`: `_update("failed", "cancelled", steps_taken)`; `raise`
- [ ] `except Exception as unexpected_error`: `all_friction.append(str(unexpected_error))`; `_update("failed", str(unexpected_error)[:80], steps_taken)`
- [ ] `finally: await browser_context.close()`

_Return:_

- [ ] `duration = round(time.monotonic() - start_time, 2)`
- [ ] Build `AgentResult(agent_index=agent_index, user_type=user_type.label, completed=completed, abandoned=abandoned, abandonment_reason=abandonment_reason, friction_points=all_friction, comment=comment, steps_taken=steps_taken, input_tokens=total_in_tok, output_tokens=total_out_tok, cost=total_cost, actions_taken=actions_taken, urls_visited=urls_visited, duration=duration)`
- [ ] Return `agent_result, total_in_tok, total_out_tok, total_cost`

---

### Phase 3 — `src/ux_swarm/swarm.py`

**New imports (add to existing import block at top of file):**

- [ ] `from playwright.async_api import async_playwright`
- [ ] `from ux_swarm.browser_agent import MAX_CONCURRENT_LLM_CALLS, run_browser_agent`

**`run_browser_swarm(url, task, users, num_agents, model, max_concurrent, max_steps, viewport=1280, headed=False, on_agent_done=None, on_agent_step=None) -> SwarmResult` (async):**

- [ ] `assigned = distribute_users(users, num_agents)`
- [ ] `browser_sem = asyncio.Semaphore(max_concurrent)`
- [ ] `llm_sem = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)`
- [ ] `results: list[AgentResult] = []`, `completed_count = 0`
- [ ] Inner `_run_agent(idx: int, user_type: UserType) -> None`:
  - [ ] `nonlocal completed_count`, `agent_result: AgentResult | None = None`
  - [ ] Inner `on_step(status, detail, step)` closure: calls `on_agent_step(idx, status, detail, step)` if set
  - [ ] `try: async with browser_sem: agent_result, _, _, _ = await run_browser_agent(browser=browser, url=url, task=task, user_type=user_type, model=model, llm_semaphore=llm_sem, max_steps=max_steps, agent_index=idx, viewport=viewport, on_step=on_step)`
  - [ ] `except asyncio.CancelledError: raise`
  - [ ] `except Exception: pass`
  - [ ] `finally: completed_count += 1; if on_agent_done: on_agent_done(completed_count, num_agents, agent_result); if agent_result is not None: results.append(agent_result)`
- [ ] `async with async_playwright() as playwright:`
  - [ ] `browser = await playwright.chromium.launch(headless=not headed)`
  - [ ] `async with asyncio.TaskGroup() as tg: for idx, user_type in enumerate(assigned): tg.create_task(_run_agent(idx, user_type)); if idx < max_concurrent: await asyncio.sleep(0.3)`
  - [ ] `await browser.close()`
- [ ] `raw_friction = [fp for r in results for fp in r.friction_points]`
- [ ] `consolidated_friction, consolidation_cost = await _consolidate_friction_points(raw_friction, model)`
- [ ] Return `_aggregate_browser(results, url, task, model, num_agents, consolidated_friction, consolidation_cost)`

**`_aggregate_browser(results, target, task, model, num_agents, friction_points=None, extra_cost=0.0) -> SwarmResult`:**

- [ ] `n = len(results)`; if `n == 0`: raise `CliError(f"All {num_agents} agents failed. Check your API key, model, and whether Chromium is installed.")`
- [ ] `completion_rate = sum(1 for r in results if r.completed) / n`
- [ ] `moe = 1.96 * math.sqrt(completion_rate * (1 - completion_rate) / n) if n > 1 else 0.0`
- [ ] Build `by_label: dict[str, list[bool]]` → `user_breakdown: dict[str, float]` (same pattern as `_aggregate`)
- [ ] `successful_steps = [r.steps_taken for r in results if r.completed]`; `avg_steps = sum(successful_steps) / len(successful_steps) if successful_steps else 0.0`
- [ ] `if friction_points is None: friction_points = [fp for r in results for fp in r.friction_points]`
- [ ] `total_cost = sum(r.cost for r in results) + extra_cost`
- [ ] `model_id = model.split("/", 1)[-1] if "/" in model else model`
- [ ] Return `SwarmResult(timestamp=datetime.now(timezone.utc).isoformat(), mode="browser", target=target, task=task, model=model_id, users=n, completion_rate=completion_rate, margin_of_error=moe, user_breakdown=user_breakdown, friction_points=friction_points, total_cost=total_cost, individual_results=results, avg_steps_to_completion=avg_steps)`

---

### Phase 4 — `src/ux_swarm/main.py`

**Imports:**

- [ ] Change `from ux_swarm.swarm import run_screenshot_swarm` → `from ux_swarm.swarm import run_browser_swarm, run_screenshot_swarm`
- [ ] Add `Table` to Rich imports: `from rich.table import Table`
- [ ] Add `distribute_users` to personas import: `from ux_swarm.personas import load_users, distribute_users`

**`RUN_DEFAULTS`:**

- [ ] Change `"max_steps": 3` → `"max_steps": 8`

**Add `_STATUS_COLORS` (module-level dict, above `_print_swarm_result`):**

- [ ] `_STATUS_COLORS = {"waiting": "dim", "navigating": "cyan", "scanning": "yellow", "acting": "blue", "complete": "green", "failed": "red"}`

**Add `_build_display(agent_labels, agent_states, done_count, num_agents, max_steps=None) -> Group`:**

- [ ] `table = Table(show_header=False, box=None, padding=(0, 1))`
- [ ] `table.add_column(width=20)` — label
- [ ] `table.add_column(width=12)` — status
- [ ] If `max_steps is not None`: `table.add_column(width=6, justify="right")` — step counter (browser only)
- [ ] `table.add_column()` — detail
- [ ] `for agent_id in sorted(agent_labels):`:
  - [ ] `label = agent_labels[agent_id]`
  - [ ] `status, step, detail = agent_states.get(agent_id, ("waiting", 0, ""))`
  - [ ] `color = _STATUS_COLORS.get(status, "white")`
  - [ ] `is_active = status not in ("waiting", "complete", "failed")`
  - [ ] `max_detail = max(_console.width - (42 if max_steps is not None else 36), 10)`; truncate `detail` to `max_detail` with `…`
  - [ ] `row = [Text.from_markup(f"[bold]{label}[/]"), Text(status, style=color)]`
  - [ ] If `max_steps is not None`: append `f"{step}/{max_steps}" if is_active else ""`
  - [ ] Append `Text(detail_display, style="dim")`; `table.add_row(*row)`
- [ ] Return `Group(table, Text(""), Text.from_markup(f"[dim]{done_count}/{num_agents} agents complete[/]"), Text(""))`

**Add `_save_result(result: SwarmResult) -> None`:**

- [ ] `LOCAL_DIR.mkdir(parents=True, exist_ok=True)`
- [ ] `try:` read `RESULTS_JSON` if exists → `json.loads()` → append `result.model_dump()` → `RESULTS_JSON.write_text(json.dumps(existing, indent=2) + "\n")`
- [ ] `except (OSError, json.JSONDecodeError) as exc: _console.print(f"[dim]Warning: could not save results: {exc}[/]")`

**Add `_run_screenshot(target, task, users, verbose, model_full) -> None`:**

- [ ] `num_agents = users or RUN_DEFAULTS["default_users"]`, `max_concurrent = RUN_DEFAULTS["max_concurrent_screenshot"]`
- [ ] `user_types = load_users()`
- [ ] Pre-assign labels: `assigned = distribute_users(user_types, num_agents)`; count with `label_counts: Counter[str] = Counter()`; build `agent_labels: dict[int, str] = {}`; then `label_counts.clear()`
- [ ] Print header: `_console.print()`, `_console.print(f"{Path(target).name}: {task}", ...)`, `_console.print()`, `_console.print("---", ...)`, `_console.print()`
- [ ] `agent_states: dict[int, tuple[str, int, str]] = {}`, `done_count = 0`
- [ ] `try: with Live(_build_display(agent_labels, agent_states, 0, num_agents, max_steps=None), ..., refresh_per_second=10, transient=True) as live:`
  - [ ] `def on_done(done, total, agent_result):` — if `agent_result` is not None: `comment = agent_result.comment or agent_result.abandonment_reason or ""`; `agent_states[agent_result.agent_index] = ("complete" if agent_result.completed else "failed", 1, comment)`; `nonlocal done_count; done_count = done`; `live.update(_build_display(agent_labels, agent_states, done_count, num_agents, max_steps=None))`
  - [ ] `result = asyncio.run(run_screenshot_swarm(target=target, task=task, users=user_types, num_agents=num_agents, model=model_full, max_concurrent=max_concurrent, on_agent_done=on_done))`
- [ ] `except click.ClickException: raise`; `except Exception as exc: if verbose: raise; raise CliError(str(exc)) from exc`
- [ ] `_save_result(result)`, `_print_swarm_result(result)`

**Add `_run_browser(url, task, users, max_steps, viewport, headed, verbose, model_full) -> None`:**

- [ ] Check Chromium: `from ux_swarm.config import playwright_state; _, chromium_ok = playwright_state()`; if not `chromium_ok`: raise `CliError("Chromium is not installed — run \`swarm config\` to install it.")`
- [ ] `num_agents = users or RUN_DEFAULTS["default_users"]`; `steps = max_steps or RUN_DEFAULTS["max_steps"]`; `vp = viewport or RUN_DEFAULTS["viewport_width"]`; `max_concurrent = RUN_DEFAULTS["max_concurrent_browser"]`
- [ ] `user_types = load_users()`
- [ ] Pre-assign labels: same `distribute_users` + `label_counts` + `agent_labels` pattern; `label_counts.clear()`
- [ ] Print header: url + task, `---`
- [ ] `agent_states: dict[int, tuple[str, int, str]] = {}`, `done_count = 0`
- [ ] `try: with Live(_build_display(agent_labels, agent_states, 0, num_agents, max_steps=steps), ..., refresh_per_second=4, transient=True) as live:`
  - [ ] `def on_step(agent_id, status, detail, step):` — `agent_states[agent_id] = (status, step, detail)`; `live.update(_build_display(agent_labels, agent_states, done_count, num_agents, max_steps=steps))`
  - [ ] `def on_agent_done(done, total, agent_result):` — `nonlocal done_count; done_count = done`; `live.update(_build_display(...))`
  - [ ] `result = asyncio.run(run_browser_swarm(url=url, task=task, users=user_types, num_agents=num_agents, model=model_full, max_concurrent=max_concurrent, max_steps=steps, viewport=vp, headed=headed, on_agent_done=on_agent_done, on_agent_step=on_step))`
- [ ] `except click.ClickException: raise`; `except Exception as exc: if verbose: raise; raise CliError(str(exc)) from exc`
- [ ] `_save_result(result)`, `_print_swarm_result(result)`

**Update `run()` command:**

- [ ] Add `@click.option("--headed", is_flag=True, help="Show browser window during run (browser mode only)")` decorator
- [ ] Add `headed` to function signature
- [ ] Replace URL `CliError` stub and screenshot-only guard with: `is_url = target.startswith("http://") or target.startswith("https://")`; `if not is_url and not Path(target).exists(): raise CliError(f"Image not found: {target}")`
- [ ] After config load + `_inject_api_key`: `if is_url: _run_browser(target, task, users, max_steps, viewport, headed, verbose, model_full)` else `_run_screenshot(target, task, users, verbose, model_full)`
- [ ] Remove the now-dead screenshot body (deque, `label_counts`, `_build_display`, `on_done`, `asyncio.run(run_screenshot_swarm(...))`, `_save_result`, `_print_swarm_result`) — all of it moves into `_run_screenshot`

**Update `_print_swarm_result`:**

- [ ] After `_console.print(f"{rate_pct} of agents completed the task:", ...)`: add `if result.avg_steps_to_completion > 0: _console.print(f"Avg steps to completion: {result.avg_steps_to_completion:.1f}", highlight=False)`

**Update `expand` command:**

- [ ] After printing `comment` for each agent: `if r.actions_taken: for action in r.actions_taken: _console.print(f"[dim]  → {action}[/]", highlight=False)`

---

### Phase 5 — Validate

**Smoke test — single agent:**

- [ ] `swarm https://example.com "find the about page" --users 1`
- [ ] Agent navigates, takes actions, reaches `done` or `give_up` within 8 steps
- [ ] `results.json` has new entry: `mode="browser"`, `actions_taken` non-empty list, `urls_visited` contains at least the start URL, `duration` is a positive float
- [ ] `swarm expand` shows `→` action bullets under the agent row

**Display — per-agent table (both modes):**

- [ ] Screenshot mode: `swarm path/to/image.png "task" --users 5` — per-agent table renders; agents flip from "waiting" to "complete"/"failed" with comment in detail column; no rolling deque
- [ ] Browser mode: `swarm https://... "task" --users 5` — table shows status cycling through colors; step counter updates in real time; detail shows current URL or action label
- [ ] `done_count/num_agents` line at bottom increments as agents finish

**Concurrency:**

- [ ] `--users 5`: exactly 5 rows in table; browser contexts capped at `max_concurrent=5`
- [ ] `--users 20`: 20 rows; staggered launch visible — rows activate at ~0.3s intervals for the first 5

**Actions — each exercised manually:**

- [ ] `click` — agent clicks a link; URL changes; `urls_visited` updated
- [ ] `type` — agent fills a text field; value appears in input
- [ ] `scroll` with `text_value = "800"` — page scrolls 800px
- [ ] `scroll` with empty `text_value` — page scrolls one `window.innerHeight`
- [ ] `hover` — agent hovers a nav item; dropdown appears in next screenshot
- [ ] `press_key` with `"Enter"` — form submits
- [ ] `select_option` — `<select>` value changes

**Accessibility tree:**

- [ ] Temporarily add `print(accessibility_tree)` inside the step loop; run `--users 1`; confirm interactive elements (buttons, links, textboxes) appear in output
- [ ] Remove the debug print
- [ ] Run against a JS-heavy SPA; confirm snapshot returns empty string or truncated text, never crashes

**New tab handling:**

- [ ] Navigate to a page with a `target="_blank"` link; confirm agent follows to new tab URL; `urls_visited` contains the new tab URL

**Step budget:**

- [ ] `--max-steps 1`: agent takes exactly one action and exits
- [ ] `--max-steps 8`: multi-step flow (sign-up or search) reaches `done` before budget exhausted

**`--headed` flag:**

- [ ] `--users 1 --headed`: Chromium window opens and is visible throughout the run
- [ ] `--users 5 --headed`: up to 5 browser windows open

**`avg_steps_to_completion`:**

- [ ] Line appears in final output after completion rate when at least one agent succeeded
- [ ] Absent when `avg_steps_to_completion == 0.0` (all failed)
- [ ] Value is the average of `steps_taken` for successful agents only — not failed agents

**Error paths:**

- [ ] Chromium not installed → `CliError`: `"Chromium is not installed — run \`swarm config\` to install it."`
- [ ] URL returning 404 → agent sees error page, calls `give_up`; no crash; friction recorded
- [ ] LLM returns selector that doesn't exist → `action_error` caught; `"Action failed: click …"` in friction; agent continues to next step
- [ ] LLM returns invalid JSON → `model_validate_json` raises; `"Agent response was not valid JSON"` in friction; step skipped via `continue`
- [ ] All agents fail (return `None`) → `_aggregate_browser` raises `CliError` with Chromium/API key hint
- [ ] `--verbose` on real exception → full Python traceback

**Screenshot mode regression:**

- [ ] `swarm path/to/image.png "task"` still completes; result saved with `mode="screenshot"`, `avg_steps_to_completion=0.0`
- [ ] `swarm results` and `swarm expand` still render screenshot entries correctly
