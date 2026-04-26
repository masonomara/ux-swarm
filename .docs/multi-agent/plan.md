# Plan: Multi-Agent Screenshot Swarm

_Based on: source audit of `src/ux_swarm/`, beta repo research, and design analysis_

---

## Scope

### In Scope

- Replace the single-agent `run` loop with N concurrent async agents
- Persona distribution across N agent slots (weight-proportional)
- `personas.py` — new file for loading and distributing user types from `.swarm/users.json`
- `swarm.py` — new async orchestrator (`run_screenshot_swarm`)
- Async refactor of `agent.py` using LiteLLM `acompletion`
- Real cost tracking per agent via `litellm.completion_cost()`
- `SwarmResult` aggregation and persistence (replacing single `AgentResult` save)
- Live progress display during run: `42 / 100  agents complete`
- Final swarm summary display: completion rate, confidence interval, user breakdown, friction
- `users` CLI command: list user types; `--config` flag to write `.swarm/users.json`
- Exponential backoff on rate limits: 60 → 120 → 240 seconds (fixes fixed-60s beta regression)
- One semaphore for screenshot mode — screenshot agents make exactly one LLM call each, so the concurrency semaphore IS the LLM semaphore

### Not In Scope

- Browser mode (URL targets, Playwright)
- `swarm report` command
- DeepSeek support (text-only models, no vision)
- Semantic deduplication of friction points
- Schema versioning on saved reports
- Config file chmod / world-readable key fix

---

## Files

### Create

| File | Role |
|---|---|
| `src/ux_swarm/personas.py` | Load, validate, distribute `UserType` objects to N agent slots |
| `src/ux_swarm/swarm.py` | Async multi-agent orchestrator: TaskGroup + semaphore, aggregation |

### Modify

| File | What Changes |
|---|---|
| `pyproject.toml` | Add `litellm` dependency |
| `src/ux_swarm/agent.py` | Make `_run_single_agent` async via LiteLLM; remove `urllib` provider calls |
| `src/ux_swarm/main.py` | Wire `--users` flag; add Live display; call swarm; print `SwarmResult`; add `users` command |

### Untouched

`cli.py`, `config.py`, `menu.py`, `models.py`, `__init__.py`

---

## Dependency Change

```toml
# pyproject.toml — add to [project.dependencies]
"litellm>=1.72.0"
```

LiteLLM replaces the hand-rolled `_call_anthropic` and `_call_openai_compat` in `agent.py`. It handles provider routing, authentication, and error normalization across OpenAI, Anthropic, and Gemini. Its `acompletion` is the async entry point. `completion_cost()` gives real cost per call — fixing the `cost = 0.0` stub in every `AgentResult`.

LiteLLM reads API keys from environment variables. Before calling `asyncio.run()`, the resolved API key from config is injected into `os.environ` (see `main.py` phase below). This matches the beta's `_inject_api_key()` pattern.

---

## Phase 1 — `src/ux_swarm/personas.py` (new file)

### Constants

```python
from ux_swarm.config import LOCAL_DIR
from ux_swarm.models import UserType

USERS_JSON = LOCAL_DIR / "users.json"

DEFAULT_USERS: list[UserType] = [
    UserType(
        label="Default User",
        weight=1.0,
        description=(
            "In a hurry and doesn't read pages — scans them quickly, looking for words "
            "or links that match the task. Doesn't weigh options or look for the best "
            "choice; clicks the first thing that looks reasonable enough to work "
            "(satisficing). Doesn't try to understand how the site is structured or how "
            "things work — muddles through, and if something seems to work, sticks with "
            "it without figuring out why. Has low tolerance for friction: any moment "
            "that requires stopping to think, read instructions, or decode an interface "
            "increases the chance of giving up and abandoning the task."
        ),
    )
]
```

### `load_users(path: str | None = None) -> list[UserType]`

Resolution order: caller-supplied path → `.swarm/users.json` → `DEFAULT_USERS`.

```python
def load_users(path: str | None = None) -> list[UserType]:
    if path:
        file = Path(path)
    elif USERS_JSON.exists():
        file = USERS_JSON
    else:
        return list(DEFAULT_USERS)

    try:
        raw = json.loads(file.read_text())
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"users.json is not valid JSON: {file}\n{exc}"
        ) from exc

    if not isinstance(raw, list):
        raise click.ClickException("users.json must be a JSON array")

    users = []
    for entry in raw:
        try:
            users.append(UserType.model_validate(entry))
        except Exception as exc:
            raise click.ClickException(f"Invalid user type entry: {exc}") from exc

    return users if users else list(DEFAULT_USERS)
```

A broken file always raises — no silent fallback to defaults. If the file is intentionally empty, fall back to defaults (empty list → defaults).

### `distribute_users(users: list[UserType], n: int) -> list[UserType]`

Weight-proportional distribution into exactly N slots.

```python
def distribute_users(users: list[UserType], n: int) -> list[UserType]:
    total_weight = sum(u.weight for u in users)
    slots: list[UserType] = []

    for u in users:
        count = max(1, round((u.weight / total_weight) * n))
        slots.extend([u] * count)

    if len(slots) > n:
        slots = slots[:n]
    while len(slots) < n:
        slots.append(users[0])

    return slots
```

Every user type gets at least 1 slot (`max(1, round(...))`). Overshoot: truncate tail. Undershoot: pad with `users[0]` — by convention, the highest-weight type is listed first (the default has only one type, so this is always it).

### `write_default_users() -> Path`

```python
def write_default_users() -> Path:
    from ux_swarm.config import LOCAL_DIR
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    data = [u.model_dump() for u in DEFAULT_USERS]
    USERS_JSON.write_text(json.dumps(data, indent=2) + "\n")
    return USERS_JSON
```

Called by `swarm users --config`.

---

## Phase 2 — `src/ux_swarm/agent.py` (async refactor)

Remove: `_call_anthropic`, `_call_openai_compat`, `_OPENAI_COMPAT_ENDPOINTS`, all `urllib` imports.

Keep: `_MIME_TYPES`, `_media_type`, `_load_image`, `_build_system_prompt`.

### New: `_RETRY_DELAYS`

```python
_RETRY_DELAYS = (60, 120, 240)
```

Exponential backoff for rate limits. 4 total attempts (3 delays). Fixes the beta's fixed-60s regression.

### New: `run_screenshot_agent` (async)

Signature replaces the old synchronous version:

```python
async def run_screenshot_agent(
    target: str,
    task: str,
    user_type: UserType,
    model: str,          # full model string from config, e.g. "anthropic/claude-sonnet-4-20250514"
    api_key: str,        # for error context only — LiteLLM reads from os.environ
) -> tuple[ScreenshotDecision, int, int, float]:
    """Returns (decision, input_tokens, output_tokens, cost)."""
```

Note: `model` is the full `"provider/model-id"` string from config — LiteLLM accepts this format directly. The api_key parameter is kept for error messages but LiteLLM reads the actual key from the environment variable set in `main.py`.

Implementation:

```python
import litellm
from litellm import acompletion
from litellm.exceptions import RateLimitError
from litellm.utils import completion_cost

async def run_screenshot_agent(target, task, user_type, model, api_key):
    image_data, media_type = _load_image(target)
    system = _build_system_prompt(user_type)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": [
            {"type": "text", "text": f"Task: {task}"},
            {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_data}"}},
        ]},
    ]

    last_exc: BaseException | None = None
    for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
        try:
            response = await acompletion(
                model=model,
                messages=messages,
                max_tokens=1024,
                response_format={"type": "json_object"},
            )
            break
        except RateLimitError as exc:
            last_exc = exc
            if delay is None:
                raise click.ClickException(
                    f"Rate limited after {len(_RETRY_DELAYS) + 1} attempts. "
                    "Try again later or reduce --users."
                ) from exc
            await asyncio.sleep(delay)
    else:
        raise click.ClickException("Rate limit exhausted") from last_exc

    raw = response.choices[0].message.content or ""
    in_tok = response.usage.prompt_tokens
    out_tok = response.usage.completion_tokens

    try:
        cost = completion_cost(completion_response=response, model=model)
    except Exception:
        cost = 0.0

    try:
        decision = ScreenshotDecision.model_validate_json(raw)
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

    return decision, in_tok, out_tok, cost
```

Key details:
- Parse failure falls back to a synthetic `ScreenshotDecision` with `completed=False`, `abandoned=True`. This counts as a non-completion without crashing the swarm.
- LiteLLM accepts the `"anthropic/claude-sonnet-4-20250514"` format directly and routes accordingly.
- `response_format={"type": "json_object"}` works for OpenAI and Gemini. For Anthropic, LiteLLM translates this to a prompt-level instruction (Anthropic doesn't have a native `response_format` field).
- `litellm.suppress_debug_info = True` — set at module top level to suppress LiteLLM's startup output.

Add at top of file:

```python
import asyncio
import litellm
litellm.suppress_debug_info = True
```

Remove from file:
- `import urllib.request`
- `_OPENAI_COMPAT_ENDPOINTS`
- `_call_anthropic`
- `_call_openai_compat`
- `_call_llm`

The `_load_image` function stays as-is. `_build_system_prompt` stays as-is. `_media_type` and `_MIME_TYPES` stay as-is.

**Important:** `_load_image` is still called inside `run_screenshot_agent` in this design. That means each of N agents reads the same file from disk N times. This is fine for now but should move to the swarm orchestrator (read once, pass b64 string) in a future pass. The plan notes this explicitly in Phase 3.

---

## Phase 3 — `src/ux_swarm/swarm.py` (new file)

Single public function: `run_screenshot_swarm`. Everything else is private.

### Signature

```python
from __future__ import annotations
import asyncio
import math
from collections.abc import Callable
from datetime import datetime, timezone

from ux_swarm.agent import run_screenshot_agent
from ux_swarm.models import AgentResult, SwarmResult, UserType


async def run_screenshot_swarm(
    target: str,
    task: str,
    users: list[UserType],
    num_agents: int,
    model: str,
    api_key: str,
    max_concurrent: int,
    on_agent_done: Callable[[int, int], None] | None = None,
) -> SwarmResult:
```

### Concurrency Design

One `asyncio.Semaphore(max_concurrent)` throttles LLM calls. In screenshot mode, each agent makes exactly one LLM call — the concurrency semaphore IS the LLM semaphore. No need for a second semaphore (unlike browser mode, which has multiple LLM calls per agent per step).

Use **`asyncio.TaskGroup`** (not `asyncio.gather`). TaskGroup cancels siblings on unhandled exceptions immediately. Per-agent exceptions are caught inside each task to prevent TaskGroup cancellation — `CancelledError` is always re-raised.

```python
from ux_swarm.personas import distribute_users

async def run_screenshot_swarm(...) -> SwarmResult:
    assigned = distribute_users(users, num_agents)
    semaphore = asyncio.Semaphore(max_concurrent)

    results: list[AgentResult] = []
    completed_count = 0

    async def _run_agent(idx: int, user_type: UserType) -> None:
        nonlocal completed_count
        try:
            async with semaphore:
                decision, in_tok, out_tok, cost = await run_screenshot_agent(
                    target=target,
                    task=task,
                    user_type=user_type,
                    model=model,
                    api_key=api_key,
                )
            result = AgentResult(
                agent_index=idx,
                user_type=user_type.label,
                completed=decision.completed,
                abandoned=decision.abandoned,
                abandonment_reason=decision.abandonment_reason,
                friction_points=decision.friction_observed,
                comment=decision.comment,
                target_element=decision.target_element,
                reasoning=decision.reasoning,
                steps_taken=1,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cost=cost,
            )
            results.append(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # silently drop individual agent failures
        finally:
            completed_count += 1
            if on_agent_done:
                on_agent_done(completed_count, num_agents)

    async with asyncio.TaskGroup() as tg:
        for idx, user_type in enumerate(assigned):
            tg.create_task(_run_agent(idx, user_type))

    return _aggregate(results, target, task, model, num_agents)
```

List `append` and `completed_count +=` are safe without locks — asyncio is single-threaded and there is no preemption between `await` calls.

### Aggregation: `_aggregate`

```python
def _aggregate(
    results: list[AgentResult],
    target: str,
    task: str,
    model: str,
    num_agents: int,
) -> SwarmResult:
    n = len(results)

    if n == 0:
        raise click.ClickException(
            f"All {num_agents} agents failed. Check your API key and model configuration."
        )

    completion_rate = sum(1 for r in results if r.completed) / n
    moe = 1.96 * math.sqrt(completion_rate * (1 - completion_rate) / n) if n > 1 else 0.0

    # group by user_type for breakdown
    by_label: dict[str, list[bool]] = {}
    for r in results:
        by_label.setdefault(r.user_type, []).append(r.completed)
    user_breakdown = {
        label: sum(outcomes) / len(outcomes)
        for label, outcomes in by_label.items()
    }

    friction_points = [fp for r in results for fp in r.friction_points]
    total_cost = sum(r.cost for r in results)
    model_id = model.split("/", 1)[-1] if "/" in model else model

    return SwarmResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        mode="screenshot",
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
    )
```

`SwarmResult.users` is the count of *successful* agents (those that returned a valid result), not `num_agents`. This matches the beta's pattern — failed agents are excluded from the denominator.

`friction_points` in `SwarmResult` is raw across all agents, not deduplicated — this is intentional per the existing model docstring.

`model_id` strips the `provider/` prefix for the result (matches the existing single-agent `_print_result` display).

---

## Phase 4 — `src/ux_swarm/main.py` (wiring)

### API Key Injection

Add before `asyncio.run()` in the `run` command:

```python
_PROVIDER_ENV_VARS = {
    "openai":    "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "gemini":    "GEMINI_API_KEY",
}

def _inject_api_key(provider: str, api_key: str) -> None:
    env_var = _PROVIDER_ENV_VARS.get(provider)
    if env_var and api_key and not os.environ.get(env_var):
        os.environ[env_var] = api_key
```

Existing env vars are not overwritten — a pre-set `ANTHROPIC_API_KEY` takes precedence over config (supports CI/CD).

Add `import os` to main.py imports.

### `run` Command Changes

Remove:
- Hardcoded `UserType(label="Default User", ...)` block
- `run_screenshot_agent` import and call
- Single-agent `AgentResult` construction
- `_print_result` call and function definition

Add:

```python
from ux_swarm.personas import load_users
from ux_swarm.swarm import run_screenshot_swarm
```

Updated `run` command body:

```python
@cli.command(hidden=True)
@click.argument("target")
@click.argument("task")
@click.option("--users", default=None, type=int, help="Number of simulated users")
@click.option("--max-steps", default=None, type=int, help="Max steps per agent (browser only)")
@click.option("--viewport", default=None, type=int, help="Viewport width in pixels (browser only)")
@click.option("--verbose", is_flag=True, help="Show full tracebacks on error")
@click.pass_context
def run(ctx, target, task, users, max_steps, viewport, verbose):
    """Run a swarm of simulated users against a URL or screenshot image."""
    if target.startswith(("http://", "https://")):
        raise click.ClickException(
            "URL targets require browser mode, which is not yet available. "
            "Pass a screenshot image path instead."
        )

    config = load_config()
    model_full = config.get("model", "")
    api_key = config.get("api_key", "")
    provider = config.get("provider", "")

    if not model_full:
        raise click.ClickException("No model configured — run `swarm config` to set one.")
    if not api_key:
        raise click.ClickException("No API key configured — run `swarm config` to set one.")

    _inject_api_key(provider, api_key)

    num_agents = users or RUN_DEFAULTS["default_users"]
    max_concurrent = RUN_DEFAULTS["max_concurrent_screenshot"]

    user_types = load_users()

    try:
        with Live(console=_console, auto_refresh=False) as live:
            def on_done(done: int, total: int) -> None:
                from rich.text import Text
                live.update(
                    Text(f"\n  {done} / {total}  agents complete\n", style="dim"),
                    refresh=True,
                )

            result = asyncio.run(
                run_screenshot_swarm(
                    target=target,
                    task=task,
                    users=user_types,
                    num_agents=num_agents,
                    model=model_full,
                    api_key=api_key,
                    max_concurrent=max_concurrent,
                    on_agent_done=on_done,
                )
            )
    except click.ClickException:
        raise
    except Exception as exc:
        if verbose:
            raise
        raise click.ClickException(str(exc)) from exc

    ensure_swarm_structure()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = Path(target).stem
    report_path = LOCAL_DIR / "reports" / f"{timestamp}_{stem}.json"
    try:
        report_path.write_text(result.model_dump_json(indent=2) + "\n")
    except OSError as exc:
        _console.print(f"[dim]Warning: could not save report: {exc}[/]")

    _print_swarm_result(target, task, result)
```

Add to imports:
```python
import asyncio
import os
from rich.live import Live
from rich.text import Text
```

### `_print_swarm_result`

Replaces the old `_print_result`. Shows aggregate data, not individual decisions.

```python
def _print_swarm_result(target: str, task: str, result: SwarmResult) -> None:
    filename = Path(target).name
    rate_pct = f"{result.completion_rate:.0%}"
    moe_pct = f"±{result.margin_of_error:.0%}"

    if result.completion_rate >= 0.8:
        rate_style = "green"
    elif result.completion_rate >= 0.5:
        rate_style = "yellow"
    else:
        rate_style = "red"

    _console.print()
    _console.rule(style="dim")
    _console.print()
    _console.print(f"  {filename} — \"{task}\"", highlight=False)
    _console.print()
    _console.print(
        f"  [{rate_style}][bold]{rate_pct}[/bold][/]  {moe_pct}  ·  {result.users} agents",
        highlight=False,
    )

    if len(result.user_breakdown) > 1:
        _console.print()
        _console.print("  User Breakdown", highlight=False)
        for label, rate in result.user_breakdown.items():
            _console.print(f"  [dim]{label:<20}[/]  {rate:.0%}", highlight=False)

    if result.friction_points:
        from collections import Counter
        top = Counter(fp for fp in result.friction_points if fp).most_common(5)
        _console.print()
        _console.print("  Friction", highlight=False)
        for point, _ in top:
            _console.print(f"  [dim]•[/] {point}", highlight=False)

    _console.print()
    cost_str = f"${result.total_cost:.4f}" if result.total_cost else ""
    model_line = result.model
    if cost_str:
        model_line += f"  ·  {cost_str}"
    _console.print(f"  [dim]{model_line}[/]", highlight=False)
    _console.print()
    _console.rule(style="dim")
    _console.print()
```

Top 5 friction points by frequency — avoids noise from one-off observations.

### `users` Command (new)

```python
@cli.command()
@click.option("--config", "write_config", is_flag=True, help="Write .swarm/users.json for editing")
def users(write_config):
    """List active user types and weights. Use --config to write .swarm/users.json."""
    from ux_swarm.personas import load_users, write_default_users, USERS_JSON

    if write_config:
        if USERS_JSON.exists():
            _console.print(f"[dim]{USERS_JSON} already exists.[/]")
        else:
            path = write_default_users()
            _console.print(f"[green]Written →[/] {path}")
            _console.print("[dim]Edit the file to define user types and weights.[/]")
        return

    active = load_users()
    total_weight = sum(u.weight for u in active)
    _console.print()
    for u in active:
        share = u.weight / total_weight
        _console.print(f"  [bold]{u.label}[/]  [dim]{share:.0%}[/]", highlight=False)
        _console.print(f"  [dim]{u.description[:120]}{'…' if len(u.description) > 120 else ''}[/]", highlight=False)
        _console.print()
```

### `RUN_DEFAULTS` Update

The existing dict already has `max_concurrent_screenshot: 20`. Add `default_users`:

```python
RUN_DEFAULTS: dict[str, int | float] = {
    "default_users": 20,
    "max_steps": 3,
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 20,
}
```

---

## Phase 5 — Validate

- [ ] Run with 1 user: `swarm screenshot.png "find the login button" --users 1` — verify it behaves like the old single-agent run but saves a `SwarmResult`
- [ ] Run with 5 users: `swarm screenshot.png "find the login button" --users 5` — verify progress counter updates 5 times, final display shows correct rate
- [ ] Check `.swarm/reports/` — verify `SwarmResult` JSON is valid, `individual_results` has 5 entries
- [ ] Run with `--users 20` — verify concurrency semaphore fires at most 20 concurrent calls (check provider dashboard or add a temporary counter)
- [ ] Verify exponential backoff: temporarily return 429 from agent, confirm delays are 60 → 120 → 240
- [ ] Test `swarm users` — lists default user type with weight and truncated description
- [ ] Test `swarm users --config` — writes `.swarm/users.json`, prints path
- [ ] Test `swarm users --config` a second time — prints "already exists", doesn't overwrite
- [ ] Write a two-type `users.json`, run with `--users 10` — verify user breakdown shows both types and distribution is correct
- [ ] Test error path: bad image path — clear `ClickException`, no traceback without `--verbose`
- [ ] Test error path: URL as target — still raises with browser-mode message
- [ ] Test error path: no config — still raises with `swarm config` instruction
- [ ] Test `--verbose` — full traceback on a parse failure

---

## Design Decisions

**TaskGroup over gather.** `asyncio.gather(return_exceptions=True)` collects exceptions but allows all tasks to complete regardless of fatal errors. `asyncio.TaskGroup` cancels siblings immediately on unhandled exceptions. We use TaskGroup with per-agent exception catching inside each task — individual agent failures are swallowed (except `CancelledError`), so the TaskGroup itself never sees an unhandled exception and never cancels siblings. This is strictly safer than gather because a programming error (not an agent failure) will still propagate correctly.

**One semaphore for screenshot mode.** The beta separates the browser concurrency semaphore from the LLM semaphore because in browser mode, each agent makes multiple LLM calls across multiple steps — an agent can be "running" (occupying a browser slot) but waiting for its per-step LLM call. In screenshot mode, each agent makes exactly one LLM call and then exits. The concurrency semaphore IS the LLM semaphore.

**Exponential backoff: 60 → 120 → 240.** The beta uses a fixed 60s sleep. Fixed sleeps are wrong because the provider's rate-limit window may need more time after successive failures. The total wait is at most 7 minutes (60+120+240) before raising, which is acceptable for a slow run but not an infinite hang.

**Screenshot runs ARE persisted.** The beta does not save screenshot results to disk — only browser runs are saved. We persist `SwarmResult` for both modes because: (a) our `models.py` already defines `SwarmResult` with `mode: Literal["screenshot", "browser"]`, (b) users running repeated tests want to compare runs, (c) cost tracking requires a record. Filename: `{timestamp}_{stem}.json` where `stem` is the image filename without extension.

**`_load_image` stays per-agent.** Ideally the image is read and encoded once before the swarm and passed as a `bytes` or `str` to each agent (matching the beta's pattern). This plan defers that optimization — it's a disk read, not an LLM call, and the latency is negligible vs network IO. A future pass should move `_load_image` to the swarm orchestrator and pass `image_data, media_type` directly into `run_screenshot_agent`.

**`UserType.label` stays as the persona field name.** The beta uses `Persona.name`. Our `models.py` already uses `UserType.label` and `AgentResult.user_type: str` stores the label string. No rename.

**Cost tracking is best-effort.** `litellm.completion_cost()` may not have pricing data for every model. The try/except falls back to `0.0`. This is better than always showing `$0.00` when cost data is available.

**LiteLLM model string format.** Config stores `"anthropic/claude-sonnet-4-20250514"`. LiteLLM accepts this format directly — the `anthropic/` prefix tells LiteLLM to route to Anthropic's API. For OpenAI models stored as `"openai/gpt-4o"`, LiteLLM similarly accepts the prefixed form. No stripping needed when passing to `acompletion`.

---

## Todo

### Phase 1 — `personas.py`

- [ ] Create `src/ux_swarm/personas.py`
- [ ] Define `USERS_JSON = LOCAL_DIR / "users.json"`
- [ ] Define `DEFAULT_USERS` list with the Krug-based default persona
- [ ] Implement `load_users(path=None) -> list[UserType]`
- [ ] Implement `distribute_users(users, n) -> list[UserType]`
- [ ] Implement `write_default_users() -> Path`

### Phase 2 — `agent.py` async refactor

- [ ] Add `litellm` to `pyproject.toml` dependencies (`uv add litellm`)
- [ ] Add `import asyncio`, `import litellm`, `litellm.suppress_debug_info = True` at top of `agent.py`
- [ ] Remove `import urllib.request`
- [ ] Remove `_OPENAI_COMPAT_ENDPOINTS`, `_call_anthropic`, `_call_openai_compat`, `_call_llm`
- [ ] Replace `run_screenshot_agent` with an `async def` version using `acompletion`
- [ ] Add `_RETRY_DELAYS = (60, 120, 240)` and implement retry loop with exponential backoff
- [ ] Add parse-failure fallback to synthetic `ScreenshotDecision` (completed=False, abandoned=True)
- [ ] Update return type to `tuple[ScreenshotDecision, int, int, float]` (adds cost)

### Phase 3 — `swarm.py`

- [ ] Create `src/ux_swarm/swarm.py`
- [ ] Implement `_run_agent` async inner function with per-agent exception catching + `CancelledError` re-raise
- [ ] Implement `run_screenshot_swarm` using `asyncio.TaskGroup` + `asyncio.Semaphore`
- [ ] Implement `_aggregate` with completion_rate, margin_of_error, user_breakdown, friction_points, total_cost
- [ ] Verify `SwarmResult.users` is count of successful agents, not `num_agents`

### Phase 4 — `main.py` wiring

- [ ] Add `import asyncio`, `import os`, `from rich.live import Live`, `from rich.text import Text` to imports
- [ ] Add `from ux_swarm.personas import load_users` and `from ux_swarm.swarm import run_screenshot_swarm`
- [ ] Remove `run_screenshot_agent` import and old `from ux_swarm.models import AgentResult`
- [ ] Add `_PROVIDER_ENV_VARS` dict and `_inject_api_key(provider, api_key)` helper
- [ ] Update `run` command body: inject key, load users, set up Live, call swarm, save SwarmResult
- [ ] Replace `_print_result` with `_print_swarm_result` (aggregate display)
- [ ] Update `RUN_DEFAULTS` to confirm `default_users: 20`
- [ ] Add `users` command with `--config` flag
- [ ] Update `main.py` imports: remove `run_screenshot_agent`, `AgentResult`, add `asyncio`, `os`, `Live`, `Text`

### Phase 5 — Validate

- [ ] Single-user smoke test
- [ ] Five-user test with progress counter check
- [ ] SwarmResult JSON structure check
- [ ] Twenty-user full run
- [ ] `swarm users` command
- [ ] `swarm users --config` command
- [ ] Two-type custom users.json test
- [ ] Error path tests (bad path, URL, no config, --verbose)
