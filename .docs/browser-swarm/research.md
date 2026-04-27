# Research: Browser Swarm Beta

_Source: `masonomara/ux-swarm--beta` — `swarm/browser_swarm.py`, `swarm/types.py`, `swarm/prompts.py`, `swarm/utils.py`, `swarm/runners.py`, `swarm/output.py`, `swarm/personas.py`, `swarm/cli.py`_

---

## Overview

The beta has a fully working browser swarm implementation. The core loop — navigate → screenshot → LLM decision → Playwright action → repeat — runs correctly and produces usable results. The architecture is close to what the current codebase needs, but several design decisions need to be improved or corrected before porting.

---

## What the Beta Does Well

### The Browser Agent Loop

`run_browser_agent` is the strongest part of the beta. The loop structure is clean:

1. Creates an isolated `browser_context` per agent (one browser, N contexts — cheap isolation)
2. Navigates to URL, waits for `domcontentloaded`, then attempts `networkidle` with a timeout cap (not a forced delay)
3. Builds the system prompt once — task lives in the system prompt, not per-step
4. Screenshots the viewport only (`full_page=False`) — offscreen content is unreachable anyway
5. Calls the LLM with: step counter, current URL, rolling 5-action history, current screenshot
6. Parses JSON, executes the action, loops

The `finally: await browser_context.close()` ensures contexts are always cleaned up even on errors. `CancelledError` is re-raised correctly. Action failures (a click that misses) are caught per-step as errors, logged, and execution continues — the agent doesn't abort on a single bad action.

### Action Vocabulary

Five actions: `click`, `type`, `scroll`, `done`, `give_up`. `done` and `give_up` are terminal. This is a clean, minimal vocabulary with clear semantics:

```json
{
    "thinking": "I can see a Sign Up button in the top right corner",
    "action": "click",
    "selector": "text=Sign Up",
    "text": "",
    "success": null
}
```

The `thinking` field is important — it gives the LLM space to reason through what it sees before committing to an action. Without it, the LLM outputs an action without showing its work, which makes it harder to debug unexpected decisions.

Actions are checked against `ALL_BROWSER_ACTIONS = frozenset({"click", "type", "scroll", "done", "give_up"})` before execution. Unrecognized actions log an error and skip rather than crash.

### CSS Selectors, Not Pixel Coordinates

The beta uses CSS selectors (`selector: str`) rather than pixel coordinates (x, y). This is the right design:

- LLMs reliably generate `text=Sign Up` or `button[type=submit]`
- CSS selectors survive responsive layout changes; pixel coordinates drift
- Playwright's locator API (`page.locator(selector).first.click()`) maps directly to selectors
- Pixel coordinate targeting would require the LLM to understand screen resolution, DPI scaling, and exact bounding boxes from a compressed image

**This is a direct conflict with the current `models.py`.** The existing `BrowserAction` model uses `x: int | None` and `y: int | None`. These need to change to `selector: str` before building the browser agent.

### Click Fallback

```python
try:
    await page.locator(selector).first.click(timeout=ELEMENT_SELECTION_TIMEOUT_MS)
except Exception:
    await page.click(selector, timeout=ELEMENT_SELECTION_TIMEOUT_MS)
```

The `.locator()` API and `page.click()` use different resolution paths. The fallback handles pages where the locator API fails but `page.click()` succeeds. Worth keeping.

### New Tab Handling

`_follow_page_after_click` detects when a click opens a new tab (by comparing `len(browser_context.pages)` before and after) and switches focus to the new tab. Without this, the agent would keep acting on the original tab after a new one opened.

### Multiple Page Load Strategies

```python
await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
try:
    await page.wait_for_load_state("networkidle", timeout=2_000)
except Exception:
    pass
```

`domcontentloaded` is required for goto — the agent can't proceed without the basic DOM. `networkidle` is optional with a short cap: on JS-heavy SPAs it settles the page without forcing a 2-second delay if it doesn't resolve.

### Task in System Prompt

```python
system_prompt = build_browser_system_prompt(
    persona_prompt=persona.prompt,
    task=task,
)
```

The task is embedded in the system prompt, not the per-step user turn. Since the task is constant across all steps, keeping it in the system prompt means it stays in context as the conversation history grows. Per-step user turns only carry: step counter, current URL, action history, and the screenshot.

### Rolling Action History

The per-step user prompt includes the last 5 actions the agent has taken:

```
Recent actions:
  click: text=Sign Up
  type: input#email
  click: button[type=submit]
```

This gives the LLM enough context to avoid repeating itself or getting stuck in a loop. Capping at 5 prevents context window growth from becoming unbounded.

### Two-Semaphore Architecture

The beta correctly separates two concurrency limits:

- `browser_concurrency_semaphore` — how many agents can hold open browser contexts simultaneously (default: 5)
- `_llm_semaphore` — how many LLM calls can be in flight simultaneously (default: 3)

These are independent because an agent holds a browser context for its entire lifetime (multiple steps) while making only one LLM call per step. Capping browsers at 5 prevents RAM exhaustion; capping LLM calls at 3 prevents rate limit saturation.

### stderr/stdout Separation

```python
console = Console(stderr=True)  # progress, warnings
out = Console()                  # structured results
```

Progress output goes to stderr so results can be piped or redirected cleanly. A user who does `swarm https://example.com "..." > results.txt` gets only the final output in the file.

### Per-Agent Duration Tracking

```python
start_time = time.monotonic()
# ...
duration_seconds = time.monotonic() - start_time
```

The beta records how long each agent takes. Useful for diagnosing slow pages or timeouts.

---

## What the Beta Gets Wrong

### Fixed 60s Retry, No Jitter

```python
await asyncio.sleep(60)  # Typical provider reset window
```

Fixed sleep with no jitter. When multiple agents hit a rate limit simultaneously and all wake up after exactly 60 seconds, they retry at the same moment — the thundering herd problem. The current codebase already has the right fix: exponential backoff (60 → 120 → 240) with `random.uniform(0, delay * 0.5)` jitter.

### `asyncio.gather` Instead of `asyncio.TaskGroup`

```python
await asyncio.gather(*[
    run_single_agent(index, persona)
    for index, persona in enumerate(agent_assignments)
])
```

`gather` with no `return_exceptions` means an unhandled exception in any task will cancel siblings without cleanup. The current codebase uses `asyncio.TaskGroup` with per-task exception catching, which is strictly safer: individual agent failures are silently dropped, but programming errors (not caught inside the task) still propagate correctly and cancel siblings.

### Global Lazy Semaphore

```python
_llm_semaphore: asyncio.Semaphore | None = None

def _get_llm_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)
    return _llm_semaphore
```

Module-level global with lazy initialization. This is fragile: if anything calls `_get_llm_semaphore()` from a context where no event loop is running, it may bind to the wrong loop. The clean pattern (used in the current codebase's screenshot swarm) is to create the semaphore at the start of the async function and pass it in.

### `BrowserAgentResult` as TypedDict

```python
class BrowserAgentResult(TypedDict):
    persona: str
    success: bool
    steps: int
    ...
```

`TypedDict` has no validation and no `.model_dump()`. It can't be passed to `json.dumps` cleanly without `default=str`. The rest of the codebase uses Pydantic `BaseModel` for all data shapes. `BrowserAgentResult` should be a `BaseModel`.

### No Friction Points from LLM Decisions

The beta's `failure_reasons` are raw Python exception strings from `result["errors"]`:

```python
failure_error_strings: list[str] = []
for result in results:
    if not result["success"]:
        stripped_errors = [step_prefix_re.sub("", error) for error in result["errors"]]
        failure_error_strings.extend(stripped_errors)
```

These are technical errors (Playwright timeouts, selector failures), not UX friction observations. The current codebase's screenshot swarm has `_consolidate_friction_points()` — an LLM-based semantic clustering pass that merges similar observations into canonical phrases. Browser mode should do the same, drawing friction from the `BrowserDecision.friction_observed` field rather than from action errors.

### State/Live Display Coupled Into Agent

The browser agent receives `state: SwarmState | None` and `live: Live | None` directly:

```python
async def run_browser_agent(
    ...
    state: SwarmState | None = None,
    live: Live | None = None,
) -> BrowserAgentResult:
```

And calls `live.update(...)` directly inside the agent. This couples display logic to the agent. The current codebase's pattern — `on_agent_done` callback passed to the swarm orchestrator — keeps agents output-agnostic. The browser agent should follow the same pattern: the swarm passes an `on_step` or `on_done` callback, not a `Live` reference.

### No Staggered Launch

All tasks are created simultaneously in `asyncio.gather`. They block on the semaphore, but creating N tasks at once still causes a burst of resource allocation. The current codebase staggers the first `max_concurrent` task creations with `asyncio.sleep(0.3)`, spreading the initial burst across ~1.5 seconds.

### SmartGroup Heuristic Too Broad

```python
@staticmethod
def _first_arg_looks_like_url(arg: str) -> bool:
    return arg.startswith(("http://", "https://")) or "." in arg
```

The `"." in arg` catch is too aggressive — it would rewrite `swarm my.config "task"` as `swarm run my.config "task"`. The current codebase's pattern (explicit URL prefix check + known image extensions) is more precise.

---

## Prompt Design Details

### System Prompt Structure

```
[persona description]

[task framing with embedded task]

[JSON response format spec]
```

The production rules / UX heuristics are included in screenshot mode but **not** in browser mode. Browser mode focuses on task completion rather than UX evaluation. This is a valid design choice: browser agents are trying to succeed, not to critique. Friction is surfaced through failure, not through explicit critique fields.

### Response Format

```json
{
    "thinking": "what you see and what to do",
    "action": "click|type|scroll|done|give_up",
    "selector": "CSS selector or text= selector",
    "text": "text to type (type action only)",
    "success": "true or false (done/give_up only)"
}
```

`thinking` comes before `action` in the JSON schema, which matters: LLMs generate JSON left-to-right, so putting chain-of-thought first means the model reasons before committing to an action.

The `success` field on `done`/`give_up` is redundant (done is always success, give_up is always failure) but it makes the LLM explicitly declare its judgment, which produces more coherent `thinking` text.

---

## What to Adopt Verbatim

| Component | Where it is | What to take |
|---|---|---|
| Action vocabulary | `types.py` | `click/type/scroll/done/give_up`, `BROWSER_TERMINAL_ACTIONS` |
| CSS selector interface | `browser_swarm.py` | `selector: str` field, not `x: int, y: int` |
| `thinking` field | `prompts.py` | Chain-of-thought before action |
| Task in system prompt | `prompts.py` | `BROWSER_TASK_FRAMING.format(task=task)` in system |
| Rolling action history | `prompts.py` | Last 5 actions in per-step user turn |
| `_follow_page_after_click` | `browser_swarm.py` | New tab detection and focus switch |
| Viewport-only screenshot | `browser_swarm.py` | `full_page=False` |
| Multiple load waits | `browser_swarm.py` | `domcontentloaded` required + `networkidle` capped |
| Click fallback | `browser_swarm.py` | Try locator first, then `page.click()` |
| `.first` on locators | `browser_swarm.py` | `page.locator(selector).first` |
| `SCROLL_DISTANCE_PX = 500` | `browser_swarm.py` | Fixed scroll distance constant |
| Timeout constants | `browser_swarm.py` | `PAGE_LOAD_TIMEOUT_MS`, `ELEMENT_SELECTION_TIMEOUT_MS`, etc. |
| Duration tracking | `browser_swarm.py` | `time.monotonic()` start/end per agent |
| Two-semaphore design | `browser_swarm.py` | Browser sem + LLM sem, both created inside async context |
| `async with async_playwright()` scope | `browser_swarm.py` | Keep playwright context open for all browser operations |
| One browser, N contexts | `browser_swarm.py` | `browser.new_context()` per agent, not `playwright.chromium.launch()` per agent |

---

## What to Improve Over the Beta

| Issue | Beta behavior | What to do instead |
|---|---|---|
| Rate limit retry | Fixed 60s sleep | 60 → 120 → 240 with jitter (current codebase already has this) |
| Task concurrency | `asyncio.gather` | `asyncio.TaskGroup` with per-task exception catching (current codebase) |
| Semaphore initialization | Global lazy module-level | Create inside the async swarm function, pass to agents |
| Agent result model | `TypedDict` | Pydantic `BaseModel` (consistent with rest of codebase) |
| Friction points | Raw Python exception strings | LLM friction observations from `BrowserDecision.friction_observed` |
| Display coupling | `Live` passed into agent | `on_step` callback only; agent stays output-agnostic |
| Agent launch | All at once, semaphore blocks | Stagger first `max_concurrent` with `asyncio.sleep(0.3)` |
| SmartGroup heuristic | `"." in arg` | Explicit URL prefix + known image extensions |

---

## Model Changes Needed

The current `models.py` `BrowserAction` must be revised before any browser agent code is written:

**Current (wrong):**
```python
class BrowserAction(BaseModel):
    action: str
    x: int | None       # pixel coordinate — wrong
    y: int | None       # pixel coordinate — wrong
    value: str | None   # vague field name
    reasoning: str      # no chain-of-thought before action
```

**Should be:**
```python
class BrowserAction(BaseModel):
    thinking: str           # chain-of-thought before committing
    action: str             # click | type | scroll | done | give_up
    selector: str           # CSS selector or text= selector; empty for scroll/done/give_up
    text: str               # text to type; empty for non-type actions
    success: bool | None    # only populated for done/give_up; None otherwise
```

`BrowserDecision` wraps `BrowserAction` with aggregated fields. In browser mode, `BrowserDecision` may not be needed as a separate model — the action IS the decision. Consider collapsing `BrowserAction` and `BrowserDecision` into a single `BrowserStep` model:

```python
class BrowserStep(BaseModel):
    thinking: str
    action: str
    selector: str
    text: str
    success: bool | None
    friction_observed: list[str]  # what confused the agent this step
```

`friction_observed` at the step level captures real UX observations — "I couldn't find a back button", "the modal appeared without any way to dismiss it" — which can be aggregated across agents and steps for the friction report.

---

## Concurrency Architecture Summary

For reference, the correct two-semaphore design:

```
browser_sem = asyncio.Semaphore(max_concurrent)   # how many agents run at once
llm_sem = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)  # how many API calls in flight

async def _run_agent(idx, persona):
    async with browser_sem:                          # holds for entire agent lifetime
        for step in range(max_steps):
            screenshot = await page.screenshot(...)
            async with llm_sem:                      # holds only during API call
                response = await acompletion(...)
            await execute_action(response)
```

This is different from screenshot mode where `browser_sem == llm_sem` (one agent = one call). In browser mode, an agent occupies a browser slot continuously but only holds the LLM slot briefly per step.
