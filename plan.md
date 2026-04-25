# Config Wizard — Implementation Plan

## Current State

| File                       | What it has                     |
| -------------------------- | ------------------------------- |
| `src/ux_swarm/cli.py`      | `SmartGroup` only               |
| `src/ux_swarm/main.py`     | `cli` group + `run` stub        |
| `src/ux_swarm/models.py`   | All Pydantic models             |
| `src/ux_swarm/__init__.py` | Empty                           |
| `pyproject.toml`           | `click`, `pydantic`, `requests` |

---

## What to Build

### New files

| File                     | Purpose                                                            |
| ------------------------ | ------------------------------------------------------------------ |
| `src/ux_swarm/menu.py`   | `select()`, `navigate()` — arrow-key menus                         |
| `src/ux_swarm/config.py` | Config paths, providers, storage, API validation, Playwright check |

### Modified files

| File                   | Changes                                                        |
| ---------------------- | -------------------------------------------------------------- |
| `src/ux_swarm/main.py` | Add `config` command, first-run check in `cli()`, wizard logic |
| `pyproject.toml`       | Add `rich`, `playwright` dependencies                          |

---

## 1. `menu.py` — Arrow-key Menu System

Port the beta's `select()` + `navigate()` with three fixes.

### Fix 1 — Back navigation via `GoBack` exception

```python
class GoBack(Exception):
    """Raised by select() when the user presses Escape to go back."""
```

In `select()`, `click.getchar()` returns `\x1b` alone for Escape and `\x1b[A` / `\x1b[B` for arrows. Handle Escape:

```python
if key == "\x1b":
    sys.stdout.write(f"\x1b[{lines_drawn}A\x1b[J")
    sys.stdout.flush()
    console.print(f"[bold]{label}:[/] [dim]← back[/]\n")
    raise GoBack
```

### Fix 2 — Ctrl-C handling

```python
if key == "\x03":
    sys.stdout.write(f"\x1b[{lines_drawn}A\x1b[J")
    sys.stdout.flush()
    console.print("\n[dim]interrupted[/]")
    raise SystemExit(130)
```

### Fix 3 — Raw stdout for cursor moves only

`sys.stdout.write` handles the ANSI cursor-up/erase move. All content goes through `console.print`.

### `navigate()` — unchanged

---

## 2. `config.py`

Sections within the file, in order:

### Config paths

```python
LOCAL_DIR    = Path(".swarm")
LOCAL_CONFIG = LOCAL_DIR / "config.json"
GLOBAL_CONFIG = Path.home() / ".config" / "ux-swarm" / "config.json"
```

### Providers

```python
PROVIDERS: list[dict[str, str]] = [
    {"name": "OpenAI",        "key": "openai",    "env": "OPENAI_API_KEY"},
    {"name": "Anthropic",     "key": "anthropic", "env": "ANTHROPIC_API_KEY"},
    {"name": "Google Gemini", "key": "gemini",    "env": "GEMINI_API_KEY"},
    {"name": "DeepSeek",      "key": "deepseek",  "env": "DEEPSEEK_API_KEY"},
]
```

### Run defaults

The wizard writes `provider`, `api_key`, `model` to disk. These run parameters are separate — never written by the wizard, merged in at load time.

```python
RUN_DEFAULTS: dict[str, int | float] = {
    "default_users": 20,
    "max_steps": 3,
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 20,
}
```

### `ProviderAuthError`

```python
class ProviderAuthError(Exception):
    """Raised when a provider rejects an API key (HTTP 401/403)."""
```

### `provider_env_var(provider_key)`

Maps a provider key to its env var name.

### `fetch_provider_models(provider_key, api_key)`

Live HTTP fetch of the provider's model list — same four-branch logic as the beta. No fallback. Non-auth failures propagate; the wizard caller handles them.

**LiteLLM model ID conventions:**

- OpenAI: no prefix — `gpt-4o`, `gpt-4o-mini`, `o3`, `o4-mini`
- Anthropic: `anthropic/` prefix — `anthropic/claude-sonnet-4-20250514`
- Gemini: `gemini/` prefix — `gemini/gemini-2.5-flash`
- DeepSeek: `deepseek/` prefix — `deepseek/deepseek-chat`

The Anthropic branch returns raw IDs (`claude-sonnet-4-20250514`). Prefix them:

```python
return [f"anthropic/{m['id']}" for m in data.get("data", [])]
```

OpenAI IDs need no prefix. Keep the beta's filter (strip fine-tunes, keep `gpt-` and `o[digit]` prefixed).

### `check_playwright_browsers()`

Returns `bool`. Unchanged from beta.

### `load_config()`

Merges `RUN_DEFAULTS` → global config → local config. `provider`, `api_key`, `model` come from the file only.

```python
def load_config() -> dict:
    resolved = dict(RUN_DEFAULTS)
    for path in (GLOBAL_CONFIG, LOCAL_CONFIG):
        if path.exists():
            try:
                resolved.update(json.loads(path.read_text()))
            except json.JSONDecodeError as exc:
                raise click.ClickException(
                    f"Config file is not valid JSON: {path}\n{exc}\nFix or delete it and run again."
                ) from exc
    return resolved
```

### `save_config(data, *, local=True)`

Writes to `LOCAL_CONFIG` by default. Creates parent dirs. Returns path written.

### `ensure_swarm_structure()`

Creates `.swarm/reports/`.

---

## 3. The Wizard — `_run_config_wizard()` in `main.py`

### Step-based loop

```python
def _run_config_wizard() -> None:
    state: dict = {}
    steps = [
        _wizard_step_provider,
        _wizard_step_api_key,
        _wizard_step_model,
        _wizard_step_playwright,
        _wizard_step_confirm,
    ]
    i = 0
    while i < len(steps):
        try:
            steps[i](state)
            i += 1
        except GoBack:
            i = max(0, i - 1)
```

Going back re-runs the previous step with the prior selection restored as `default_index`.

### Step 0 — Provider selection

```python
def _wizard_step_provider(state: dict) -> None:
    names = [p["name"] for p in PROVIDERS]
    default = next(
        (i for i, p in enumerate(PROVIDERS) if p["key"] == state.get("provider_key")),
        0,
    )
    chosen_name = select("LLM Provider", names, default_index=default)
    provider = next(p for p in PROVIDERS if p["name"] == chosen_name)
    state["provider_key"] = provider["key"]
    state["provider_env"] = provider["env"]
    state["provider_name"] = provider["name"]
```

### Step 1 — API key entry with env-var skip

```python
def _wizard_step_api_key(state: dict) -> None:
    env_var = state["provider_env"]
    env_val = os.environ.get(env_var, "")

    if env_val:
        console.print(f"[dim]Press Enter to use ${env_var} from environment[/]")

    while True:
        raw = click.prompt(f"API Key ({env_var})", default="", show_default=False).strip()
        api_key = raw or env_val

        if not api_key:
            console.print("[red]No API key found. Enter a key or set the env var.[/]")
            continue

        try:
            model_options = fetch_provider_models(state["provider_key"], api_key)
            state["api_key"] = api_key
            state["api_key_source"] = "env" if (not raw and env_val) else "entered"
            state["model_options"] = model_options
            return
        except ProviderAuthError:
            console.print("[red]Invalid API key, please try again.[/]")
        except Exception as exc:
            console.print(f"[red]Could not reach {state['provider_name']} API.[/]")
            console.print(f"[dim]{exc}[/]")
            console.print("[dim]Check your network and try again.[/]")
            raise SystemExit(1)
```

### Step 2 — Model selection

```python
def _wizard_step_model(state: dict) -> None:
    options = state["model_options"]
    default = next(
        (i for i, m in enumerate(options) if m == state.get("model")),
        0,
    )
    state["model"] = select("Model", options, default_index=default)
```

### Step 3 — Playwright check and install

```python
def _wizard_step_playwright(state: dict) -> None:
    console.print("\n[bold]Playwright[/]")
    console.print("─" * 40)

    if check_playwright_browsers():
        console.print("  [green]•[/] Chromium installed")
        state["playwright_ok"] = True
        return

    console.print("  [yellow]•[/] Chromium not found — required for browser mode\n")
    choice = select("Install Chromium now?", ["Yes", "No, skip for now"])

    if choice.startswith("No"):
        state["playwright_ok"] = False
        return

    _install_chromium()
    state["playwright_ok"] = True
```

```python
def _install_chromium() -> None:
    import subprocess
    with console.status("Installing Chromium…"):
        result = subprocess.run(
            ["playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        console.print("[red]Chromium install failed.[/]")
        if result.stderr.strip():
            console.print(f"[dim]{result.stderr.strip()}[/]")
        console.print("[dim]Run: playwright install chromium[/]")
    else:
        console.print("[green]✓[/] Chromium installed")
```

### Step 4 — Confirmation

```python
def _wizard_step_confirm(state: dict) -> None:
    from rich.table import Table

    key = state["api_key"]
    masked = key[:8] + "…" + key[-4:] if len(key) > 12 else "•" * len(key)
    source_note = " [dim](from environment)[/]" if state.get("api_key_source") == "env" else ""
    playwright_status = "[green]✓ installed[/]" if state.get("playwright_ok") else "[yellow]not installed[/]"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Provider", state["provider_name"])
    table.add_row("Model", state["model"])
    table.add_row("API Key", f"{masked}{source_note}")
    table.add_row("Chromium", playwright_status)

    console.print()
    console.print(table)
    console.print()

    choice = select("Save config?", ["Yes, save", "Go back"])
    if choice == "Go back":
        raise GoBack
```

### Write to disk

```python
    saved_path = save_config({
        "provider": state["provider_key"],
        "api_key": state["api_key"],
        "model": state["model"],
    })
    console.print(f"\n[green]Config saved →[/] {saved_path}\n")
```

---

## 4. `main.py` — Wiring

### First-run check

```python
@click.group(cls=SmartGroup, invoke_without_command=True, help=__description__)
@click.version_option(...)
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        _print_home()
        if not (LOCAL_CONFIG.exists() or GLOBAL_CONFIG.exists()):
            if select("Run setup?", ["Yes", "No"]) == "Yes":
                _run_config_wizard()
```

### `config` command

```python
@cli.command()
def config():
    """Run the setup wizard: provider, API key, model, Chromium."""
    _run_config_wizard()
```

---

## 5. `pyproject.toml` — Dependencies

```toml
dependencies = [
    "click>=8.1.8",
    "pydantic>=2.13.3",
    "requests>=2.32.5",
    "rich>=13.0",
    "playwright>=1.40",
]
```

`litellm` added when `run` is implemented.

---

## 6. Todo

### Phase 1 — Dependencies

- [x] Add `rich>=13.0` to `pyproject.toml` dependencies
- [x] Add `playwright>=1.40` to `pyproject.toml` dependencies
- [x] Run `uv sync`

### Phase 2 — `menu.py`

- [x] Create `src/ux_swarm/menu.py`
- [x] Define `GoBack` exception class
- [x] Port `navigate()` from beta — up/down arrow handling, clamped index
- [x] Port `select()` from beta — in-place redraw loop, collapse on Enter
- [x] Add `default_index` parameter to `select()`
- [x] Add Escape (`\x1b`) → erase menu, print `← back`, raise `GoBack`
- [x] Add Ctrl-C (`\x03`) → erase menu, print `interrupted`, `SystemExit(130)`

### Phase 3 — `config.py`

- [ ] Create `src/ux_swarm/config.py`
- [ ] Add `LOCAL_DIR`, `LOCAL_CONFIG`, `GLOBAL_CONFIG` path constants
- [ ] Define `PROVIDERS` list — four entries, three keys each (`name`, `key`, `env`)
- [ ] Define `RUN_DEFAULTS` dict — five run parameters
- [ ] Define `ProviderAuthError` exception
- [ ] Implement `provider_env_var()` — key → env var name, fallback to `OPENAI_API_KEY`
- [ ] Implement `fetch_provider_models()` — OpenAI branch (filter fine-tunes and non-chat models)
- [ ] Implement `fetch_provider_models()` — Anthropic branch (prefix each ID with `anthropic/`)
- [ ] Implement `fetch_provider_models()` — Gemini branch (filter to `gemini-` IDs, prefix with `gemini/`)
- [ ] Implement `fetch_provider_models()` — DeepSeek branch (prefix each ID with `deepseek/`)
- [ ] Wire auth failure (HTTP 401/403) → raise `ProviderAuthError` in all four branches
- [ ] Wire non-auth failures to propagate (no bare `except` swallowing them)
- [ ] Implement `check_playwright_browsers()` — import `sync_playwright`, check executable path exists
- [ ] Implement `load_config()` — merge `RUN_DEFAULTS` → global file → local file
- [ ] Add `json.JSONDecodeError` → `click.ClickException` in `load_config()`
- [ ] Implement `save_config()` — write to `LOCAL_CONFIG`, `mkdir(parents=True, exist_ok=True)`
- [ ] Implement `ensure_swarm_structure()` — create `.swarm/reports/`

### Phase 4 — Wizard steps in `main.py`

- [ ] Add imports: `GoBack`, `select` from `menu`; all needed names from `config`
- [ ] Implement `_wizard_step_provider()` — `select()` with restored `default_index` from `state`
- [ ] Implement `_wizard_step_api_key()` — detect env var, show hint if set, `click.prompt` with `default=""`
- [ ] Implement `_wizard_step_api_key()` — empty input + no env var → re-prompt with error
- [ ] Implement `_wizard_step_api_key()` — call `fetch_provider_models()`, loop on `ProviderAuthError`
- [ ] Implement `_wizard_step_api_key()` — non-auth exception → print error, `SystemExit(1)`
- [ ] Implement `_wizard_step_model()` — `select()` with restored `default_index` from `state`
- [ ] Implement `_wizard_step_playwright()` — call `check_playwright_browsers()`, branch on result
- [ ] Implement `_install_chromium()` — `subprocess.run` with `capture_output=True`, no `check=True`
- [ ] Implement `_install_chromium()` — non-zero returncode → print error + `result.stderr`, no raise
- [ ] Implement `_wizard_step_confirm()` — Rich table with provider, model, masked key, Chromium status
- [ ] Implement `_wizard_step_confirm()` — "Go back" → raise `GoBack`
- [ ] Implement `_run_config_wizard()` — step list + `while` loop catching `GoBack`
- [ ] After step loop — call `save_config()` with `provider`, `api_key`, `model`

### Phase 5 — Wiring in `main.py`

- [ ] Import `LOCAL_CONFIG`, `GLOBAL_CONFIG` from `config`
- [ ] Add first-run check to `cli()` — existence check on both config paths, `select("Run setup?")` prompt
- [ ] Add `config` command — single call to `_run_config_wizard()`

### Phase 6 — Smoke Test

- [ ] `swarm` with no config → "Run setup?" appears
- [ ] "No" at "Run setup?" → exits cleanly
- [ ] `swarm config` → wizard starts directly
- [ ] Provider: arrow-key navigation works, Enter commits
- [ ] API key: Enter with env var set → uses env var
- [ ] API key: bad key → red error, re-prompts
- [ ] API key: network failure → prints error, exits 1
- [ ] Model: Escape goes back to API key step; previously chosen provider is default
- [ ] Playwright already installed → confirmation shown, no install prompt
- [ ] Playwright missing + "Yes" → install runs; failure → stderr shown, not a traceback
- [ ] Playwright missing + "No, skip" → wizard continues to confirmation
- [ ] Confirmation: values shown correctly, key is masked
- [ ] "Go back" at confirmation → returns to Playwright step
- [ ] "Yes, save" → `.swarm/config.json` written with `provider`, `api_key`, `model`
- [ ] `swarm` after config exists → no wizard prompt
- [ ] Ctrl-C in any arrow-key menu → "interrupted", exit 130
