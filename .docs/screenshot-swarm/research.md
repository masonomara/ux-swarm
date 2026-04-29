# Research: Single Screenshot Agent

_Source: `masonomara/ux-swarm--beta` + local repo audit_

---

## What Exists Today

The project is fully scaffolded. Configuration, CLI routing, data models, and the setup wizard are all done. The one thing that doesn't exist: any LLM call logic. The `run` command is a literal `pass`.

```python
@cli.command(hidden=True)
def run(ctx, target, task, users, max_steps, viewport, verbose):
    """Run a swarm of simulated users against a URL or screenshot image."""
    pass
```

That's the entire implementation. Everything else is in place and working.

---

## The Data Model (models.py)

The `ScreenshotDecision` is already defined and ready:

```python
class ScreenshotDecision(BaseModel):
    target_element: str          # "Submit button in modal footer"
    reasoning: str               # Why this element
    comment: str                 # First-person one-sentence summary — from this call, not a second one
    friction_observed: list[str] # UX friction points; empty list if none
    completed: bool
    abandoned: bool
    abandonment_reason: str | None
```

The `AgentResult` wraps it for aggregation:

```python
class AgentResult(BaseModel):
    agent_index: int
    user_type: str
    completed: bool
    abandoned: bool
    abandonment_reason: str | None
    friction_points: list[str]
    comment: str
    steps_taken: int   # always 1 in screenshot mode
    input_tokens: int
    output_tokens: int
    cost: float
```

And `SwarmResult` is the full output written to `.swarm/reports/`:

```python
class SwarmResult(BaseModel):
    timestamp: str               # ISO 8601
    mode: Literal["screenshot", "browser"]
    target: str
    task: str
    model: str
    users: int
    completion_rate: float
    margin_of_error: float       # 1.96 * sqrt(p*(1-p)/n)
    user_breakdown: dict[str, float]  # label → completion rate
    friction_points: list[str]        # raw from all agents; not deduplicated
    total_cost: float
    individual_results: list[AgentResult]
```

---

## The Config System (config.py)

Config is stored in JSON, with a global and local layer:

- Global: `~/.config/ux-swarm/config.json`
- Local: `.swarm/config.json`

Both are merged at load time with last-write-wins. The config file stores:

```json
{
  "provider": "anthropic",
  "api_key": "sk-ant-...",
  "model": "anthropic/claude-sonnet-4-20250514"
}
```

The model field uses a `provider/model-id` format. The calling code will need to strip the prefix to get the actual model ID for API calls.

Providers supported: `openai`, `anthropic`, `gemini`, `deepseek`. The wizard validates API keys against each provider's live API before saving, and pulls the live model list so the list never goes stale.

`load_config()` is the entry point for the run command to read all config:

```python
config = load_config()
provider = config.get("provider")       # "anthropic"
api_key = config.get("api_key")         # "sk-ant-..."
model = config.get("model")             # "anthropic/claude-sonnet-4-20250514"
```

---

## The CLI Surface (main.py + cli.py)

The `SmartGroup` auto-routes bare URLs and image paths to `run`, so:

```bash
swarm https://example.com "find the login button"
```

is identical to:

```bash
swarm run https://example.com "find the login button"
```

Detection logic:

```python
extensions = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
return arg.startswith(("http://", "https://")) or any(
    arg.endswith(ext) for ext in extensions)
```

The `run` command signature:

```python
def run(ctx, target, task, users, max_steps, viewport, verbose):
```

- `target` — URL or image path (positional, required)
- `task` — task description (positional, required)
- `--users` — number of agents (default: 20 from `RUN_DEFAULTS`)
- `--max-steps` — browser mode only (default: 3)
- `--viewport` — browser mode only (default: 1280)
- `--verbose` — show full tracebacks

For the single screenshot agent proof-of-concept, only `target`, `task`, and `users` matter. The rest are browser-mode concerns.

---

## The UserType System (models.py)

```python
class UserType(BaseModel):
    label: str
    weight: float       # relative, not a probability
    description: str    # injected verbatim into the LLM system prompt
```

Weights are relative (e.g., 3, 1, 1 — not 0.6, 0.2, 0.2). The sampling logic doesn't exist yet either. For the single-agent proof-of-concept, one hardcoded `UserType` is fine.

There's no `users.json` file yet — the user type definitions will need defaults. The `description` field is the key one: it gets injected into the system prompt verbatim to define the persona.

---

## What the Single Screenshot Agent Needs to Do

Based on the models and config system, here's the complete flow:

**1. Load config**

```python
config = load_config()
model_string = config["model"]   # "anthropic/claude-sonnet-4-20250514"
api_key = config["api_key"]
provider = config["provider"]
```

**2. Acquire the image**

- If `target` is an image path: read from disk, base64-encode
- If `target` is a URL: the beta repo doesn't specify, but the intent is to take a screenshot first (hence the Playwright check in the wizard) — for the proof-of-concept, a URL could be passed directly to the LLM if the provider supports URL images, or a screenshot could be taken via Playwright

**3. Build the prompt**

System prompt: persona description + UX evaluation instructions + JSON schema from `ScreenshotDecision.model_json_schema()`

User prompt: the task + the image

**4. Call the LLM with vision**

The model format `"provider/model-id"` suggests a routing layer is expected. The exact HTTP call depends on provider. Anthropic example:

- Endpoint: `https://api.anthropic.com/v1/messages`
- Image content block with `type: "base64"`, `media_type`, `data`
- `response_format` or prompt-based JSON extraction

**5. Parse into ScreenshotDecision**

```python
decision = ScreenshotDecision.model_validate_json(llm_response_text)
```

**6. Map to AgentResult**

```python
result = AgentResult(
    agent_index=0,
    user_type=user_type.label,
    completed=decision.completed,
    abandoned=decision.abandoned,
    abandonment_reason=decision.abandonment_reason,
    friction_points=decision.friction_observed,
    comment=decision.comment,
    steps_taken=1,
    input_tokens=...,
    output_tokens=...,
    cost=...,
)
```

---

## Key Design Decisions Already Made

**`comment` is first-person, from the same call** — the docstring says "comes from this call, not a second one." No second LLM call to summarize. The LLM writes it as part of the `ScreenshotDecision`.

**`friction_observed` is raw, not deduplicated** — `SwarmResult.friction_points` comment says "raw from all agents; not deduplicated." Deduplication is a later problem.

**`margin_of_error` has a formula** — `1.96 * sqrt(p*(1-p)/n)`. This is the 95% confidence interval for a proportion. Hardcoded in the model comment.

**`steps_taken` is always 1 in screenshot mode** — hardcoded in the `AgentResult` comment. The field exists to unify the model with browser mode.

**Concurrency defaults** — `max_concurrent_screenshot: 20`. For the proof-of-concept, 1 agent with no concurrency is the target.

**`ensure_swarm_structure()`** creates `.swarm/reports/` — this should be called before writing output.

---

## What Does NOT Exist Yet

- Any LLM HTTP call (no `requests` or `httpx` usage in the codebase)
- Image loading / base64 encoding
- Prompt construction
- JSON schema injection into the prompt
- Token counting and cost calculation
- Any output display (Rich tables, etc.)
- User type defaults or sampling
- Results JSON writing
- Any async code (all `pass` stubs)

The `requests` library is listed as a dependency but is not imported anywhere in the source. `urllib` is used in `config.py` for provider API calls — the LLM call itself will likely use `requests` or the Anthropic SDK.

---

## File Map

```
src/ux_swarm/
├── __init__.py      — empty
├── cli.py           — SmartGroup: routes URLs/images to run
├── config.py        — load_config, save_config, run_config_wizard, fetch_provider_models
├── main.py          — CLI commands: cli, config, help, run (stub)
├── menu.py          — Arrow-key select() with GoBack support
└── models.py        — UserType, ScreenshotDecision, BrowserAction, BrowserDecision, AgentResult, SwarmResult
```

The `wizard.cpython-311.pyc` in `__pycache__` with no matching `wizard.py` — there was a `wizard.py` that was merged into `config.py` at some point.

---

## Minimum Implementation Surface for the Proof-of-Concept

To prove the core loop works with one LLM call:

1. Fill in `run()` in `main.py`
2. Detect mode: image path → skip Playwright; URL → either screenshot or pass URL to LLM
3. Load config, get model/key/provider
4. Load image → base64
5. Build system + user prompts
6. HTTP call to provider with vision input
7. Parse JSON → `ScreenshotDecision`
8. Print result to terminal

No async needed. No concurrency. No `AgentResult`/`SwarmResult` aggregation. No file output. Just: does the LLM return a valid `ScreenshotDecision` with real UX observations?

The Pydantic schema generation is the key unlock:

```python
schema = ScreenshotDecision.model_json_schema()
```

Inject that schema into the system prompt and the LLM knows exactly what shape to return.
