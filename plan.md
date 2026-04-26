# Plan: Single Screenshot Agent

One LLM call. Accept an image path, call the LLM with it, parse into `ScreenshotDecision`, print the result, save to disk. No concurrency, no Playwright, no aggregation.

---

## Scope

**In scope:**

- Image path input (PNG, JPG, JPEG, WebP, GIF)
- One hardcoded default `UserType` persona
- One synchronous LLM call via `urllib` (consistent with `config.py`)
- Provider routing: Anthropic, OpenAI, Gemini
- Parse response into `ScreenshotDecision`
- Map to `AgentResult`
- Print result to terminal with Rich
- Save result to `.swarm/reports/`

**Not in scope:**

- URL targets (require Playwright — browser mode)
- Multiple agents / concurrency
- Persona sampling or `users.json`
- Cost calculation (stubbed as `0.0`)
- `--users`, `--max-steps`, `--viewport` flags

---

## Files

**Create:** `src/ux_swarm/agent.py`  
**Modify:** `src/ux_swarm/main.py` — fill in `run()`

Nothing else changes.

---

## Todo

### Phase 1 — Create `src/ux_swarm/agent.py`

- [ ] Create file with imports: `base64`, `json`, `urllib.request`, `urllib.error`, `Path` from `pathlib`, `click`, `UserType` and `ScreenshotDecision` from `ux_swarm.models`
- [ ] Add `_MIME_TYPES` dict mapping extensions to MIME strings
- [ ] Add `_media_type(path: Path) -> str`
- [ ] Add `_load_image(target: str) -> tuple[str, str]` — reads file, raises `ClickException` if missing
- [ ] Add `_build_system_prompt(user_type: UserType) -> str` — persona + heuristics + injected JSON schema
- [ ] Add `_OPENAI_COMPAT_ENDPOINTS` dict (openai, gemini)
- [ ] Add `_call_anthropic(model_id, api_key, system, image_data, media_type, user_prompt) -> tuple[str, int, int]` — urllib POST, extract text + tokens
- [ ] Add `_call_openai_compat(endpoint, model_id, api_key, system, image_data, media_type, user_prompt) -> tuple[str, int, int]` — urllib POST with `response_format: json_object`, extract text + tokens
- [ ] Add `_call_llm(...)` — routes to `_call_anthropic` or `_call_openai_compat`, raises `ClickException` for unknown provider
- [ ] Add `run_screenshot_agent(target, task, user_type, provider, model_id, api_key) -> tuple[ScreenshotDecision, int, int]` — orchestrates load → prompt → call → parse

### Phase 2 — Fill in `run()` in `src/ux_swarm/main.py`

- [ ] Add imports at top of file: `Path` from `pathlib`, `datetime` + `timezone` from `datetime`, `AgentResult` + `UserType` from `ux_swarm.models`, `run_screenshot_agent` from `ux_swarm.agent`, `LOCAL_DIR` from `ux_swarm.config`
- [ ] Write `_print_result(target, task, model_id, decision, in_tok, out_tok) -> None`
  - [ ] Opening `_console.rule(style="dim")`
  - [ ] Header: `{filename} — "{task}"`
  - [ ] Comment in bold
  - [ ] `Target` and `Reason` labeled fields
  - [ ] `Friction` section with bullet list — omit section entirely if `friction_observed` is empty
  - [ ] `Completed` / `Abandoned` status line — dim when false, normal when true
  - [ ] Token footer dimmed: `{model_id} · {in_tok} in / {out_tok} out tokens`
  - [ ] Closing `_console.rule(style="dim")`
- [ ] Fill in `run()` body
  - [ ] Load config with `load_config()`, raise `ClickException` if `model` or `api_key` missing
  - [ ] Parse `provider` and `model_id` by splitting `config["model"]` on `"/"`
  - [ ] Guard: raise `ClickException` for URL targets with redirect message
  - [ ] Instantiate default `UserType` (Krug-based description, label `"Default User"`)
  - [ ] Call `run_screenshot_agent()` inside `_console.status()` spinner showing filename + task
  - [ ] Wrap call in try/except: re-raise if `--verbose`, else wrap in `ClickException`
  - [ ] Map `ScreenshotDecision` fields to `AgentResult` (`steps_taken=1`, `cost=0.0`)
  - [ ] Call `ensure_swarm_structure()` to create `.swarm/reports/` if needed
  - [ ] Write `result.model_dump_json(indent=2)` to `.swarm/reports/{timestamp}_screenshot.json`
  - [ ] Call `_print_result()`

### Phase 3 — Validate

- [ ] Run against a real screenshot: `swarm screenshot.png "find the sign up button"`
- [ ] Confirm `ScreenshotDecision` fields in terminal output are coherent and useful
- [ ] Check actual output token count — raise `max_tokens` if consistently near 1024, lower if consistently under 500
- [ ] Confirm `.swarm/reports/` contains saved JSON with all `AgentResult` fields populated correctly
- [ ] Test: missing image path → clear error message
- [ ] Test: URL as target → clear error message with instruction to pass an image path
- [ ] Test: no config → clear error message pointing to `swarm config`
- [ ] Test: `--verbose` flag surfaces full traceback on parse failure

---

## Step 1 — `src/ux_swarm/agent.py`

Four functions. Reads the image file, builds prompts, calls the LLM, returns parsed output. No display logic. No config loading — receives what it needs as arguments.

### `_media_type(path: Path) -> str`

Maps file extension to MIME type string.

```python
_MIME_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
}

def _media_type(path: Path) -> str:
    return _MIME_TYPES.get(path.suffix.lower(), "image/png")
```

Defaults to `image/png` for unrecognized extensions.

### `_load_image(target: str) -> tuple[str, str]`

Reads image from disk, returns `(base64_data, media_type)`. Raises `click.ClickException` if the file doesn't exist.

```python
def _load_image(target: str) -> tuple[str, str]:
    path = Path(target)
    if not path.exists():
        raise click.ClickException(f"Image not found: {target}")
    media = _media_type(path)
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return data, media
```

MIME type and base64 data travel together as a pair from this point — never separated.

### `_build_system_prompt(user_type: UserType) -> str`

Three sections: persona (archetype name + behavioral description), UX heuristics, JSON schema. Task framing belongs in the user turn, not here.

**System prompt vs user turn:** Every LLM call has two structural layers. The system prompt is set once and defines the model's role, rules, and output format — standing instructions that frame every response. The user turn is the actual request: the task plus the image. The model treats the system prompt as invariant context and the user turn as what it's being asked to do right now.

For this agent the shape is:

```
System:  [who you are] + [UX heuristics] + [JSON schema]
User:    [task text] + [image]
```

Persona and output rules go in the system prompt because they don't change between calls. The task goes in the user turn because it's the request.

```python
def _build_system_prompt(user_type: UserType) -> str:
    schema = json.dumps(ScreenshotDecision.model_json_schema(), indent=2)
    return (
        f"You are a synthetic user in a UX test.\n\n"
        f"You are playing the role of: {user_type.label}\n"
        f"{user_type.description}\n\n"
        "Apply these UX heuristics when evaluating the interface:\n"
        "1. Good UI does not make users think\n"
        "2. Visual hierarchy should guide the eye to the next action\n"
        "3. Interactive elements should look interactive\n"
        "4. Users scan — they do not read\n"
        "5. The most important action should be the most prominent\n"
        "6. Users need to know where they are in a flow\n"
        "7. If you feel confused or uncertain, that confusion is the finding\n\n"
        "Return a JSON object matching this schema exactly:\n"
        f"{schema}\n\n"
        "Return ONLY valid JSON. No markdown fences, no explanation, no preamble."
    )
```

Schema is generated from `ScreenshotDecision.model_json_schema()` — stays in sync with the model automatically.

### `run_screenshot_agent`

Public entry point.

```python
def run_screenshot_agent(
    target: str,
    task: str,
    user_type: UserType,
    provider: str,
    model_id: str,
    api_key: str,
) -> tuple[ScreenshotDecision, int, int]:
```

Returns `(decision, input_tokens, output_tokens)`.

```python
image_data, media_type = _load_image(target)
system = _build_system_prompt(user_type)
user_prompt = f"Task: {task}"
raw, in_tok, out_tok = _call_llm(provider, model_id, api_key, system, image_data, media_type, user_prompt)
decision = ScreenshotDecision.model_validate_json(raw)
return decision, in_tok, out_tok
```

`model_validate_json` raises `ValidationError` on bad output. Let it propagate — `run()` handles display based on `--verbose`.

### `_call_llm` (internal routing)

```python
def _call_llm(
    provider: str,
    model_id: str,
    api_key: str,
    system: str,
    image_data: str,
    media_type: str,
    user_prompt: str,
) -> tuple[str, int, int]:
```

```python
if provider == "anthropic":
    return _call_anthropic(model_id, api_key, system, image_data, media_type, user_prompt)
if provider in ("openai", "gemini"):
    endpoint = _OPENAI_COMPAT_ENDPOINTS[provider]
    return _call_openai_compat(endpoint, model_id, api_key, system, image_data, media_type, user_prompt)
raise click.ClickException(f"Unknown provider: {provider!r} — run `swarm config` to reconfigure")
```

---

## Step 2 — Provider calls

Use `urllib` throughout, consistent with `config.py`. Both call functions return `(response_text, input_tokens, output_tokens)`.

### `_call_anthropic`

```http
POST https://api.anthropic.com/v1/messages
x-api-key: {api_key}
anthropic-version: 2023-06-01
content-type: application/json

{
  "model": "{model_id}",
  "max_tokens": 1024,
  "system": "{system}",
  "messages": [{
    "role": "user",
    "content": [
      {
        "type": "image",
        "source": {
          "type": "base64",
          "media_type": "{media_type}",
          "data": "{image_data}"
        }
      },
      {"type": "text", "text": "{user_prompt}"}
    ]
  }]
}
```

Extract:

- `text = data["content"][0]["text"]`
- `input_tokens = data["usage"]["input_tokens"]`
- `output_tokens = data["usage"]["output_tokens"]`

Anthropic enforces JSON output through the system prompt instruction, not a `response_format` field.

### `_call_openai_compat`

OpenAI and Gemini share the same request format.

```python
_OPENAI_COMPAT_ENDPOINTS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}
```

Provider scope for v1: Anthropic, OpenAI, Gemini. DeepSeek's API models are text-only — add back when they ship vision or when browser mode (text-only) is built.

```http
POST {endpoint}
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "model": "{model_id}",
  "messages": [
    {"role": "system", "content": "{system}"},
    {
      "role": "user",
      "content": [
        {
          "type": "image_url",
          "image_url": {"url": "data:{media_type};base64,{image_data}"}
        },
        {"type": "text", "text": "{user_prompt}"}
      ]
    }
  ],
  "response_format": {"type": "json_object"},
  "max_tokens": 1024
}
```

Extract:

- `text = data["choices"][0]["message"]["content"]`
- `input_tokens = data["usage"]["prompt_tokens"]`
- `output_tokens = data["usage"]["completion_tokens"]`

**On `max_tokens`:** This caps the output of a single call — it has nothing to do with how many agents run in parallel. A complete `ScreenshotDecision` JSON runs roughly 300–700 tokens depending on how verbose `reasoning` and `friction_observed` are. 1024 is a safe ceiling for now. After the first real run, check the actual output token counts in the terminal — if they're consistently approaching 1024, raise it. If they're consistently under 500, lower it to reduce the chance of runaway verbose responses.

---

## Step 3 — Fill in `run()` in `main.py`

### 1. Load and validate config

```python
config = load_config()
if not config.get("model") or not config.get("api_key"):
    raise click.ClickException("Not configured — run `swarm config` first")
model_string = config["model"]              # "anthropic/claude-sonnet-4-20250514"
provider, model_id = model_string.split("/", 1)
api_key = config["api_key"]
```

### 2. Reject URL targets

```python
if target.startswith(("http://", "https://")):
    raise click.ClickException(
        "URL targets require browser mode — not yet implemented.\n"
        "Pass an image path instead: swarm screenshot.png \"<task>\""
    )
```

### 3. Default persona

Grounded in Steve Krug's research on how real users actually behave — scanning not reading, satisficing not optimizing, muddling through rather than understanding.

```python
user_type = UserType(
    label="Default User",
    weight=1.0,
    description=(
        "In a hurry and doesn't read pages — scans them quickly, looking for words or links that match the task. "
        "Doesn't weigh options or look for the best choice; clicks the first thing that looks reasonable enough to work (satisficing). "
        "Doesn't try to understand how the site is structured or how things work — muddles through, and if something seems to work, sticks with it without figuring out why. "
        "Has low tolerance for friction: any moment that requires stopping to think, read instructions, or decode an interface increases the chance of giving up and abandoning the task."
    ),
)
```

### 4. Call the agent

Task and filename are visible during the call via the status spinner.

```python
with _console.status(f"Analyzing [dim]{Path(target).name}[/] — {task}"):
    try:
        decision, in_tok, out_tok = run_screenshot_agent(
            target=target,
            task=task,
            user_type=user_type,
            provider=provider,
            model_id=model_id,
            api_key=api_key,
        )
    except Exception as exc:
        if verbose:
            raise
        raise click.ClickException(str(exc)) from exc
```

### 5. Map to `AgentResult`

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
    input_tokens=in_tok,
    output_tokens=out_tok,
    cost=0.0,
)
```

### 6. Save result

```python
ensure_swarm_structure()
timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
report_path = LOCAL_DIR / "reports" / f"{timestamp}_screenshot.json"
report_path.write_text(result.model_dump_json(indent=2) + "\n")
```

### 7. Print result

```python
_print_result(target, task, model_id, decision, in_tok, out_tok)
```

---

## Step 4 — Output (`_print_result` in `main.py`)

Private function, not exported. Output to stdout — no live display needed for a single agent.

```python
def _print_result(
    target: str,
    task: str,
    model_id: str,
    decision: ScreenshotDecision,
    in_tok: int,
    out_tok: int,
) -> None:
```

**What gets printed:**

```text
─────────────────────────────────────────────

  screenshot.png — "find and click the sign up button"

  "I would click the large blue Get Started button in the
   hero section — it's the most prominent action on the page."

  Target     Blue "Get Started" button, hero section
  Reason     Strong contrast and central placement make it
             the obvious primary action.

  Friction
  • No sign-up link in the top nav — discoverability depends
    entirely on scrolling to the hero
  • "Get Started" does not specify what you're starting

  Completed   No   ·   Abandoned   No

  claude-sonnet-4-20250514  ·  842 in / 312 out tokens

─────────────────────────────────────────────
```

Implementation notes:

- Divider: `_console.rule(style="dim")`
- Header: filename + task — both always shown so runs are identifiable
- `comment` is the most prominent text — the first thing you read
- `friction_observed` section is omitted entirely when the list is empty
- `Completed` / `Abandoned` are dimmed when false, normal weight when true
- Token line is dimmed — secondary info
- Cost line omitted until real pricing is calculated

Rich calls:

- `_console.rule(style="dim")` for dividers
- `_console.print(f"[bold]{comment}[/]")` for the comment
- `_console.print(f"  [dim]Target[/]   {decision.target_element}")` for labeled fields
- `_console.print(f"  [dim]•[/] {point}")` for each friction point
- `_console.print(f"  [dim]{model_id}  ·  {in_tok} in / {out_tok} out tokens[/]")` for the footer

---

## Deferred

- **URL targets** — need Playwright for screenshot capture; browser mode
- **Persona sampling** — `users.json` and distribution come with multi-agent support
- **Cost calculation** — requires per-model pricing table; `cost = 0.0` until then
- **Rate limit retry** — add when concurrency is introduced
- **`--users` flag** — wired up in the CLI, ignored in this mode
- **DeepSeek** — API models are text-only; add back when they ship vision or when browser mode is built
