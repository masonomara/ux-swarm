# Screenshot Agent — Implementation Plan

## Scope

One LLM call per simulated user: capture a screenshot of the target (URL or image
file), send it to the LLM with a task description and a user persona, parse the
structured response into `ScreenshotDecision` and `AgentResult`, display the result.

No concurrency. No browser interaction loop. No multi-step Playwright. Prove that the
LLM produces useful UX observations before building anything else.

---

## Current State

| File                       | Relevant state                                          |
| -------------------------- | ------------------------------------------------------- |
| `src/ux_swarm/models.py`   | `UserType`, `ScreenshotDecision`, `AgentResult` defined |
| `src/ux_swarm/config.py`   | `load_config()`, `check_chromium_installed()`, etc.     |
| `src/ux_swarm/main.py`     | `run` command is a stub — `pass` body, hidden           |
| `pyproject.toml`           | `playwright`, `rich`, `click`, `pydantic` — no litellm  |

---

## Files Changed

| File                        | Change                                                  |
| --------------------------- | ------------------------------------------------------- |
| `pyproject.toml`            | Add `litellm>=1.40`                                     |
| `src/ux_swarm/agents.py`    | New — screenshot capture, LLM call, result parsing      |
| `src/ux_swarm/main.py`      | Wire `run`, add `_preflight()`, add `_print_result()`   |

---

## Phase 1 — Dependencies

Add `litellm` to `pyproject.toml`:

```toml
dependencies = [
    "click>=8.1.8",
    "litellm>=1.40",
    "playwright>=1.58.0",
    "pydantic>=2.13.3",
    "requests>=2.32.5",
    "rich>=15.0.0",
]
```

Then run `uv sync`.

litellm is the only new dependency. It handles the provider-agnostic LLM call (OpenAI,
Anthropic, Gemini, DeepSeek) and normalizes the vision message format across providers.

---

## Phase 2 — `agents.py`

### File skeleton

```python
import base64
import json
import os
from pathlib import Path

import litellm
from rich.console import Console

from ux_swarm.models import AgentResult, ScreenshotDecision, UserType

console = Console()
```

---

### Default user personas

Define `DEFAULT_USER_TYPES` directly in `agents.py`. These are the personas used when
no custom personas file is provided. Weights are relative — used later for sampling
when running N agents. For now (single agent) we'll just pick index 0.

```python
DEFAULT_USER_TYPES: list[UserType] = [
    UserType(
        label="Casual",
        weight=3.0,
        description=(
            "A non-technical user who browses slowly and reads labels carefully. "
            "You get confused by jargon, non-obvious UI patterns, and cluttered layouts. "
            "You trust simple, clear language and give up quickly if you feel lost."
        ),
    ),
    UserType(
        label="Power User",
        weight=1.0,
        description=(
            "An experienced user who moves fast and has high expectations for efficiency. "
            "You scan rather than read. You notice when an interface is slower or more "
            "verbose than necessary and get frustrated by redundant confirmation dialogs."
        ),
    ),
    UserType(
        label="First-Time Visitor",
        weight=2.0,
        description=(
            "You have no prior context for this product. You rely entirely on what you "
            "can see on screen right now. You read headlines and CTAs literally and are "
            "uncertain what the site is for until it clearly tells you."
        ),
    ),
    UserType(
        label="Skeptical",
        weight=1.0,
        description=(
            "You are cautious. You read fine print, hesitate before submitting forms, "
            "and distrust anything that looks like a dark pattern or misleading CTA. "
            "You will abandon if something feels off, even if you can't say exactly why."
        ),
    ),
    UserType(
        label="Mobile Mindset",
        weight=2.0,
        description=(
            "You are accustomed to mobile apps: large tap targets, minimal typing, "
            "and gesture-driven layouts. You get frustrated by dense desktop UIs, "
            "small buttons, or forms that require excessive keyboard input."
        ),
    ),
]
```

---

### `capture_screenshot(target)`

Detects whether `target` is a local image file or a URL. For image files, reads bytes
directly. For URLs, uses Playwright to render the page and capture a PNG.

```python
def capture_screenshot(target: str) -> tuple[bytes, str]:
    """Return (image_bytes, media_type) for target (URL or image path)."""
    path = Path(target)
    if path.exists():
        media_types = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
        }
        media_type = media_types.get(path.suffix.lower(), "image/png")
        return path.read_bytes(), media_type

    if not (target.startswith("http://") or target.startswith("https://")):
        raise click.ClickException(
            f"Target must be a URL (http/https) or a path to an existing image file.\n"
            f"Got: {target}"
        )

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 800})
        try:
            page.goto(target, wait_until="networkidle", timeout=30_000)
        except Exception:
            page.goto(target, wait_until="load", timeout=30_000)
        screenshot_bytes = page.screenshot(type="png", full_page=False)
        browser.close()

    return screenshot_bytes, "image/png"
```

Two key decisions:
- `wait_until="networkidle"` catches dynamic content but times out on some pages.
  The `except` fallback retries with `"load"` so SSR pages don't fail.
- `full_page=False` captures only the viewport. Full-page screenshots at 1280px wide
  can be enormous and exceed provider image size limits.

---

### `_build_messages(image_bytes, media_type, task, user_type)`

Constructs the litellm-compatible message list. The system prompt embeds the persona
description. The user prompt embeds the task and the JSON schema. The image is
base64-encoded into a data URL in the `image_url` content block.

```python
_SYSTEM_PROMPT = """\
You are simulating a real user attempting to complete a task on a user interface.

Your persona:
{description}

You will be shown a screenshot of the interface. Analyze it through the eyes of this
persona and respond with a single JSON object that exactly matches this schema:

{{
  "target_element": "<the specific UI element you would interact with first, e.g. 'Sign up button' or 'Search input'>",
  "reasoning": "<your internal monologue as this persona: what you notice, what draws your attention, what confuses you>",
  "comment": "<one first-person sentence summarizing your overall experience, e.g. 'I could find the sign-up button but wasn't sure if I needed an account first.'>",
  "friction_observed": ["<specific UI problem>", "<another problem>"],
  "completed": <true if this screenshot shows a clear completion state — success message, confirmation page, etc. Otherwise false>,
  "abandoned": <true if nothing on screen indicates how to make progress toward the task and you would give up>,
  "abandonment_reason": "<one sentence explaining why you abandoned, or null if abandoned is false>"
}}

Rules:
- Respond with ONLY the JSON object. No markdown. No code fences. No explanation.
- friction_observed may be an empty array if the UI is clear.
- completed and abandoned cannot both be true.
- If the page is still loading or clearly broken, set abandoned to true and explain in abandonment_reason.
"""

_USER_PROMPT = "Task: {task}"


def _build_messages(
    image_bytes: bytes,
    media_type: str,
    task: str,
    user_type: UserType,
) -> list[dict]:
    b64 = base64.standard_b64encode(image_bytes).decode()
    data_url = f"data:{media_type};base64,{b64}"

    system = _SYSTEM_PROMPT.format(description=user_type.description)
    user_text = _USER_PROMPT.format(task=task)

    return [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": user_text},
            ],
        },
    ]
```

The system prompt uses `{{` / `}}` to escape literal braces in the JSON schema example
so they survive `.format()`. The schema is prose, not a machine-readable spec — this
is intentional because embedding the full Pydantic JSON Schema is noisy and models
respond better to annotated examples.

---

### `run_screenshot_agent(index, image_bytes, media_type, task, user_type, model, api_key)`

Calls the LLM, parses the response, maps it to `AgentResult`.

```python
def run_screenshot_agent(
    index: int,
    image_bytes: bytes,
    media_type: str,
    task: str,
    user_type: UserType,
    model: str,
    api_key: str,
) -> AgentResult:
    messages = _build_messages(image_bytes, media_type, task, user_type)

    response = litellm.completion(
        model=model,
        messages=messages,
        api_key=api_key,
        response_format={"type": "json_object"},
        timeout=60,
    )

    raw = response.choices[0].message.content
    try:
        decision = ScreenshotDecision.model_validate_json(raw)
    except Exception as exc:
        raise click.ClickException(
            f"LLM returned invalid JSON.\n"
            f"Model: {model}\n"
            f"Response: {raw[:500]}\n"
            f"Error: {exc}"
        ) from exc

    usage = response.usage or {}
    input_tokens = getattr(usage, "prompt_tokens", 0) or 0
    output_tokens = getattr(usage, "completion_tokens", 0) or 0

    return AgentResult(
        agent_index=index,
        user_type=user_type.label,
        completed=decision.completed,
        abandoned=decision.abandoned,
        abandonment_reason=decision.abandonment_reason,
        friction_points=decision.friction_observed,
        comment=decision.comment,
        steps_taken=1,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=0.0,  # stubbed — add cost lookup in a later phase
    )
```

`response_format={"type": "json_object"}` is supported by OpenAI, Anthropic (via
litellm's translation layer), and Gemini. DeepSeek supports it on chat models. litellm
silently ignores it for providers that don't support it — the JSON schema in the prompt
is the actual guarantee, not this flag.

Token counts use `getattr(..., 0)` because litellm normalizes usage objects across
providers but the field names can be `None` when the provider omits them.

---

## Phase 3 — `_preflight()` in `main.py`

Add before the `run` command. Validates config is present and an API key is
resolvable, then returns the merged config dict with the key injected.

```python
import os
from ux_swarm.config import check_chromium_installed, load_config, provider_env_var

def _preflight(*, require_browser: bool = False) -> dict:
    config = load_config()
    if not config.get("provider"):
        raise click.ClickException("No config found. Run: swarm config")

    api_key = config.get("api_key") or os.environ.get(
        provider_env_var(config["provider"]), ""
    )
    if not api_key:
        raise click.ClickException(
            f"No API key found. Run: swarm config  (or set "
            f"{provider_env_var(config['provider'])} in your environment)"
        )

    config["api_key"] = api_key

    if require_browser:
        try:
            ok = check_chromium_installed()
        except Exception:
            ok = False
        if not ok:
            raise click.ClickException(
                "Chromium is required for browser mode. Run: swarm config"
            )

    return config
```

---

## Phase 4 — Wire `run` in `main.py`

Replace the `pass` stub. Unhide the command. For this phase: one agent, one persona,
one screenshot.

```python
from ux_swarm.agents import DEFAULT_USER_TYPES, capture_screenshot, run_screenshot_agent

@cli.command()  # remove hidden=True
@click.argument("target")
@click.argument("task")
@click.option("--users", default=None, type=int, help="Number of simulated users")
@click.option("--max-steps", default=None, type=int, help="Max steps per agent (browser only)")
@click.option("--viewport", default=None, type=int, help="Viewport width in pixels")
@click.option("--verbose", is_flag=True, help="Show full tracebacks on error")
@click.pass_context
def run(ctx, target, task, users, max_steps, viewport, verbose):
    """Run simulated users against a URL or screenshot image."""
    config = _preflight(require_browser=False)

    with _console.status("Capturing screenshot…"):
        image_bytes, media_type = capture_screenshot(target)

    user_type = DEFAULT_USER_TYPES[0]

    with _console.status(f"Running agent [{user_type.label}]…"):
        result = run_screenshot_agent(
            index=0,
            image_bytes=image_bytes,
            media_type=media_type,
            task=task,
            user_type=user_type,
            model=config["model"],
            api_key=config["api_key"],
        )

    _print_result(result)
```

The `--users`, `--max-steps`, `--viewport` options are wired but unused in this phase.
They stay so the interface is stable when we add multi-agent and browser mode.

---

## Phase 5 — `_print_result()` in `main.py`

Display the single agent result. Designed to extend naturally when we add multi-agent
output — this function will become one row in a larger summary.

```python
def _print_result(result: AgentResult) -> None:
    if result.completed:
        status = "[green]Completed[/]"
    elif result.abandoned:
        status = "[red]Abandoned[/]"
    else:
        status = "[yellow]Incomplete[/]"

    _console.print(f"\n[bold]{result.user_type}[/] — {status}\n")
    _console.print(f'  [italic]"{result.comment}"[/]\n')

    if result.friction_points:
        _console.print("[bold]Friction observed:[/]")
        for point in result.friction_points:
            _console.print(f"  [yellow]•[/] {point}")
        _console.print()

    if result.abandoned and result.abandonment_reason:
        _console.print(f"[red]Abandoned:[/] {result.abandonment_reason}\n")

    _console.print(
        f"[dim]Tokens: {result.input_tokens} in / {result.output_tokens} out[/]\n"
    )
```

---

## Phase 6 — Todo

### Dependencies

- [ ] Add `litellm>=1.40` to `pyproject.toml`
- [ ] Run `uv sync`

### `agents.py`

- [ ] Create `src/ux_swarm/agents.py`
- [ ] Add imports: `base64`, `json`, `litellm`, `click`, `Path`, `Console`, models
- [ ] Define `DEFAULT_USER_TYPES` — five `UserType` instances
- [ ] Implement `capture_screenshot(target)` — file path branch: read bytes + detect media type
- [ ] Implement `capture_screenshot(target)` — URL branch: Playwright launch, `networkidle` with `load` fallback, viewport PNG
- [ ] Implement `capture_screenshot(target)` — invalid target branch: `ClickException` with clear message
- [ ] Define `_SYSTEM_PROMPT` — persona slot, JSON schema prose, rules section
- [ ] Define `_USER_PROMPT` — task slot only
- [ ] Implement `_build_messages()` — format system prompt, encode image as data URL, build content list
- [ ] Implement `run_screenshot_agent()` — litellm call with `response_format={"type": "json_object"}`, 60s timeout
- [ ] Implement `run_screenshot_agent()` — parse response with `ScreenshotDecision.model_validate_json()`
- [ ] Implement `run_screenshot_agent()` — JSON parse failure → `ClickException` showing raw response (first 500 chars)
- [ ] Implement `run_screenshot_agent()` — map `ScreenshotDecision` → `AgentResult` (steps_taken=1, cost=0.0)
- [ ] Implement `run_screenshot_agent()` — extract token counts from `response.usage` with `getattr` guards

### `main.py`

- [ ] Add imports: `os`, `DEFAULT_USER_TYPES`, `capture_screenshot`, `run_screenshot_agent`, `provider_env_var`
- [ ] Implement `_preflight()` — config existence check, API key resolution (config → env var), `require_browser` branch
- [ ] Implement `_print_result()` — status line, quoted comment, friction list, abandonment reason, token count footer
- [ ] Update `run` command — remove `hidden=True`, remove `pass`, call `_preflight()` → `capture_screenshot()` → `run_screenshot_agent()` → `_print_result()`

---

## Phase 7 — Smoke Tests

**Screenshot from image file:**
```
swarm run path/to/screenshot.png "Sign up for an account"
```
- Agent status, comment, and friction points appear
- Token counts shown at the bottom
- No crash

**Screenshot from URL (requires Playwright):**
```
swarm run https://example.com "Find the pricing page"
```
- Screenshot is captured silently (status spinner, then gone)
- Agent result appears

**No config:**
```
swarm run https://example.com "Find the pricing page"
# (with no .swarm/config.json)
```
- `Error: No config found. Run: swarm config`

**Invalid target:**
```
swarm run not-a-url-or-file "Do something"
```
- `Error: Target must be a URL (http/https) or a path to an existing image file.`

**Bad API key (in config):**
- litellm raises `AuthenticationError` → propagates as unhandled exception
- Add to a future phase: catch `litellm.AuthenticationError` in `run_screenshot_agent` and raise `ClickException("Invalid API key…")`

**Verify output quality (manual):**
- Screenshot of a simple, clear UI → `completed: true` or clear friction list
- Screenshot of a confusing UI → `friction_observed` is non-empty and specific
- Screenshot with a success/confirmation page → `completed: true`

---

## What This Unlocks

Once this phase is done:

1. **Multi-agent** — loop `run_screenshot_agent` N times with different personas,
   collect `list[AgentResult]`, aggregate into `SwarmResult`. The functions are already
   designed to take `index` and `user_type` as parameters.

2. **Results to disk** — write `SwarmResult` to `.swarm/reports/{timestamp}.json` after
   the loop.

3. **Better results display** — completion rate, per-persona breakdown, deduplicated
   friction themes — all derivable from `list[AgentResult]`.

4. **Browser mode** — swap `capture_screenshot()` for a Playwright interaction loop
   that takes per-step screenshots and feeds them to a `run_browser_agent()` function
   with `BrowserDecision` instead of `ScreenshotDecision`.
