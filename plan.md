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

### `ProviderAuthError`

```python
class ProviderAuthError(Exception):
    """Raised when a provider rejects an API key (HTTP 401/403)."""
```

### `provider_env_var(provider_key)`

Maps a provider key to its env var name. No fallback — unknown key raises `StopIteration`.

### `fetch_provider_models(provider_key, api_key)`

Live HTTP fetch of the provider's model list. No fallback. Non-auth failures propagate; the wizard caller handles them.

**LiteLLM model ID conventions (validated against provider docs):**

- OpenAI: `openai/` prefix — `openai/gpt-4o`, `openai/o3`. Filter: strip fine-tunes (`:`), keep `gpt-`, `chatgpt-`, `o[digit]`
- Anthropic: `anthropic/` prefix — `anthropic/claude-sonnet-4-20250514`. Fetch with `?limit=1000`
- Gemini: `gemini/` prefix — `gemini/gemini-2.5-flash`. Filter: `startswith("gemini-")` and `"embedding" not in id`
- DeepSeek: `deepseek/` prefix — `deepseek/deepseek-chat`

### `check_chromium_installed()`

Returns `bool`. Unchanged from beta.

### `load_config()`

Merges global config → local config into an empty dict. `provider`, `api_key`, `model` come from the file only. `json.JSONDecodeError` → `click.ClickException`.

### `save_config(data, *, local=True)`

Writes to `LOCAL_CONFIG` by default. Creates parent dirs. Returns path written.

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

    if check_chromium_installed():
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

- [x] Create `src/ux_swarm/config.py`
- [x] Add `LOCAL_DIR`, `LOCAL_CONFIG`, `GLOBAL_CONFIG` path constants
- [x] Define `PROVIDERS` list — four entries, three keys each (`name`, `key`, `env`)
- [x] Define `ProviderAuthError` exception
- [x] Implement `provider_env_var()` — key → env var name, no fallback
- [x] Implement `fetch_provider_models()` — OpenAI branch (prefix `openai/`, filter fine-tunes and non-chat models, add `chatgpt-`)
- [x] Implement `fetch_provider_models()` — Anthropic branch (prefix `anthropic/`, `?limit=1000`)
- [x] Implement `fetch_provider_models()` — Gemini branch (prefix `gemini/`, filter embeddings)
- [x] Implement `fetch_provider_models()` — DeepSeek branch (prefix `deepseek/`)
- [x] Wire auth failure (HTTP 401/403) → raise `ProviderAuthError` in all four branches
- [x] Wire non-auth failures to propagate (no bare `except` swallowing them)
- [x] Implement `check_chromium_installed()` — import `sync_playwright`, check executable path exists
- [x] Implement `load_config()` — merge global file → local file, start from empty dict
- [x] Add `json.JSONDecodeError` → `click.ClickException` in `load_config()`
- [x] Implement `save_config()` — write to `LOCAL_CONFIG`, `mkdir(parents=True, exist_ok=True)`
- [x] `RUN_DEFAULTS` and `ensure_swarm_structure()` moved to `main.py` (not config wizard concerns)

### Phase 4 — Wizard steps in `main.py`

- [ ] Add imports: `GoBack`, `select` from `menu`; all needed names from `config`
- [ ] Implement `_wizard_step_provider()` — `select()` with restored `default_index` from `state`
- [ ] Implement `_wizard_step_api_key()` — detect env var, show hint if set, `click.prompt` with `default=""`
- [ ] Implement `_wizard_step_api_key()` — empty input + no env var → re-prompt with error
- [ ] Implement `_wizard_step_api_key()` — call `fetch_provider_models()`, loop on `ProviderAuthError`
- [ ] Implement `_wizard_step_api_key()` — non-auth exception → print error, `SystemExit(1)`
- [ ] Implement `_wizard_step_model()` — `select()` with restored `default_index` from `state`
- [ ] Implement `_wizard_step_playwright()` — call `check_chromium_installed()`, branch on result
- [ ] Implement `_install_chromium()` — `subprocess.run` with `capture_output=True`, no `check=True`
- [ ] Implement `_install_chromium()` — non-zero returncode → print error + `result.stderr`, no raise
- [ ] Implement `_wizard_step_confirm()` — Rich table with provider, model, masked key, Chromium status
- [ ] Implement `_wizard_step_confirm()` — "Go back" → raise `GoBack`
- [ ] Implement `_run_config_wizard()` — step list + `while` loop catching `GoBack`
- [ ] After step loop — call `save_config()` with `provider`, `api_key`, `model`

### Phase 5 — Wiring in `main.py`

- [x] Import `LOCAL_CONFIG`, `GLOBAL_CONFIG` from `config`
- [x] Add first-run check to `cli()` — existence check on both config paths, `select("Run setup?")` prompt
- [x] Add `config` command — single call to `run_config_wizard()`

### Phase 6 — Smoke Test

- [x] `swarm` with no config → "Run setup?" appears
- [x] "No" at "Run setup?" → exits cleanly
- [x] `swarm config` → wizard starts directly
- [x] Provider: arrow-key navigation works, Enter commits
- [x] API key: Enter with env var set → uses env var
- [x] API key: bad key → red error, re-prompts
- [x] API key: network failure → prints error, exits 1
- [x] Model: Escape goes back to API key step; previously chosen provider is default
- [x] Playwright already installed → confirmation shown, no install prompt
- [ ] Playwright missing + "Yes" → install runs; failure → stderr shown, not a traceback
- [ ] Playwright missing + "No, skip" → wizard continues to confirmation
- [x] Confirmation: values shown correctly, key is masked
- [ ] "Go back" at confirmation → returns to Playwright step
- [x] "Yes, save" → `.swarm/config.json` written with `provider`, `api_key`, `model`
- [x] `swarm` after config exists → no wizard prompt
- [x] Ctrl-C in any arrow-key menu → "interrupted", exit 130


### Phase 7 - UI Changes

- On run setup, i see the whole usage stuff:

```bash
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm
Usage: swarm [OPTIONS] COMMAND [ARGS]...

  Synthetic UX testing

Options:
  --version  Show the version and exit.
  --help     Show this message and exit.

Commands:
  config  Run the setup wizard: provider, API key,...
Run setup?

  › Yes
    No
```


I shoudlnt see that, I shoudl jsut see soemthing like this:

```bash
(ux-swarm) masonomara@Masons-MacBook-Pro ux-swarm % swarm

Need to run config wizard.
Ok to proceed? (y)
```

- take out the environment variable option for now, dont need it
- in save config, when chromium is isntalled, it shoudl take you to the api key step

---

## Phase 8 — First-Run Home Screen

### Goal

Replace Click's default help output with a designed home screen. Running bare `swarm` — whether or not config exists — shows a single, intentional screen. The setup wizard prompt appears only when no config is found.

### Target Output (no config)

```
  __  ___  __    _____      _____   ___  __  ___
 / / / / |/_/___/ __/ | /| / / _ | / _ \/  |/  /
/ /_/ />  </___/\ \ | |/ |/ / __ |/ , _/ /|_/ /
\____/_/|_|   /___/ |__/|__/_/ |_/_/|_/_/  /_/

ux-swarm v0.1.0 — genesis

Execute automated user testing by releasing 100 AI bots at a user
interface or live website to observe how they execute tasks.

• Playwright:    Not installed
• LLM Provider:  Not configured
• Model:         Not configured

────────────────────────────────────────────────

Usage:

  swarm <target> <task>

Commands:

  config    Run the setup wizard: provider, API key, model, Chromium
  --help    View all commands

────────────────────────────────────────────────

Begin Setup Wizard?

  › Yes
    No
```

### Target Output (config exists)

Same screen, but status reflects saved config and no wizard prompt at the bottom:

```
• Playwright:    Enabled
• LLM Provider:  Anthropic
• Model:         claude-opus-4-7
```

---

### Implementation

#### 1. Add `_print_home()` to `main.py`

This function owns the entire home screen. It reads config and Playwright state, then renders everything with Rich.

```python
TAGLINES: dict[str, str] = {
    "0.1.0": "genesis",
}

def _print_home() -> None:
    from rich.console import Console
    from ux_swarm.config import PROVIDERS, check_chromium_installed, load_config

    console = Console()

    # ASCII art — printed as plain text to preserve exact spacing
    console.print(
        "\n"
        "  __  ___  __    _____      _____   ___  __  ___\n"
        " / / / / |/_/___/ __/ | /| / / _ | / _ \\/  |/  /\n"
        "/ /_/ />  </___/\\ \\ | |/ |/ / __ |/ , _/ /|_/ /\n"
        "\\____/_/|_|   /___/ |__/|__/_/ |_/_/|_/_/  /_/",
        highlight=False,
    )

    # Version line with optional tagline
    tagline = TAGLINES.get(__version__)
    version_line = f"v{__version__} — {tagline}" if tagline else f"v{__version__}"
    console.print(f"\n[bold]ux-swarm[/] {version_line}\n")

    # Description
    console.print(
        "Execute automated user testing by releasing 100 AI bots at a user\n"
        "interface or live website to observe how they execute tasks.\n"
    )

    # Status — read live from config + Playwright check
    config = load_config()

    try:
        playwright_ok = check_chromium_installed()
    except Exception:
        playwright_ok = False
    playwright_label = "[green]Enabled[/]" if playwright_ok else "[yellow]Not installed[/]"

    provider_key = config.get("provider")
    provider_name = (
        next((p["name"] for p in PROVIDERS if p["key"] == provider_key), provider_key)
        if provider_key else "[dim]Not configured[/]"
    )

    raw_model = config.get("model", "")
    model_display = raw_model.split("/", 1)[-1] if "/" in raw_model else raw_model
    model_label = model_display if model_display else "[dim]Not configured[/]"

    console.print(f"• Playwright:    {playwright_label}")
    console.print(f"• LLM Provider:  {provider_name}")
    console.print(f"• Model:         {model_label}\n")

    # Divider + usage
    console.print("[dim]" + "─" * 48 + "[/]\n")
    console.print("[bold]Usage:[/]\n")
    console.print("  [bold]swarm[/] [dim]<target> <task>[/]\n")
    console.print("[bold]Commands:[/]\n")
    console.print("  [bold]config[/]    Run the setup wizard: provider, API key, model, Chromium")
    console.print("  [bold]--help[/]    View all commands")
    console.print("\n[dim]" + "─" * 48 + "[/]\n")
```

Key decisions:
- ASCII art uses `highlight=False` so Rich doesn't misinterpret the slashes and brackets as markup.
- `TAGLINES` dict maps version string → tagline. Add an entry per release; missing versions get no tagline.
- `check_chromium_installed()` is wrapped in `try/except` because it imports `playwright.sync_api` — if playwright isn't installed as a package at all, it raises `ImportError`. The screen should degrade gracefully rather than crash.
- Model display strips the provider prefix (`anthropic/claude-opus-4-7` → `claude-opus-4-7`) since the provider line already makes the prefix redundant.

---

#### 2. Update `cli()` in `main.py`

Replace the current `if/else` block that splits on config existence with `_print_home()` called unconditionally, followed by the setup wizard prompt only when no config exists.

**Current code:**

```python
def cli(ctx):
    if ctx.invoked_subcommand is None:
        if not (LOCAL_CONFIG.exists() or GLOBAL_CONFIG.exists()):
            if select("No config found — run setup?", ["Yes", "No"]) == "Yes":
                run_config_wizard()
        else:
            click.echo(ctx.get_help())
```

**New code:**

```python
def cli(ctx):
    if ctx.invoked_subcommand is None:
        _print_home()
        if not (LOCAL_CONFIG.exists() or GLOBAL_CONFIG.exists()):
            if select("Begin Setup Wizard?", ["Yes", "No"]) == "Yes":
                run_config_wizard()
```

The `ctx.get_help()` call is removed entirely — `_print_home()` replaces it. This also fixes the Phase 7 bug where Click's usage block was printing before the wizard prompt: that was caused by `ctx.get_help()` running in the wrong branch. Now nothing from Click prints automatically.

---

#### 3. No changes to `config.py` or `menu.py`

`_print_home()` calls `load_config()`, `check_chromium_installed()`, and `PROVIDERS` from `config.py`, all of which already exist. `select()` from `menu.py` is already imported in `main.py`. No new dependencies needed.

---

### Todo

#### Phase 8 — First-Run Home Screen

- [ ] Add `TAGLINES` dict to `main.py` with `"0.1.0": "genesis"`
- [ ] Implement `_print_home()` in `main.py`
  - [ ] ASCII art block with `highlight=False`
  - [ ] Version line with tagline lookup
  - [ ] Description paragraph
  - [ ] `load_config()` call to read current provider/model
  - [ ] `check_chromium_installed()` with `try/except` for missing playwright package
  - [ ] Status bullets: Playwright, LLM Provider, Model
  - [ ] Strip provider prefix from model string for display
  - [ ] Divider + Usage section
  - [ ] Divider before returning
- [ ] Update `cli()` — call `_print_home()` unconditionally, remove `ctx.get_help()`
- [ ] Change setup wizard prompt text to `"Begin Setup Wizard?"`

#### Phase 8 — Smoke Tests

- [ ] `swarm` with no config → home screen renders, "Begin Setup Wizard?" appears below
- [ ] `swarm` with config → home screen renders, no wizard prompt, correct provider/model shown
- [ ] Playwright installed → `• Playwright: Enabled` in green
- [ ] Playwright not installed → `• Playwright: Not installed` in yellow
- [ ] Model stored as `anthropic/claude-opus-4-7` → displays as `claude-opus-4-7`
- [ ] No config at all → all three status lines show "Not configured"
- [ ] ASCII art renders without Rich interpreting brackets as markup