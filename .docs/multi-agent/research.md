# Research: Multi-Agent Run Pipeline (Beta Repo)

_Source: `masonomara/ux-swarm--beta` — all files read verbatim_

---

## Overview

The beta repo implements a complete multi-agent swarm pipeline. Two execution modes share most infrastructure but diverge at the runner level:

- **Screenshot mode** — N agents make parallel LLM vision calls against the same static base64-encoded image. No browser. No navigation state. One LLM call per agent per run.
- **Browser mode** — N agents each get an isolated Playwright browser context. Each agent navigates a live URL across up to `max_steps` LLM-driven interaction cycles.

Both modes use the same personas system, the same concurrency primitives (`asyncio.Semaphore`), the same `call_llm_with_retry` wrapper, the same Rich live display infrastructure, and the same CLI entry point.

---

## File Map

```
swarm/
├── cli.py              — Entry point, arg parsing, env validation, SmartGroup routing
├── runners.py          — High-level coordinator: Live setup, delegates to swarm runners, save_report()
├── browser_swarm.py    — N agents × Playwright contexts, per-step LLM vision calls
├── screenshot_swarm.py — N agents × parallel LLM calls on a static screenshot
├── types.py            — All Pydantic models, TypedDicts, dataclasses, constants
├── output.py           — Rich terminal UI: live status table, final summaries
├── utils.py            — call_llm_with_retry, margin_of_error, persona_rates, top_counts
├── personas.py         — load_personas, distribute_personas, write_default_personas
└── prompts.py          — System/user prompt templates and builder functions
```

---

## 1. Constants and Defaults (types.py)

Every numeric constant in the pipeline is defined here. No magic numbers elsewhere.

```python
DEFAULT_MODEL = "gpt-4o"

DEFAULT_AGENTS_SCREENSHOT = 100   # 100 agents → roughly ±10% confidence interval
DEFAULT_AGENTS_BROWSER = 20       # real browser sessions; memory is the bottleneck

MAX_CONCURRENT_SCREENSHOT_AGENTS = 20   # bottleneck is rate limits, not memory
MAX_CONCURRENT_BROWSER_AGENTS = 5       # more than ~5 parallel browsers thrashes RAM
MAX_CONCURRENT_LLM_CALLS = 3           # extra guard against provider rate limits

DEFAULT_VIEWPORT_WIDTH = 1280
DEFAULT_VIEWPORT_HEIGHT = 720

DEFAULT_MAX_STEPS = 3   # max click/type/scroll cycles per browser agent

TOP_CONFUSION_POINTS_LIMIT = 5
TOP_FAILURE_REASONS_LIMIT = 10
```

Config paths:

```python
LOCAL_DIR = Path(".swarm")
LOCAL_CONFIG = LOCAL_DIR / "config.json"
GLOBAL_CONFIG = Path.home() / ".config" / "uxswarm" / "config.json"
PERSONAS_JSON = Path(".swarm/personas.json")
```

Action constants — tuples for prompt display order, frozensets for O(1) membership:

```python
SCREENSHOT_ACTION_SEQUENCE: tuple[str, ...] = ("click", "scroll", "type", "confused", "leave")
BROWSER_ACTION_SEQUENCE: tuple[str, ...] = ("click", "type", "scroll", "done", "give_up")

SUCCESS_ACTIONS: frozenset[str] = frozenset({"click", "type", "scroll"})
BROWSER_TERMINAL_ACTIONS: frozenset[str] = frozenset({"done", "give_up"})
ALL_BROWSER_ACTIONS: frozenset[str] = frozenset(BROWSER_ACTION_SEQUENCE)
```

`SUCCESS_ACTIONS` is shared between screenshot and browser modes. In screenshot mode, any of click/type/scroll means the UI was discoverable enough that the agent chose to interact. In browser mode, terminal actions are `done` and `give_up`; `done` with `success: true` is the only outcome that increments the completion count.

Provider registry (used by both the config wizard and `utils.provider_env_var()`):

```python
PROVIDERS: list[dict[str, str]] = [
    {"name": "OpenAI",        "key": "openai",    "env": "OPENAI_API_KEY",    "model": "gpt-4o"},
    {"name": "Anthropic",     "key": "anthropic", "env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-4-20250514"},
    {"name": "Google Gemini", "key": "gemini",    "env": "GEMINI_API_KEY",    "model": "gemini/gemini-2.5-flash"},
    {"name": "DeepSeek",      "key": "deepseek",  "env": "DEEPSEEK_API_KEY",  "model": "deepseek/deepseek-chat"},
]
```

Fallback model lists used when the live API call in `fetch_provider_models` fails:

```python
MODELS: dict[str, list[str]] = {
    "openai":    ["gpt-4o", "gpt-4o-mini", "o3", "o4-mini"],
    "anthropic": ["claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-haiku-4-5-20251001"],
    "gemini":    ["gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"],
    "deepseek":  ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"],
}
```

---

## 2. Pydantic Models and TypedDicts (types.py)

### SwarmParams (NamedTuple)

Fully-resolved parameters after CLI flags → config file → hard-coded defaults are merged:

```python
class SwarmParams(NamedTuple):
    model: str
    num_agents_browser: int
    num_agents_screenshot: int
    max_steps: int
    viewport: int
    concurrent_browser: int
    concurrent_screenshot: int
```

Note: browser and screenshot agent counts are separate fields even though `--agents` is a single CLI flag. The resolution function `_resolve_swarm_parameters` maps the same `agents` flag into both `num_agents_browser` and `num_agents_screenshot` but sources them from the same config key `default_agents`.

### SwarmError

```python
class SwarmError(Exception):
    def __init__(self, message: str, hint: str | None = None, cause: BaseException | None = None):
        ...
        self.hint = hint
        self.cause = cause
```

Used at every failure boundary. `hint` is user-facing, `cause` is the underlying exception (shown when `--verbose`).

### Persona (Pydantic BaseModel)

```python
class Persona(BaseModel):
    name: str
    prompt: str
    weight: float = 1.0

    @field_validator("weight")
    @classmethod
    def weight_must_be_positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError(f"weight must be > 0, got {v}")
        return v
```

`weight` is relative (not a probability). Distribution is computed at run time by normalizing against the sum of all active persona weights.

### AgentResponse (Pydantic BaseModel)

The expected LLM JSON output shape for screenshot mode:

```python
class AgentResponse(BaseModel):
    action: str
    target: str
    confidence: float      # validated 0.0–1.0
    reasoning: str
    confusion_points: list[str]

    @field_validator("confidence")
    @classmethod
    def confidence_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"confidence must be between 0.0 and 1.0, got {v}")
        return v
```

### ScreenshotAgentResult (TypedDict)

What `run_single_agent()` returns to `run_screenshot_swarm()`:

```python
class ScreenshotAgentResult(TypedDict):
    persona: str
    response: AgentResponse
    cost: float
    raw: str    # raw LLM response text, kept for debugging
```

### ScreenshotSwarmResult (Pydantic BaseModel)

The aggregated output of a complete screenshot swarm run:

```python
class ScreenshotSwarmResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_agents: int
    task: str
    model: str
    temperature: float
    discoverability_rate: float
    margin_of_error: float
    action_distribution: dict[str, int]    # "action: target" → count, sorted by frequency
    persona_breakdown: dict[str, float]    # persona name → success rate
    confusion_clusters: list[str]          # top N unique confusion points
    total_cost: float
```

Note: screenshot runs do NOT have a `url` field — image path is not included in the aggregated output.

### BrowserAgentResult (TypedDict)

What `run_browser_agent()` returns as a plain dict:

```python
class BrowserAgentResult(TypedDict):
    persona: str
    success: bool
    steps: int
    actions: list[str]          # e.g. ["click: #login-btn", "type: username"]
    errors: list[str]
    final_result: str | None    # last action string, or None
    reasoning: list[str]        # "thinking" fields from each LLM step
    urls_visited: list[str]
    duration: float             # wall-clock seconds
    cost: float
```

### BrowserSwarmResult (Pydantic BaseModel)

```python
class BrowserSwarmResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    total_agents: int
    task: str
    url: str
    model: str
    completion_rate: float
    margin_of_error: float
    persona_breakdown: dict[str, float]
    avg_steps_to_completion: float
    failure_reasons: dict[str, int]       # error string → count
    total_cost: float
    individual_results: list[dict[str, Any]]   # raw BrowserAgentResult dicts
```

### AgentState (dataclass)

Per-agent mutable state for the live display:

```python
@dataclass
class AgentState:
    persona: str
    status: str = "waiting"
    detail: str = ""
    step: int = 0
    max_steps: int = DEFAULT_MAX_STEPS
```

### SwarmState

Centralised mutable state for all agents in one browser run. Not thread-safe — asyncio only:

```python
class SwarmState:
    def __init__(self, agent_personas: list[str], task: str, url: str, max_steps: int = DEFAULT_MAX_STEPS):
        self.task = task
        self.url = url
        self._agents: dict[int, AgentState] = {
            agent_id: AgentState(persona=persona_name, max_steps=max_steps)
            for agent_id, persona_name in enumerate(agent_personas)
        }
        self._completed_ids: set[int] = set()

    @property
    def total(self) -> int: ...
    @property
    def completed(self) -> int: ...
    def set(self, agent_id: int, status: str, detail: str = "") -> None: ...
    def set_step(self, agent_id: int, step: int) -> None: ...
    @property
    def agents(self) -> dict[int, AgentState]: ...
```

The `set()` method auto-records an agent as completed when status is `"complete"` or `"failed"`. The set prevents double-counting if `set()` is called twice with a terminal status.

---

## 3. Personas: Loading, Validation, Distribution (personas.py)

### load_personas()

Resolution order: caller-supplied path → `.swarm/personas.json` → `DEFAULT_PERSONAS`.

A broken file always raises `SwarmError` rather than silently falling back. Only personas with `"active": true` (or no `"active"` key — default is active) are included.

```python
def load_personas(path: str | None = None) -> list[Persona]:
    if path:
        personas_file = Path(path)
    elif PERSONAS_JSON.exists():
        personas_file = PERSONAS_JSON
    else:
        return DEFAULT_PERSONAS
    # ... load, parse, validate ...
    return active_personas if active_personas else DEFAULT_PERSONAS
```

### distribute_personas()

Assigns personas to exactly `num_agents` slots proportional to weights:

```python
def distribute_personas(personas: list[Persona], num_agents: int) -> list[Persona]:
    total_weight = sum(persona.weight for persona in personas)
    assigned_personas: list[Persona] = []

    for persona in personas:
        slot_count = max(1, round((persona.weight / total_weight) * num_agents))
        assigned_personas.extend([persona] * slot_count)

    if len(assigned_personas) > num_agents:
        assigned_personas = assigned_personas[:num_agents]

    while len(assigned_personas) < num_agents:
        assigned_personas.append(personas[0])

    return assigned_personas
```

Key details:
- Every persona gets at least 1 slot (`max(1, round(...))`).
- Rounding overshoot: trimmed from the tail (`[:num_agents]`).
- Rounding undershoot: padded by repeating `personas[0]` (the first persona, by convention the most common archetype).
- The return value is a flat list of `Persona` objects in weight-proportional quantity.

### Default Personas (types.py)

```python
DEFAULT_PERSONAS = [
    Persona(name="Scanner",           weight=0.3,  prompt="You scan pages quickly..."),
    Persona(name="Methodical Reader", weight=0.25, prompt="You read every label..."),
    Persona(name="Pattern Matcher",   weight=0.25, prompt="You rely on standard web conventions..."),
    Persona(name="Minimalist",        weight=0.2,  prompt="You want the absolute minimum..."),
]
```

Weights sum to 1.0 but that's not required — they are normalized at distribution time.

---

## 4. CLI Entry Point (cli.py)

### SmartGroup

A custom `click.Group` subclass that rewrites bare URLs and image paths as `swarm run <target> ...`:

```python
class SmartGroup(click.Group):
    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and self._first_arg_looks_like_url(args[0]):
            args = ["run"] + args
        return super().parse_args(ctx, args)

    @staticmethod
    def _first_arg_looks_like_url(arg: str) -> bool:
        return arg.startswith(("http://", "https://")) or "." in arg
```

### Image Detection

```python
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

def _is_image_path(candidate: str) -> bool:
    return Path(candidate).suffix.lower() in _IMAGE_EXTENSIONS
```

### Image Path Resolution

```python
def _resolve_image(path_str: str) -> Path:
    candidate = Path(path_str)
    if candidate.exists():
        return candidate.resolve()
    matches = list(Path(".").rglob(candidate.name))
    if len(matches) == 1:
        return matches[0].resolve()
    if len(matches) > 1:
        raise SwarmError(f"Multiple files named '{candidate.name}' found.", ...)
    raise SwarmError(f"Image not found: {path_str}")
```

If a bare filename is given and exactly one match is found recursively, it resolves automatically.

### Preflight Validation (_run_preflight)

Runs before every `swarm run` invocation:

1. Checks that at least one config file exists (`LOCAL_CONFIG` or `GLOBAL_CONFIG`).
2. For browser mode only: checks Playwright Chromium is installed.
3. Checks for a non-empty API key (env var takes precedence over config file for CI/CD override).

Returns the loaded config dict on success; calls `raise SystemExit(1)` on failure.

### Parameter Resolution

Priority chain: CLI flag → config file key → hard-coded default:

```python
def _resolve_param(flag_value, config_key, default, config, cast: type = str):
    if flag_value is not None:
        return cast(flag_value)
    return cast(config.get(config_key, default))
```

All seven `SwarmParams` fields are resolved this way in `_resolve_swarm_parameters()`. Browser and screenshot modes use the same `--agents` flag but different defaults (20 vs 100) and different config keys (both resolve from `default_agents`).

### API Key Injection

LiteLLM reads API keys from environment variables only. The resolved key is injected into `os.environ` immediately before the `asyncio.run()` call:

```python
def _inject_api_key(config: dict) -> None:
    api_key = str(config.get("api_key", ""))
    provider = str(config.get("provider", "openai"))
    api_key_env_var = provider_env_var(provider)
    if api_key and not os.environ.get(api_key_env_var):
        os.environ[api_key_env_var] = api_key
```

Existing env vars are not overwritten — a pre-set `OPENAI_API_KEY` takes precedence over the config file.

### run command

Hidden from help (SmartGroup exposes it implicitly):

```python
@main.command(hidden=True)
@click.argument("url")
@click.argument("task")
@click.option("--agents", default=None, type=int, ...)
@click.option("--model", default=None, ...)
@click.option("--max-concurrent", default=None, type=int, ...)
@click.option("--max-steps", default=None, type=int, ...)
@click.option("--viewport", default=None, type=int, ...)
@click.option("-v", "--verbose", is_flag=True, ...)
def run(url, task, agents, model, max_concurrent, max_steps, viewport, verbose):
    is_image = _is_image_path(url)
    config = _run_preflight(require_browser=not is_image)
    params = _resolve_swarm_parameters(config, agents, model, max_concurrent, max_steps, viewport)
    _inject_api_key(config)

    if is_image:
        image_path = _resolve_image(url)
        asyncio.run(run_screenshot(...))
    else:
        asyncio.run(run_browser(...))
```

Errors: `SwarmError` is caught and printed cleanly. `KeyboardInterrupt` exits with code 130 (POSIX convention). `atexit` registers `stty sane` to restore terminal state after Rich Live's raw key reads.

---

## 5. Runners: High-Level Coordinator (runners.py)

### run_browser()

```python
async def run_browser(config, url, task, num_agents, model, max_concurrent, max_steps, viewport):
    all_personas = load_personas()
    assigned_personas = distribute_personas(all_personas, num_agents)
    persona_names = [persona.name for persona in assigned_personas]

    state = SwarmState(
        agent_personas=persona_names,
        task=task,
        url=url,
        max_steps=max_steps,
    )

    with Live(build_swarm_table(state), console=console, auto_refresh=False, refresh_per_second=1) as live_display:
        result = await run_browser_swarm(
            url=url, task=task, model=model, num_agents=num_agents,
            max_concurrent=max_concurrent, max_steps=max_steps, viewport=viewport,
            personas=all_personas, state=state, live=live_display,
        )

    console.print()
    print_browser_swarm_result(result)
    save_report(result, url)
```

The `SwarmState` is created here and passed all the way through to individual agents. `Live` is set up with `auto_refresh=False` — every update is explicit (agents call `live.update(..., refresh=True)` after state changes).

### run_screenshot()

```python
async def run_screenshot(config, image_path, task, num_agents, model, max_concurrent):
    console.print(f"[dim]{image_path}[/]\n")

    with Live(build_screenshot_progress(0, num_agents), console=console, auto_refresh=False) as live:
        def on_done(completed: int, total: int) -> None:
            live.update(build_screenshot_progress(completed, total), refresh=True)

        result = await run_screenshot_swarm(
            screenshot_b64=base64.b64encode(image_path.read_bytes()).decode(),
            task=task, model=model, num_agents=num_agents,
            max_concurrent=max_concurrent, on_agent_done=on_done,
        )

    console.print()
    print_screenshot_swarm_result(result)
```

The image is read and base64-encoded once here, then the same string is passed to all N agents. No per-agent disk reads.

Screenshot runs do NOT call `save_report()`. Only browser runs persist results to disk.

### save_report()

```python
def save_report(result: BrowserSwarmResult, url: str) -> None:
    ensure_swarm_structure()
    reports_dir = Path(".swarm/reports")

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
    domain = url.split("//")[-1].split("/")[0].replace(".", "-")
    filename = f"{timestamp}_{domain}.json"

    (reports_dir / filename).write_text(json.dumps(result.model_dump(), indent=2, default=str))
    console.print(f"[dim].swarm/reports/{filename}[/]\n")
```

Filename format: `YYYY-MM-DD_HH-MM_domain-with-dashes.json`. Content: `BrowserSwarmResult.model_dump()` serialized with `indent=2`. `default=str` handles any non-serializable types (e.g., datetimes). The `swarm report` browser command parses this filename format: `stem.rsplit("_", 1)[0]` strips the domain for display, then `.replace("_", " ")` makes a human-readable date.

---

## 6. Screenshot Swarm Pipeline (screenshot_swarm.py)

### Single Agent: run_single_agent()

One agent = one LLM vision call. No loop, no steps.

```python
async def run_single_agent(screenshot_b64, task, persona, model, temperature=0.4) -> ScreenshotAgentResult:
    system_prompt = build_screenshot_system_prompt(persona.prompt)
    user_prompt = build_screenshot_user_prompt(task)

    raw_response = await call_llm_with_retry(
        model=model,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
            ]},
        ],
        response_format={"type": "json_object"},
    )

    raw_text = llm_response.choices[0].message.content or ""
    call_cost = completion_cost(completion_response=llm_response, model=model)

    try:
        agent_decision = AgentResponse.model_validate_json(raw_text)
    except (ValidationError, json.JSONDecodeError):
        agent_decision = AgentResponse(
            action="confused",
            target="unknown",
            confidence=0.0,
            reasoning=raw_text[:500],
            confusion_points=["Agent response was not valid JSON"],
        )

    return {"persona": persona.name, "response": agent_decision, "cost": call_cost, "raw": raw_text}
```

Key details:
- No `semaphore` passed here — concurrency is controlled one level up in `run_screenshot_swarm`.
- Parse failure falls back to a synthetic `AgentResponse` with `action="confused"`. `confused` is not in `SUCCESS_ACTIONS`, so it counts as a non-discoverable outcome.
- `response_format={"type": "json_object"}` is passed via `**kwargs` through to `litellm.acompletion`.
- Image is sent as `data:image/png;base64,...` regardless of actual file format. The base64 encoding happens in `runners.py` once, outside all agents.

### Swarm Orchestrator: run_screenshot_swarm()

```python
async def run_screenshot_swarm(screenshot_b64, task, model, num_agents=100, temperature=0.4,
                               max_concurrent=20, personas=None, personas_path=None,
                               on_agent_done=None) -> ScreenshotSwarmResult:
    loaded_personas = personas or load_personas(personas_path)
    agent_assignments = distribute_personas(loaded_personas, num_agents)

    concurrency_limiter = asyncio.Semaphore(max_concurrent)
    completed = 0

    async def run_agent_with_concurrency_limit(persona):
        nonlocal completed
        async with concurrency_limiter:
            result = await run_single_agent(screenshot_b64, task, persona, model, temperature)
        completed += 1
        if on_agent_done:
            on_agent_done(completed, num_agents)
        return result

    raw_results = await asyncio.gather(
        *[run_agent_with_concurrency_limit(persona) for persona in agent_assignments],
        return_exceptions=True,
    )

    successful_results = [r for r in raw_results if isinstance(r, dict)]
    failed_results = [r for r in raw_results if isinstance(r, BaseException)]

    if len(successful_results) == 0:
        raise RuntimeError(f"All {num_agents} agents failed. First error: {failed_results[0]}")

    # ... aggregation ...
```

Concurrency model: `asyncio.gather` launches all N coroutines at once but each acquires the semaphore before calling the LLM. The semaphore limits active LLM calls to `max_concurrent` at any moment. `return_exceptions=True` means one agent failure becomes an exception object in the results list, not a cancellation of the entire gather.

The `completed` counter and `on_agent_done` callback are called after releasing the semaphore (`async with concurrency_limiter:` block exits before the counter increments). This is intentional: the live display updates as soon as an agent finishes, not when it starts.

### Aggregation Logic

```python
success_count = 0
action_counts: Counter[str] = Counter()
persona_pairs: list[tuple[str, bool]] = []
all_confusion_points: list[str] = []
total_cost = 0.0

for result in successful_results:
    agent_decision = result["response"]
    agent_succeeded = agent_decision.action in SUCCESS_ACTIONS
    if agent_succeeded: success_count += 1

    # Key = "action: target" — captures both what and where
    action_counts[f"{agent_decision.action}: {agent_decision.target}"] += 1
    persona_pairs.append((persona_name, agent_succeeded))
    all_confusion_points.extend(agent_decision.confusion_points)
    total_cost += call_cost

persona_breakdown = persona_rates(persona_pairs)
non_empty_points = [p for p in all_confusion_points if p]
top_confusion_points = list(top_counts(non_empty_points, TOP_CONFUSION_POINTS_LIMIT).keys())

discoverability_rate = success_count / num_successful_agents
moe = margin_of_error(discoverability_rate, num_successful_agents)

return ScreenshotSwarmResult(
    total_agents=num_successful_agents,
    discoverability_rate=discoverability_rate,
    margin_of_error=moe,
    action_distribution=dict(action_counts.most_common()),
    persona_breakdown=persona_breakdown,
    confusion_clusters=top_confusion_points,
    total_cost=total_cost,
    ...
)
```

`discoverability_rate` = agents that returned click/type/scroll / total successful agents. Failed agents (LLM error, network failure) are excluded from the denominator entirely — they are just not counted. `total_agents` in the result is the number of *successful* agents, not `num_agents`.

`action_distribution` keys look like `"click: the blue Sign Up button"` — the full `action: target` string, not just the action. Sorted by frequency (`.most_common()`).

`confusion_clusters` is the top 5 most-common non-empty confusion_point strings across all agents, by raw string equality (no semantic deduplication).

---

## 7. Browser Swarm Pipeline (browser_swarm.py)

### Playwright Setup

```python
# Timeouts (Playwright API unit: milliseconds)
PAGE_LOAD_TIMEOUT_MS = 30_000
INITIAL_PAGE_LOAD_WAIT_MS = 2_000      # cap for JS-heavy pages; not a forced delay
POST_NAVIGATION_WAIT_MS = 1_500
ELEMENT_SELECTION_TIMEOUT_MS = 5_000
NEW_TAB_LOAD_TIMEOUT_MS = 10_000
SCROLL_DISTANCE_PX = 500
```

One browser process, N isolated contexts:

```python
async with async_playwright() as playwright:
    browser = await playwright.chromium.launch(headless=True)
    # ... all agents share this browser ...
    await browser.close()
```

Each agent gets its own context:

```python
browser_context = await browser.new_context(viewport={
    "width": viewport,
    "height": DEFAULT_VIEWPORT_HEIGHT,
})
```

Context is closed in a `finally` block — even if the agent raises.

### LLM Semaphore

Browser mode has a second semaphore separate from the browser concurrency semaphore:

```python
MAX_CONCURRENT_LLM_CALLS = 3   # defined in types.py

_llm_semaphore: asyncio.Semaphore | None = None

def _get_llm_semaphore() -> asyncio.Semaphore:
    global _llm_semaphore
    if _llm_semaphore is None:
        _llm_semaphore = asyncio.Semaphore(MAX_CONCURRENT_LLM_CALLS)
    return _llm_semaphore
```

The semaphore is lazily initialized because `asyncio.Semaphore` must be created inside a running event loop. This semaphore is passed to `call_llm_with_retry` on every step of every agent. Even if 5 browser agents are running in parallel (the browser concurrency limit), only 3 can be waiting for LLM responses simultaneously.

Screenshot mode does not use this semaphore — it uses the `max_concurrent` semaphore directly.

### Single Browser Agent: run_browser_agent()

```python
async def run_browser_agent(browser, url, task, persona, model, temperature=0.4,
                            max_steps=DEFAULT_MAX_STEPS, agent_id=0, viewport=1280,
                            state=None, live=None) -> BrowserAgentResult:
```

Per-agent state:

```python
actions_taken: list[str] = []
errors: list[str] = []
urls_visited: list[str] = []
reasoning: list[str] = []
success = False
steps_completed = 0
total_cost = 0.0
start_time = time.monotonic()
```

Step loop:

```python
for step_index in range(max_steps):
    steps_completed = step_index + 1
    state.set_step(agent_id, steps_completed)
    update_display("scanning", f"step {steps_completed}/{max_steps} — reading page")

    # Viewport-only screenshot (full_page=False)
    screenshot_bytes = await page.screenshot(full_page=False)
    screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

    user_prompt = build_browser_user_prompt(step, max_steps, page.url, actions_taken)

    raw_response = await call_llm_with_retry(
        model=model, temperature=temperature,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": [
                {"type": "text", "text": user_prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_b64}"}},
            ]},
        ],
        semaphore=_get_llm_semaphore(),
        on_rate_limit=lambda retry_number: update_display("waiting", f"rate limited — retry {retry_number}/3 in 60s"),
        timeout=120,
        response_format={"type": "json_object"},
    )
```

Action dispatch:

```python
if action in BROWSER_TERMINAL_ACTIONS:
    if action == "done":
        success = action_data.get("success", True)
        update_display("complete", thinking)
    else:
        success = False
        update_display("failed", thinking)
    break

# Non-terminal:
if action == "click":
    page_count_before_click = len(browser_context.pages)
    try:
        await page.locator(selector).first.click(timeout=ELEMENT_SELECTION_TIMEOUT_MS)
    except Exception:
        await page.click(selector, timeout=ELEMENT_SELECTION_TIMEOUT_MS)  # fallback path
    page = await _follow_page_after_click(browser_context, page, page_count_before_click)

elif action == "type":
    await page.locator(selector).first.fill(text_to_type)

elif action == "scroll":
    await page.evaluate(f"window.scrollBy(0, {SCROLL_DISTANCE_PX})")
```

Click handles new tab detection:

```python
async def _follow_page_after_click(browser_context, page, page_count_before_click):
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=POST_NAVIGATION_WAIT_MS)
    except Exception:
        pass

    pages_after_click = browser_context.pages
    if len(pages_after_click) > page_count_before_click:
        new_tab = pages_after_click[-1]
        await new_tab.wait_for_load_state("domcontentloaded", timeout=NEW_TAB_LOAD_TIMEOUT_MS)
        return new_tab
    else:
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=ELEMENT_SELECTION_TIMEOUT_MS)
        except Exception:
            pass
        return page
```

Return value is a plain dict (matching `BrowserAgentResult` TypedDict):

```python
return {
    "persona": persona.name,
    "success": success,
    "steps": steps_completed,
    "actions": actions_taken,
    "errors": errors,
    "final_result": actions_taken[-1] if actions_taken else None,
    "reasoning": reasoning,
    "urls_visited": urls_visited,
    "duration": round(duration_seconds, 2),
    "cost": total_cost,
}
```

`CancelledError` is explicitly re-raised in the except block — swallowing it would leave `TaskGroup`/`gather` hanging. This is the pattern used throughout the codebase.

### Browser Swarm Orchestrator: run_browser_swarm()

```python
async def run_browser_swarm(url, task, model, num_agents=20, temperature=0.4,
                            max_concurrent=5, max_steps=DEFAULT_MAX_STEPS, viewport=1280,
                            personas=None, personas_path=None, on_agent_done=None,
                            state=None, live=None) -> BrowserSwarmResult:

    loaded_personas = personas or load_personas(personas_path)
    agent_assignments = distribute_personas(loaded_personas, num_agents)
    browser_concurrency_semaphore = asyncio.Semaphore(max_concurrent)

    results: list[BrowserAgentResult] = []
    agent_errors: list[BaseException] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)

        async def run_single_agent(agent_index, persona):
            async with browser_concurrency_semaphore:
                try:
                    result = await run_browser_agent(...)
                    results.append(result)
                    if on_agent_done:
                        on_agent_done(result, len(results), num_agents)
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    agent_errors.append(error)

        await asyncio.gather(*[
            run_single_agent(index, persona)
            for index, persona in enumerate(agent_assignments)
        ])

        await browser.close()

    return _aggregate_results(results, agent_errors, num_agents, task, url, model)
```

Unlike screenshot mode, browser mode uses `asyncio.gather` without `return_exceptions=True`. Instead, exceptions are caught inside `run_single_agent` and appended to `agent_errors`. `CancelledError` is re-raised to propagate cancellation correctly.

### Browser Aggregation: _aggregate_results()

```python
def _aggregate_results(results, errors, num_agents, task, url, model) -> BrowserSwarmResult:
    if len(results) == 0:
        first_error = errors[0] if errors else None
        # ... intelligent hint based on error text ...
        raise SwarmError(f"all {num_agents} agents failed", hint=hint, cause=first_error)

    num_successful = sum(1 for r in results if r["success"])
    completion_rate = num_successful / len(results)
    moe = margin_of_error(completion_rate, len(results))

    persona_success_pairs = [(r["persona"], r["success"]) for r in results]
    breakdown_by_persona = persona_rates(persona_success_pairs)

    # Average steps only for successful agents — failed agents always exhaust max_steps
    steps_for_successful_agents = [r["steps"] for r in results if r["success"]]
    avg_steps = sum(steps_for_successful_agents) / len(steps_for_successful_agents) if steps_for_successful_agents else 0.0

    # Strip "Step N: " prefix from errors before counting
    failure_error_strings = []
    step_prefix_re = re.compile(r"^Step \d+: ")
    for result in results:
        if not result["success"]:
            failure_error_strings.extend(
                step_prefix_re.sub("", error) for error in result["errors"]
            )

    total_cost = sum(r["cost"] for r in results)

    return BrowserSwarmResult(
        total_agents=len(results),
        completion_rate=completion_rate,
        margin_of_error=moe,
        persona_breakdown=breakdown_by_persona,
        avg_steps_to_completion=avg_steps,
        failure_reasons=top_counts(failure_error_strings, TOP_FAILURE_REASONS_LIMIT),
        total_cost=total_cost,
        individual_results=list(results),
        ...
    )
```

Key differences from screenshot aggregation:
- `completion_rate` denominator is `len(results)` (all agents that didn't crash), not `num_agents`.
- `avg_steps_to_completion` excludes failed agents — they exhaust `max_steps` by definition.
- Error hint detection: checks for "auth", "rate", "not found" in the first error's text.
- All agent errors raise `SwarmError` if zero results; screenshot mode raises `RuntimeError`.

---

## 8. LLM Call Infrastructure (utils.py)

### call_llm_with_retry()

```python
async def call_llm_with_retry(*, model, temperature, messages, semaphore=None,
                              on_rate_limit=None, **kwargs) -> Any:
    from litellm import acompletion
    from litellm.exceptions import RateLimitError

    MAX_ATTEMPTS = 4

    for attempt in range(MAX_ATTEMPTS):
        try:
            if semaphore is not None:
                async with semaphore:
                    return await acompletion(model=model, temperature=temperature,
                                            messages=messages, **kwargs)
            return await acompletion(model=model, temperature=temperature,
                                     messages=messages, **kwargs)
        except RateLimitError:
            if attempt >= MAX_ATTEMPTS - 1:
                raise
            if on_rate_limit is not None:
                on_rate_limit(attempt + 1)
            await asyncio.sleep(60)

    raise AssertionError("Unreachable")
```

- 4 total attempts (3 retries).
- Sleeps 60 seconds between attempts on `RateLimitError` (typical provider reset window).
- The semaphore is acquired inside each attempt — if the call fails with a rate limit, the semaphore is released before sleeping, allowing other callers to proceed.
- All other exceptions propagate immediately (no retry).
- `**kwargs` are passed directly to `acompletion`. This is how `response_format`, `timeout`, etc. are forwarded.
- `litellm.suppress_debug_info = True` is set in `browser_swarm.py` to suppress LiteLLM's verbose startup output.

### margin_of_error()

```python
def margin_of_error(rate: float, total: int) -> float:
    """95% confidence interval half-width: 1.96 * sqrt(p*(1-p)/n)."""
    if total <= 0:
        return 0.0
    return 1.96 * math.sqrt(rate * (1 - rate) / total)
```

### persona_rates()

```python
def persona_rates(pairs: Sequence[tuple[str, bool]]) -> dict[str, float]:
    """Compute success rate per persona from (name, did_succeed) pairs."""
    outcomes_by_persona: dict[str, list[bool]] = {}
    for persona_name, did_succeed in pairs:
        outcomes_by_persona.setdefault(persona_name, []).append(did_succeed)
    return {
        persona_name: sum(outcomes) / len(outcomes)
        for persona_name, outcomes in outcomes_by_persona.items()
    }
```

### top_counts()

```python
def top_counts(items: Sequence[str], limit: int) -> dict[str, int]:
    return dict(Counter(items).most_common(limit))
```

### fetch_provider_models()

Makes a live HTTP request to each provider's model list endpoint. Falls back to the `MODELS` dict on any non-auth failure. Raises `ProviderAuthError` on HTTP 401/403. Used only in the config wizard, not at run time.

---

## 9. Prompt Templates (prompts.py)

### Constants

```python
SCREENSHOT_ACTIONS = "|".join(SCREENSHOT_ACTION_SEQUENCE)  # "click|scroll|type|confused|leave"
BROWSER_ACTIONS = "|".join(BROWSER_ACTION_SEQUENCE)        # "click|type|scroll|done|give_up"
```

### PRODUCTION_RULES (injected into every system prompt)

```
You are evaluating a user interface. Apply these usability principles:

1. Don't make users think — navigation and actions should be obvious at a glance
2. Every screen should have a clear visual hierarchy guiding the eye
3. Buttons should look clickable, links should look like links
4. Users scan, they don't read — labels and CTAs must work at a glance
5. The most important action on the page should be the most visually prominent
6. Users should always know where they are and how to get back
7. If you're confused, that confusion is the finding — describe exactly what confuses you
```

### Screenshot System Prompt

Assembly: `persona_prompt` + `PRODUCTION_RULES` + `SCREENSHOT_TASK_FRAMING` + `SCREENSHOT_RESPONSE_FORMAT`, joined with `\n\n`.

Response format instruction:

```
Respond in this JSON format:
{
    "action": "click|scroll|type|confused|leave",
    "target": "description of the element you would interact with",
    "confidence": 0.0 to 1.0,
    "reasoning": "why you chose this action",
    "confusion_points": ["list of things that were unclear"]
}
```

### Screenshot User Prompt

```python
def build_screenshot_user_prompt(task: str) -> str:
    return SCREENSHOT_USER_TASK.format(task=task)
# Expands to: "Your task: {task}\n\nLook at the screenshot and describe exactly what you would do."
```

### Browser System Prompt

Assembly: `persona_prompt` + `BROWSER_TASK_FRAMING.format(task=task)` + `BROWSER_RESPONSE_FORMAT`, joined with `\n\n`.

The task is embedded in the system prompt (not per-step user turn) because it doesn't change between steps — this keeps it in scope as conversation history grows.

Response format instruction:

```
Respond in JSON:
{
    "thinking": "what you see and what to do",
    "action": "click|type|scroll|done|give_up",
    "selector": "CSS selector or text= selector",
    "text": "text to type (type action only)",
    "success": true or false (done/give_up only)
}
```

### Browser User Prompt (per step)

```python
def build_browser_user_prompt(step, max_steps, current_url, actions_taken) -> str:
    return BROWSER_USER_STEP.format(
        step=step,
        max_steps=max_steps,
        current_url=current_url,
        step_history=_format_action_history(actions_taken),
    )
```

`_format_action_history` returns the last 5 actions formatted as:

```
Recent actions:
  click: #login-btn
  type: username
```

Or empty string if no actions yet.

---

## 10. Live Display (output.py)

### Console Setup

```python
console = Console(stderr=True, highlight=False)   # progress/warnings — stays clean when stdout is piped
out = Console(highlight=False)                    # structured results — safe to pipe or redirect
```

All live display and status messages go to `stderr`. Final results go to `stdout`.

### Status Styles

```python
STATUS_STYLES = {
    "waiting":    "dim",
    "navigating": "cyan",
    "scanning":   "yellow",
    "acting":     "blue",
    "complete":   "green",
    "failed":     "red",
}
```

### build_swarm_table() — Browser Live Display

Builds a new `Rich.Table` on every call (called from agent `update_display()` closures and the initial Live setup):

Columns: `Agent` (20ch, bold), `Status` (12ch), `Step` (6ch, right-align), `Detail` (no_wrap).

Step counter is hidden for waiting/complete/failed agents — shown only for `navigating`, `scanning`, `acting`.

A summary row is appended after a section break: `"" | "{completed}/{total}" | "" | "agents complete"`.

### build_screenshot_progress() — Screenshot Live Display

```python
def build_screenshot_progress(completed: int, total: int) -> Text:
    return Text(f"  {completed} / {total}  agents complete", style="dim")
```

A single line of dim text. Updated by the `on_done` callback after each agent finishes.

### Rate Color Thresholds

```python
RATE_THRESHOLD_GOOD = 0.8
RATE_THRESHOLD_WARNING = 0.5
```

Green ≥ 80%, Yellow ≥ 50%, Red < 50%. Applied to both `discoverability_rate` and `completion_rate` in the final summaries.

### print_screenshot_swarm_result()

Prints to `out` (stdout):
1. Bold header with task name.
2. Separator line.
3. Rate as `{rate:.0%} ±{moe:.0%}  {total_agents} agents  ${cost:.2f}`, colored by rate.
4. Action distribution table: `action: target | count | share%`.
5. Persona breakdown table (sorted best-first, zero rates dimmed not red).
6. Confusion points list prefixed with ` · `.

### print_browser_swarm_result()

Prints to `out` (stdout):
1. `{hostname}  ·  {task}` (hostname extracted from URL).
2. Completion rate with agent count and margin of error.
3. Persona breakdown table.
4. Top failure reasons: `{count}×  {reason}` (first 120 chars of first line only).

---

## 11. Complete Data Flow

### Screenshot Mode

```
CLI args
  ↓
SmartGroup.parse_args → injects "run" prefix if bare URL/image path
  ↓
run() [cli.py]
  ↓
_is_image_path() → True
_run_preflight(require_browser=False) → loads config, checks API key
_resolve_swarm_parameters() → SwarmParams
_inject_api_key() → os.environ[OPENAI_API_KEY] = key
_resolve_image() → absolute Path
  ↓
asyncio.run(run_screenshot()) [runners.py]
  ↓
load_personas() → list[Persona]  (file or defaults)
distribute_personas() → list[Persona] (length = num_agents)
image_path.read_bytes() → base64.b64encode() → screenshot_b64 (str, one copy shared)
Live(build_screenshot_progress(0, N)) → starts Rich live display
  ↓
run_screenshot_swarm() [screenshot_swarm.py]
  ↓
asyncio.Semaphore(max_concurrent)
asyncio.gather(N × run_agent_with_concurrency_limit(), return_exceptions=True)
  ↓ (each agent, up to max_concurrent in parallel)
run_single_agent()
  build_screenshot_system_prompt(persona.prompt)  [prompts.py]
  build_screenshot_user_prompt(task)              [prompts.py]
  call_llm_with_retry(model, temp, messages, response_format=json_object)  [utils.py]
    → litellm.acompletion() → provider API
  AgentResponse.model_validate_json(raw_text)
    → on failure: synthetic AgentResponse(action="confused", ...)
  → ScreenshotAgentResult dict
  ↓ (after semaphore release)
on_done(completed, total) → live.update(build_screenshot_progress(), refresh=True)
  ↓ (after all agents)
_aggregate_results:
  discoverability_rate = success_count / num_successful_agents
  margin_of_error = 1.96 * sqrt(p*(1-p)/n)
  action_distribution = Counter("action: target").most_common()
  persona_breakdown = persona_rates(pairs)
  confusion_clusters = top_counts(non_empty_points, 5).keys()
  ↓
ScreenshotSwarmResult
  ↓
print_screenshot_swarm_result() [output.py] → stdout
(no save_report — screenshot runs not persisted)
```

### Browser Mode

```
CLI args
  ↓
run() [cli.py]
  ↓
_is_image_path() → False
_run_preflight(require_browser=True) → additionally checks Playwright Chromium
_resolve_swarm_parameters() → SwarmParams
_inject_api_key()
  ↓
asyncio.run(run_browser()) [runners.py]
  ↓
load_personas() → list[Persona]
distribute_personas() → list[Persona] (length = num_agents)
SwarmState(agent_personas=[...], task, url, max_steps) → initializes N AgentState objects
Live(build_swarm_table(state)) → starts Rich live table display
  ↓
run_browser_swarm() [browser_swarm.py]
  ↓
asyncio.Semaphore(max_concurrent)  → browser concurrency
_get_llm_semaphore()               → LLM concurrency (MAX_CONCURRENT_LLM_CALLS=3)
async_playwright() → browser = chromium.launch(headless=True)
asyncio.gather(N × run_single_agent())
  ↓ (each agent, up to max_concurrent in parallel)
run_browser_agent()
  browser.new_context(viewport={width, height=720})
  page.goto(url, wait_until="domcontentloaded", timeout=30s)
  page.wait_for_load_state("networkidle", timeout=2s)  [catches exception]
  build_browser_system_prompt(persona.prompt, task)    [prompts.py]
  for step in range(max_steps):
    state.set_step(agent_id, step+1)
    update_display("scanning", ...)
    page.screenshot(full_page=False) → base64
    build_browser_user_prompt(step, max_steps, url, actions)  [prompts.py]
    call_llm_with_retry(semaphore=_llm_semaphore, timeout=120, ...)
      → litellm.acompletion()
    json.loads(raw_text) → action_data
    if action in BROWSER_TERMINAL_ACTIONS: break
    execute action (click / type / scroll)
    _follow_page_after_click() if click
    urls_visited.append(page.url) if new url
  browser_context.close() [finally]
  → BrowserAgentResult dict
  ↓ (inside run_single_agent wrapper)
results.append(result)
on_agent_done(result, len(results), num_agents)  [if provided]
  ↓ (after all agents)
browser.close()
_aggregate_results():
  if len(results) == 0: raise SwarmError(...)
  completion_rate = num_successful / len(results)
  margin_of_error = 1.96 * sqrt(p*(1-p)/n)
  persona_breakdown = persona_rates(pairs)
  avg_steps_to_completion = mean(steps for successful agents only)
  failure_reasons = top_counts(stripped_errors, 10)
  ↓
BrowserSwarmResult(individual_results=[all raw dicts])
  ↓
print_browser_swarm_result() [output.py] → stdout
save_report(result, url):
  ensure_swarm_structure()
  filename = f"{YYYY-MM-DD_HH-MM}_{domain-with-dashes}.json"
  .swarm/reports/{filename}.write(result.model_dump(), indent=2, default=str)
```

---

## 12. Concurrency Architecture Summary

| Layer | Mechanism | Limit | Where Set |
|---|---|---|---|
| Browser agents (screenshot mode) | `asyncio.Semaphore` | `max_concurrent` (default 20) | `run_screenshot_swarm()` |
| LLM calls (screenshot mode) | Same semaphore as browser agents | Same | `run_screenshot_swarm()` |
| Browser contexts (browser mode) | `asyncio.Semaphore` | `max_concurrent` (default 5) | `run_browser_swarm()` |
| LLM calls (browser mode) | Second `asyncio.Semaphore` | 3 (`MAX_CONCURRENT_LLM_CALLS`) | `_get_llm_semaphore()` module-level |
| Gather error handling (screenshot) | `return_exceptions=True` | — | `asyncio.gather()` |
| Gather error handling (browser) | `try/except` inside inner fn | — | `run_single_agent()` closure |

In browser mode, with 5 browser agents running, each at step N, all 5 could be waiting for LLM responses simultaneously. The LLM semaphore (limit 3) ensures at most 3 of those 5 are actively in an LLM call at once. The other 2 wait on the semaphore without blocking the event loop.

`asyncio.CancelledError` is explicitly re-raised everywhere it can appear. The comments in the code emphasize this: "swallowing CancelledError leaves TaskGroup/gather hanging forever."

---

## 13. Error Handling Patterns

| Scenario | Behavior |
|---|---|
| Screenshot agent parse failure | Fallback `AgentResponse(action="confused", ...)`, not an exception |
| Screenshot agent LLM error | Exception captured by `return_exceptions=True`, excluded from results |
| All screenshot agents fail | `RuntimeError` raised from `run_screenshot_swarm` |
| Browser agent action error | Appended to `errors` list, agent continues to next step |
| Browser agent unexpected error | Appended to `agent_errors`, agent slot excluded from results |
| All browser agents fail | `SwarmError` raised with context-aware hint from `_aggregate_results` |
| Rate limit | 60s sleep, up to 3 retries, `on_rate_limit` callback updates display |
| Ctrl-C | `KeyboardInterrupt` caught in `run()`, prints "interrupted", exits 130 |
| `CancelledError` | Always re-raised |
| `SwarmError` in CLI | Message + optional hint printed; `--verbose` adds traceback; exits 1 |

---

## 14. Key Design Decisions

**Single base64 encoding.** The image is read and encoded once in `runners.run_screenshot()`, then the same string is passed to all N agents. No per-agent disk reads.

**Viewport-only screenshots.** `page.screenshot(full_page=False)` captures only the visible viewport. The comment explains: "full_page includes offscreen content the LLM can't act on."

**Task in system prompt for browser mode.** The task is embedded in the browser system prompt (not the per-step user turn) so it stays in scope as conversation history grows and doesn't add to per-step token costs.

**Action history: last 5 only.** `_format_action_history` caps at 5 recent actions to keep per-step prompts short.

**No `SwarmResult` unification.** Screenshot and browser results are separate Pydantic models (`ScreenshotSwarmResult` and `BrowserSwarmResult`) — no shared base class. The discriminating field in the old proof-of-concept `SwarmResult.mode` does not exist in the beta.

**Screenshot runs not persisted.** `save_report()` is only called from `run_browser()`. Screenshot runs produce terminal output only.

**`completed` counter is nonlocal.** In `run_screenshot_swarm`, the counter is a bare integer in closure scope with `nonlocal completed`. The increment happens after the semaphore is released, so the display update reflects actual completion, not in-flight agents.

**`litellm.suppress_debug_info = True`.** Set at module level in `browser_swarm.py` to suppress LiteLLM's verbose startup output. Not set in `screenshot_swarm.py` (screenshot mode imports it through `call_llm_with_retry` lazily).

**Persona[0] as padding default.** When `distribute_personas` undershoots due to rounding, it pads with `personas[0]`. The comment says "by convention, the most common type" — in the defaults, that's `Scanner` with weight 0.3.

**Step prefix stripping in failure reasons.** `re.compile(r"^Step \d+: ")` strips `Step 3: ` from error strings before counting, so the same underlying error from different steps merges into one count.

**`avg_steps_to_completion` excludes failures.** Failed agents exhaust `max_steps` by definition, so including them would pull the average toward the maximum and obscure how many steps successful users actually needed.
