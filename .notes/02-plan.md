# UX Swarm — Build Plan

## File Structure

The project will grow from a single `main.py` into a package. Introduce `ux_swarm/` early — it keeps `main.py` as a thin CLI entry point and prevents a painful refactor later.

```
ux_swarm/
  __init__.py
  main.py                      ← CLI entry point (already scaffolded)
  cli.py                       ← SmartGroup and shared CLI utilities
  models.py                    ← Pydantic models (Phase 1)
  config.py                    ← Config loading (Phase 2)
  llm.py                       ← LiteLLM calls (Phase 2)
  agent.py                     ← Agent logic — screenshot + browser (Phase 2, 4)
  runner.py                    ← Swarm orchestration (Phase 2, 4)
  results.py                   ← Persistence (Phase 2)
  display.py                   ← Rich terminal output (Phase 5)
```

Entry points in `pyproject.toml` point to `ux_swarm.main:cli`. Build backend is `uv_build` (native uv, no hatchling) with `module-root = ""` so uv finds the package at the project root.

---

## CLI Architecture Conventions

Three rules that apply across every phase. Don't drift from these.

**1. `main.py` is wiring only.**
No logic lives in `main.py` — only Click decorators, argument/option declarations, and delegation to `ux_swarm` functions. If a function in `main.py` does anything other than call into `ux_swarm`, it belongs in the package. The `SmartGroup` class currently in `main.py` is the first violation of this — move it to `ux_swarm/cli.py` at the start of Phase 1 before adding any new code.

**2. Config loads once at the root group, flows via `ctx.obj`.**
The root `cli()` group is decorated with `@click.pass_context`. It calls `load_config()` and stores the result in `ctx.obj`. Every subcommand that needs config receives it via `@click.pass_context` and reads from `ctx.obj["config"]`. This prevents redundant file reads, eliminates parameter pollution, and ensures CLI flag overrides are applied exactly once.

```python
@cli.result_callback()
# or at the group level:
@click.group()
@click.pass_context
def cli(ctx):
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config({})   # CLI overrides injected per-command

@cli.command()
@click.pass_context
def run(ctx, target, task, ...):
    config = {**ctx.obj["config"], **{k: v for k, v in overrides.items() if v is not None}}
    ...
```

**3. Module separation by responsibility.**
Each `ux_swarm/` module has one job and no cross-cutting concerns. CLI logic never leaks into `runner.py` or `agent.py`. Display logic (`display.py`) is never called from `runner.py` directly — use callbacks (see Phase 5 Rich UI note). Config is never hardcoded outside `config.py`.

> **[note]** The first positional argument in `main.py`'s `run` command is named `url` but it handles both URLs and images. Rename it to `target` everywhere — in the Click argument declaration, the function parameter, and all call sites — before Phase 2 begins. Leaving it as `url` will cause constant confusion when reading code that handles image paths.

---

## Phase 1 — Data Shape

**Goal:** Define all Pydantic models before writing any logic. This forces design decisions up front. Every function signature in later phases will be obvious because the input/output contracts are already defined.

**Dependencies:** None. Pydantic is not yet in `pyproject.toml` — add it now.

```bash
uv add pydantic
```

### `ux_swarm/models.py`

**UserType**

```python
class UserType(BaseModel):
    label: str
    weight: float
    description: str
```

**ScreenshotDecision** — what the LLM returns for one screenshot agent

```python
class ScreenshotDecision(BaseModel):
    target_element: str           # description of what they would click/interact with
    reasoning: str                # why they chose it
    friction_observed: list[str]  # friction points noticed on the page
    completed: bool               # did the agent complete the task?
    abandoned: bool               # did the agent give up?
    abandonment_reason: str | None
```

> **[note]** Add a `comment: str` field here. This is a one-sentence first-person summary from the agent's perspective — "I clicked the blue button but couldn't find the pricing link." Ask the LLM to produce it as part of this single response. Do not generate it in a second LLM call later. A second call per agent across 20 agents multiplies cost for no reason, and the comment is naturally available in the same context as everything else.

> **[note]** `completed=True` and `abandoned=True` simultaneously is an invalid state, but nothing prevents it. Add a Pydantic `@model_validator` that raises if both are True. Also handle the edge case where both are False — in screenshot mode this shouldn't happen (single call, agent either commits or gives up), but make the intention explicit.

**BrowserAction** — one action in the browser step loop

```python
class BrowserAction(BaseModel):
    action: Literal["click", "type", "scroll"]
    x: int                        # pixel coordinates on the screenshot
    y: int
    value: str | None             # text to type, or None for click/scroll
    reasoning: str
```

> **[note]** `x` and `y` are meaningless for `scroll` — you're scrolling the page, not targeting a point. Either remove `x`/`y` from scroll actions (make them `Optional[int]` with a note that they're ignored for scroll) or split into separate action types. The current shape will confuse the LLM when it tries to populate coordinates for a scroll action.

**BrowserDecision** — what the LLM returns after each browser screenshot

```python
class BrowserDecision(BaseModel):
    next_action: BrowserAction | None
    completed: bool
    abandoned: bool
    abandonment_reason: str | None
    friction_observed: list[str]
```

> **[note]** Add `comment: str` here too, same reason as `ScreenshotDecision`. The LLM writes a one-sentence first-person summary at each step. The runner collects the final step's comment as the agent's overall comment in `AgentResult`.

> **[note]** Same `@model_validator` guard needed here: `completed` and `abandoned` can't both be True. And if `completed=False` and `abandoned=False`, `next_action` must not be None — add that constraint too.

**AgentResult** — the output of one agent, shared by both modes

```python
class AgentResult(BaseModel):
    agent_index: int
    user_type: str
    completed: bool
    abandoned: bool
    abandonment_reason: str | None
    friction_points: list[str]
    comment: str                  # the agent's summary of their experience
    steps_taken: int              # 1 for screenshot mode, N for browser mode
    input_tokens: int
    output_tokens: int
    cost: float
```

> **[note]** `comment` is populated from the `comment` field in `ScreenshotDecision` / the final `BrowserDecision`. Not from a second LLM call. Not from `reasoning`. From the field we're adding to the decision models above.

**SwarmResult** — the full output written to `results.json`

```python
class SwarmResult(BaseModel):
    timestamp: str                # ISO 8601
    mode: Literal["screenshot", "browser"]
    target: str
    task: str
    model: str
    users: int
    completion_rate: float
    margin_of_error: float
    user_breakdown: dict[str, float]   # label → completion rate
    friction_points: list[str]         # deduplicated, sorted by frequency
    total_cost: float
    individual_results: list[AgentResult]
```

> **[note]** The "deduplicated, sorted by frequency" description for `friction_points` won't work in practice. Twenty agents each phrasing the same friction differently produces twenty unique strings — exact deduplication catches nothing. Don't try to solve this in v1. Collect all friction points raw, unsorted, and present them as-is. A meaningful deduplication pass would require an LLM or embedding similarity, which is out of scope. Change the field comment to just "collected from all agents."

**Done when:** All models import cleanly, `python -c "from ux_swarm.models import SwarmResult"` passes.

---

## Phase 2 — Screenshot Swarm, End-to-End

**Goal:** `swarm screenshot.png "find the login button"` runs, prints a completion rate, and writes a result to `.swarm/results.json`.

**Constraint:** Sequential agents only. No Rich UI. No file-based config. Print with `click.echo`.

### Step 1 — Config loading

**Dependencies:** None new.

Create `ux_swarm/config.py`. Implement config resolution in priority order (later wins):

1. Hardcoded defaults
2. `~/.config/uxswarm/config.json` — read if exists, ignore if not
3. `.swarm/config.json` — read if exists, ignore if not
4. CLI flags — passed in at call time
5. Environment variables — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`

Wire the full resolution chain now even though only env vars matter in Phase 2 — it's cheap and avoids un-hardcoding later.

```python
def load_config(cli_overrides: dict) -> dict:
    """Returns merged config. cli_overrides are highest priority after env vars."""
```

Hardcoded defaults:

```python
DEFAULTS = {
    "model": "gpt-4o",
    "users": 20,
    "max_steps": 3,
    "viewport": 1280,
}
```

> **[note]** Env vars are API keys only — `OPENAI_API_KEY` etc. They don't override `model` or `users`. The resolution order for non-key config is: defaults → global file → project file → CLI flags. Env vars feed the `api_key` field in the resolved config, not the general override chain.

> **[note]** `"model": "gpt-4o"` is fine as the default. This tool does vision + nuanced persona reasoning — model quality directly determines output quality. Be prepared to make the default configurable early; gpt-4o won't be the right answer for everyone. Keep the default but don't bury it.

**Done when:** `load_config({})` returns a dict with all defaults populated, and setting `OPENAI_API_KEY` in the environment makes it accessible via the returned config.

---

### Step 2 — LiteLLM call

**Dependencies:**

```bash
uv add litellm
```

Create `ux_swarm/llm.py`.

**Screenshot call** — takes an image, returns a structured decision:

```python
async def screenshot_decision(
    image_path: str,
    task: str,
    user_type: UserType,
    model: str,
) -> tuple[ScreenshotDecision, int, int, float]:
    # returns (decision, input_tokens, output_tokens, cost)
```

Implementation notes:

- Read the image file and base64-encode it
- Build a system prompt that injects `user_type.description` as the persona
- Build a user message with the task and image (as `image_url` content block with base64 data URI)
- Use `response_format` with `ScreenshotDecision.model_json_schema()` for structured output
- Call `litellm.acompletion()`
- Parse the response content as `ScreenshotDecision`
- Extract token counts from `response.usage`; use LiteLLM's `completion_cost()` for cost

> **[note]** `response_format` with a JSON schema only works reliably with OpenAI models that support structured output (gpt-4o, gpt-4o-mini). Anthropic and Gemini handle this differently through LiteLLM — behavior varies and may silently fall back to unstructured JSON. For Phase 2, target gpt-4o and verify structured output works before testing other providers. When adding provider support later, each provider may need its own `response_format` handling path.

> **[note]** The system prompt needs to tell the LLM explicitly what `completed` means — otherwise it will almost always return `completed: true` because LLMs are helpful by default and assume success. Add something like: "Only mark `completed: true` if the task goal is unambiguously achievable from what you can see on screen right now, with no additional navigation required. If you would need to scroll, navigate to another page, or if the target element is absent, do not mark it complete." This is the most important prompt constraint — getting it wrong breaks the entire value of the tool.

**System prompt template:**

```
You are simulating a user with the following profile:

{user_type.description}

Your job is to look at a UI screenshot and decide what you would do to complete the task.
Be honest about friction — anything that slows you down, confuses you, or makes you consider
giving up. Report what you actually would do, not what an ideal user would do.
```

> **[note]** Add the `completed` definition constraint above to this prompt. Also ask explicitly for a first-person `comment` field that summarizes the experience in one sentence from the user's perspective.

**Done when:** Calling `screenshot_decision()` with a real image and task returns a populated `ScreenshotDecision`.

---

### Step 3 — Single screenshot agent

In `ux_swarm/agent.py`:

```python
async def run_screenshot_agent(
    image_path: str,
    task: str,
    user_type: UserType,
    model: str,
    agent_index: int,
) -> AgentResult:
```

Maps `ScreenshotDecision` → `AgentResult`. The `comment` field should be a one-sentence natural language summary of the agent's experience, extracted from `reasoning` or generated as a second, cheap LLM call.

> **[note]** Not a second LLM call. Not extracted from `reasoning`. Use the `comment` field added directly to `ScreenshotDecision`. This is already available at zero extra cost.

**Done when:** A single agent call returns a valid `AgentResult`.

---

### Step 4 — Swarm runner (sequential)

In `ux_swarm/runner.py`:

```python
async def run_screenshot_swarm(
    image_path: str,
    task: str,
    config: dict,
) -> SwarmResult:
```

- Load user types: hardcode the default `UserType` for now (`.swarm/users.json` loading comes in Phase 5)
- Run agents sequentially: `for i in range(config["users"]): result = await run_screenshot_agent(...)`
- Aggregate into `SwarmResult`:
  - `completion_rate` = `completed_count / total`
  - `margin_of_error` = `1.96 * sqrt(p * (1 - p) / n)` — wire it now, it's one line
  - `friction_points` — collect all friction from all agents, deduplicate, sort by frequency
  - `user_breakdown` — for now, just `{"Default": completion_rate}` (multi-type comes in Phase 5)
  - `total_cost` = sum of all agent costs

> **[note]** As noted on the model: don't deduplicate `friction_points`. Just flatten all `friction_observed` lists from all agents into one list. Present them raw.

**Done when:** `run_screenshot_swarm()` returns a valid `SwarmResult` with correct completion rate.

---

### Step 5 — Wire the `run` command

In `main.py`, fill in the `run` command body. Config comes from `ctx.obj` (loaded once at the root group) — merge CLI overrides on top:

```python
@cli.command()
@click.pass_context
def run(ctx, target, task, users, max_steps, viewport, verbose):
    overrides = {k: v for k, v in {"users": users, "max_steps": max_steps, "viewport": viewport}.items() if v is not None}
    config = {**ctx.obj["config"], **overrides}

    if _looks_like_image(url):
        result = asyncio.run(run_screenshot_swarm(url, task, config))
    else:
        click.echo("Browser mode not yet implemented.")
        return

    click.echo(f"Completion rate: {result.completion_rate:.0%} (±{result.margin_of_error:.0%})")
    click.echo(f"Users: {result.users}  Cost: ${result.total_cost:.4f}")
    if result.friction_points:
        click.echo("Friction points:")
        for point in result.friction_points:
            click.echo(f"  • {point}")
```

> **[note]** Rename `url` → `target` in the Click argument and function parameter here. Also rename in `SmartGroup._looks_like_target` — the detection logic is already correct, just the naming is wrong.

> **[note]** `load_config` receives a dict of CLI overrides — filter out `None` values before passing them in, otherwise a CLI option that wasn't specified (defaulted to `None`) will overwrite the config file value with `None`. Do: `{k: v for k, v in {"users": users, ...}.items() if v is not None}`.

> **[note]** Wire `--verbose` through to the runner now, not in Phase 5. You'll want it immediately for debugging in Phase 2. At minimum, pass it through and use it to print each agent's `reasoning` and `comment` during sequential runs. One flag, one `if verbose: click.echo(...)`. Costs nothing to do now.

Move `_looks_like_target` out of `SmartGroup` into a shared utility so `run` can reuse it for the URL/image branch.

---

### Step 6 — Results persistence

In `ux_swarm/results.py`:

```python
def save_result(result: SwarmResult) -> None:
def load_results() -> list[SwarmResult]:
```

- Target path: `.swarm/results.json` relative to cwd
- Create `.swarm/` directory if it doesn't exist
- Read the existing JSON array (empty list if file doesn't exist), append, write back
- Use `SwarmResult.model_dump_json()` and `SwarmResult.model_validate_json()` for serialization

Call `save_result(result)` at the end of the `run` command.

**Done when:** Running `swarm screenshot.png "task"` twice produces a `results.json` with two entries.

---

## Phase 3 — Validate Prompt Design

**Goal:** Before building browser mode, deeply validate that the screenshot agent produces useful, accurate output. This is the cheapest time to iterate — no Playwright, no async bugs, fast feedback.

> **[note]** This phase is not optional and not a soft gate. The prompt is the product — if the LLM output isn't genuinely useful here, browser mode will produce garbage faster. Do not move to Phase 4 until Phase 3's bar is met.

**Prepare four reference test cases** with known expected behavior:

| Test case      | Image                                              | Task                                  | Expected outcome                           |
| -------------- | -------------------------------------------------- | ------------------------------------- | ------------------------------------------ |
| Clear CTA      | Simple landing page with prominent sign-up button  | "sign up for an account"              | High completion, minimal friction          |
| Cluttered form | Dense registration form with many unlabeled fields | "create an account"                   | Lower completion, specific friction points |
| Pricing page   | Three-tier pricing table                           | "find the best plan for a small team" | Variable — tests reasoning quality         |
| Hidden action  | Page where the key action requires scrolling       | "find the contact link"               | Tests whether friction detection fires     |

**Iterate until:**

- Completion detection is reliable (not just "always true" or "always false")
- Different user type personas actually produce different `reasoning` and `friction_observed` content — test by injecting a "power user" description vs. the default "satisficer" and confirming the outputs meaningfully differ
- Friction points are specific and actionable, not generic ("confusing interface") — tune the prompt if they're too vague
- Running the same test 5x produces natural variation, not identical outputs

**This phase is done when** you'd trust the output enough to show it to a developer as real UX feedback. That's the bar.

---

## Phase 4 — Browser Swarm

**Goal:** `swarm https://example.com "find the login button"` runs a real browser swarm end-to-end.

**Dependencies:**

```bash
uv add playwright
playwright install chromium
```

### The element targeting decision

This is the hardest design problem in browser mode. The LLM needs to return something Playwright can act on. **Use coordinate-based clicking.** The LLM looks at the screenshot and returns `{x, y}` pixel coordinates. Playwright calls `page.mouse.click(x, y)`.

Why coordinates, not CSS selectors:

- The LLM sees a screenshot, not the DOM — it can point to what it sees far more reliably than it can generate a correct CSS selector
- Works on any page regardless of DOM structure
- Simulates real user behavior (clicking on what's visible)

The tradeoff is brittleness on dynamic layouts — acceptable for v1.

> **[note]** Vision models are not consistently precise with pixel coordinates. The LLM sees the image at one resolution and reasons about coordinates in the viewport's pixel space — these should match, but clicks can land 10–20px off on smaller targets. Before building the full loop, run a standalone test: give the model a screenshot of a known page, ask it for the coordinates of a specific button, and verify the click actually lands. If it's consistently off, you may need to ask the LLM for a description of the target element and do a DOM query fallback. Know this before writing the full agent loop.

### Step 1 — Browser LLM call

Add to `ux_swarm/llm.py`:

```python
async def browser_decision(
    screenshot_bytes: bytes,
    task: str,
    user_type: UserType,
    model: str,
    step_history: list[BrowserAction],
) -> tuple[BrowserDecision, int, int, float]:
```

The system prompt should include the step history so the LLM knows what it has already tried. Format each past action as a plain English summary: `"Step 1: clicked at (342, 156) — the blue 'Sign up' button"`.

The BrowserDecision schema tells the LLM to either return a `next_action` (with `x`, `y`, `action`, `value`) or set `completed`/`abandoned` to true. Include clear stopping criteria in the prompt: the agent should abandon if it has been on the same page for two steps without progress.

---

### Step 2 — Single browser agent

Add to `ux_swarm/agent.py`:

```python
async def run_browser_agent(
    url: str,
    task: str,
    user_type: UserType,
    model: str,
    max_steps: int,
    viewport: int,
    browser,                      # shared playwright Browser instance
    agent_index: int,
) -> AgentResult:
```

Implementation:

```
context = await browser.new_context(viewport={"width": viewport, "height": 768})
page = await context.new_page()
await page.goto(url)
history = []

for step in range(max_steps):
    screenshot = await page.screenshot()
    decision, in_tok, out_tok, cost = await browser_decision(screenshot, task, user_type, model, history)

    if decision.completed or decision.abandoned:
        break

    action = decision.next_action
    if action.action == "click":
        await page.mouse.click(action.x, action.y)
    elif action.action == "type":
        await page.keyboard.type(action.value)
    elif action.action == "scroll":
        await page.mouse.wheel(0, 300)

    history.append(action)
    await page.wait_for_load_state("networkidle", timeout=5000)

await context.close()
return build_agent_result(...)
```

> **[note]** Replace `wait_for_load_state("networkidle", timeout=5000)` with `wait_for_load_state("domcontentloaded")` followed by `page.wait_for_timeout(500)`. The `networkidle` state waits for no network requests for 500ms — it hangs indefinitely on SPAs with background polling, analytics pings, or websocket connections. Many real-world sites never reach `networkidle`. This will silently time out on a huge fraction of URLs.

> **[note]** Error handling: use `except Exception` explicitly — not bare `except` and not `except BaseException`. `CancelledError` in Python 3.11 is a `BaseException`, not an `Exception`. If you use bare `except` or `except BaseException` inside the loop, you'll swallow keyboard interrupts and task cancellations. Let those propagate. Only catch `Exception` to handle Playwright errors (element not found, navigation error, timeout).

> **[note]** `await context.close()` must be in a `finally` block, not after the loop. If any exception escapes the try/except in the loop body, the context leaks. Structure as: `try: [loop] finally: await context.close()`.

Error handling: wrap the loop body in try/except. If Playwright throws (element not found, navigation error, timeout), mark the agent as abandoned with the error as the abandonment reason.

---

### Step 3 — Browser swarm runner

Add to `ux_swarm/runner.py`:

```python
async def run_browser_swarm(
    url: str,
    task: str,
    config: dict,
) -> SwarmResult:
```

```python
async with async_playwright() as p:
    browser = await p.chromium.launch()
    results = []
    for i in range(config["users"]):
        result = await run_browser_agent(url, task, user_type, model, max_steps, viewport, browser, i)
        results.append(result)
    await browser.close()
```

Sequential for now — concurrency comes next.

Wire into the `run` command's `else` branch.

**Done when:** `swarm https://example.com "find the login button"` completes and writes a result.

---

### Step 4 — Add concurrency to both runners

Once both swarm runners work sequentially, add `asyncio.Semaphore` to both in the same change:

```python
# Screenshot runner
semaphore = asyncio.Semaphore(20)
async def run_one(i):
    async with semaphore:
        return await run_screenshot_agent(...)

results = await asyncio.gather(*[run_one(i) for i in range(n_users)])
```

```python
# Browser runner
semaphore = asyncio.Semaphore(5)
async def run_one(i):
    async with semaphore:
        return await run_browser_agent(..., browser, i)

results = await asyncio.gather(*[run_one(i) for i in range(n_users)])
```

The Semaphore(5) for browser mode limits concurrent Playwright contexts — essential because each context holds browser resources. Semaphore(20) for screenshot mode limits concurrent LLM calls.

**CancelledError note:** Keyboard interrupt during `asyncio.gather` raises `CancelledError`. In Python 3.11+, `CancelledError` is not a subclass of `Exception` — a bare `except Exception` will not catch it. Any cleanup code (closing the browser) must be in a `finally` block, not an `except Exception` block.

> **[note]** Python 3.11 also introduced `asyncio.TaskGroup`, which is the modern structured-concurrency alternative to `asyncio.gather`. TaskGroup gives better cancellation semantics: if one task raises, it cancels the others automatically. Consider it here, especially for browser mode where a single agent crashing should cleanly cancel siblings. The semaphore pattern works with both — it's orthogonal.

---

## Phase 5 — Polish

Each item here is independent of the others and can be done in any order.

### Rich terminal UI

**Dependencies:**

```bash
uv add rich
```

Create `ux_swarm/display.py`.

**During a run** — `Live` table updating as agents complete:

```python
def make_progress_table(results_so_far: list[AgentResult], total: int) -> Table:
    table = Table(box=box.SIMPLE)
    table.add_column("Agent", style="dim")
    table.add_column("Persona")
    table.add_column("Result")
    table.add_column("Friction")
    for r in results_so_far:
        status = "[green]✓" if r.completed else "[red]✗"
        friction = r.friction_points[0] if r.friction_points else "—"
        table.add_row(str(r.agent_index + 1), r.user_type, status, friction)
    # Pending rows
    for i in range(len(results_so_far), total):
        table.add_row(str(i + 1), "—", "[dim]running…", "—")
    return table
```

Pass a `Live` instance into the runners and call `live.update(make_progress_table(...))` after each agent completes.

> **[note]** Don't pass a `Live` instance into the runners — it couples runner logic to display logic and makes runners untestable without a terminal. Instead, add an `on_agent_complete: Callable[[AgentResult], None] | None = None` parameter to the runner. The caller (in `main.py`) wires up the Live update: `on_agent_complete=lambda r: live.update(make_progress_table(...))`. Runners that don't need UI call this or skip it if None.

**After a run** — summary panel:

```python
def print_summary(result: SwarmResult) -> None:
```

Use `rich.panel.Panel` and `rich.text.Text` to render: completion rate (large), margin of error, model, cost, then a list of friction points sorted by frequency.

---

### `swarm config` wizard

Implement the `config` command body in `main.py`. Use Click's `click.prompt()` and `click.confirm()` for input, and Rich's `rich.prompt.Prompt` for styled select lists.

Steps:

1. Select LLM provider from a fixed list
2. Enter API key — `prompt(hide_input=True)` — validate live by making a cheap test call (`litellm.completion()` with `max_tokens=1`)
3. Select model — generate the list dynamically from the provider after API key validation
4. Install Playwright if not already installed — detect via `playwright._impl._driver.compute_driver_executable()` or by attempting `import playwright`; if missing, run `playwright install chromium` as a subprocess

> **[note]** `playwright._impl._driver.compute_driver_executable()` is a private API that can change between Playwright versions. Use `shutil.which("playwright")` to check if the CLI is installed, then run `subprocess.run(["playwright", "install", "chromium"])` directly. To check if chromium is actually installed (vs. just Playwright), run `subprocess.run(["playwright", "install", "--dry-run", "chromium"])` and check the output, or just always run the install — it's idempotent and fast when already installed.

Save to `~/.config/uxswarm/config.json`.

---

### User type system

**`swarm users`** (no flag):

- Load `.swarm/users.json` if it exists, else show the hardcoded default
- Print each type: label, weight percentage, description

**`swarm users --config`**:

- Write the default `users.json` to `.swarm/users.json`
- Print the file path

**Loading user types in the runner:**

- In `runner.py`, implement `load_user_types() -> list[UserType]`
- Reads `.swarm/users.json` if present, else returns `[DEFAULT_USER_TYPE]`
- When assigning agents to types, use weighted random sampling: `random.choices(types, weights=[t.weight for t in types], k=n_users)`

---

### `swarm report`

Implement the `report` command in `main.py`.

```
swarm report          # all results
swarm report -n 3     # last 3
```

For each result, print a formatted summary line:

```
2026-04-25 14:32  screenshot  screenshot.png  "find the login button"
                  75% complete (±9%)  20 users  $0.14
```

Add a `--full` flag to also print friction points and individual agent results for the selected entry.

---

### Full config resolution

Once `swarm config` is implemented and writing `~/.config/uxswarm/config.json`, update `config.py` to read it. The chain is already wired (Phase 2, Step 1) — just enable the file reads.

---

### `--verbose` flag

Pass `verbose` through from the `run` command into the runners. When `verbose=True`, wrap the runner in a try/except and `traceback.print_exc()` on error instead of swallowing it. Also print each agent's full `reasoning` and `comment` after the summary.

> **[note]** Wire `--verbose` through in Phase 2, not here. You'll need it to debug the prompt during development. At minimum: if `verbose`, print each agent's `reasoning` and `comment` after it completes, before moving to the next agent. One `if verbose: click.echo(...)` in the sequential loop. Do it now.

---

## Dependency Timeline

| Phase   | `uv add`                                        |
| ------- | ----------------------------------------------- |
| Phase 1 | `pydantic`                                      |
| Phase 2 | `litellm`                                       |
| Phase 4 | `playwright` then `playwright install chromium` |
| Phase 5 | `rich`                                          |

`requests` (already in `pyproject.toml`) is unused — remove it when starting Phase 2.

---

## Success Criteria Per Phase

| Phase | Done when                                                                                         |
| ----- | ------------------------------------------------------------------------------------------------- |
| 1     | All models import cleanly; `SwarmResult` instantiates with test data                              |
| 2     | `swarm screenshot.png "task"` prints a completion rate and writes `results.json`                  |
| 3     | Four reference test cases produce specific, accurate, varied output you'd trust                   |
| 4a    | `swarm https://example.com "task"` runs sequentially and writes a result                          |
| 4b    | Both modes run concurrently; Ctrl-C cleans up without hanging                                     |
| 5     | `swarm config` completes the wizard; `swarm report` shows history; Rich table renders during runs |
