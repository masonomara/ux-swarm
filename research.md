# Config Wizard — Research Notes

Everything there is to know about how the `swarm config` wizard works: its triggers,
UI mechanics, data flow, validation loops, persistence model, and all the edge cases
baked into each layer.

---

## 1. The Two Entry Points

The wizard is one function (`_run_config_wizard`) reachable from two places.

### 1a. First-run auto-prompt (`main()` — cli.py:145–151)

```python
@click.group(invoke_without_command=True, ...)
@click.pass_context
def main(ctx):
    if ctx.invoked_subcommand is None:
        _print_branded_help()
        if not (LOCAL_CONFIG.exists() or GLOBAL_CONFIG.exists()):
            if select("Run setup?", ["Yes", "No"]) == "Yes":
                _run_config_wizard()
```

Running bare `swarm` (no subcommand) does three things in order:
1. Prints the branded home/splash screen.
2. Checks for the presence of **either** a local config (`.swarm/config.json`) **or** a
   global config (`~/.config/uxswarm/config.json`). If neither exists, the user has never
   configured swarm on this machine or in this project.
3. Asks a yes/no prompt via `select()`. Only if the user chooses "Yes" is the wizard
   invoked. Choosing "No" exits silently — no error, no nag.

The guard is a pure file-existence check, not a content check. A corrupt or empty
`config.json` still suppresses the first-run prompt.

### 1b. Explicit invocation (`config` command — cli.py:343–345)

```python
@main.command()
def config():
    """Interactive setup wizard: choose provider, model, API key, and install browsers."""
    _run_config_wizard()
```

`swarm config` is an unconditional entry — no guards, no existence checks. It always
runs the full wizard, overwriting whatever is on disk. There is no `--global` or
`--local` flag; `save_config()` always writes local by default (see section 5).

---

## 2. The Wizard Step-by-Step (`_run_config_wizard` — cli.py:296–341)

```python
def _run_config_wizard():
    provider_display_names = [provider["name"] for provider in PROVIDERS]
    chosen_provider_name = select("LLM Provider", provider_display_names)
    provider_info = next(p for p in PROVIDERS if p["name"] == chosen_provider_name)
    provider_key = provider_info["key"]

    while True:
        api_key = click.prompt(f"API Key ({provider_info['env']})")
        try:
            model_options = fetch_provider_models(provider_key, api_key)
            break
        except ProviderAuthError:
            console.print("[red]Invalid API key, please try again.[/]")

    chosen_model = select("Model", model_options)

    console.print("\n[bold]Playwright[/]")
    console.print("─" * 40)
    if check_playwright_browsers():
        console.print("  [green]•[/] chromium installed")
    else:
        console.print("  [yellow]•[/] Chromium is required to run swarm.\n")
        if select("Install and continue?", ["Yes", "No"]) == "Yes":
            with console.status("Installing chromium…"):
                subprocess.run(["playwright", "install", "chromium"],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            console.print("[green]✓[/] chromium installed")

    saved_path = save_config({
        "provider": provider_key,
        "api_key": api_key,
        "model": chosen_model,
    })
    console.print(f"\n[green]Config saved to {saved_path}[/]\n")
```

The wizard has exactly four phases:

### Phase 1 — Provider selection
An arrow-key menu built from `PROVIDERS` in `types.py`. The menu shows `.name` values
("OpenAI", "Anthropic", "Google Gemini", "DeepSeek"). Internally the wizard works with
`.key` values ("openai", "anthropic", "gemini", "deepseek").

### Phase 2 — API key + live validation (retry loop)
`click.prompt()` reads a key string (input is not hidden — no `hide_input=True`).
The key is immediately sent live to the provider's API via `fetch_provider_models()`.
If the key is rejected (HTTP 401/403), `ProviderAuthError` is raised and the loop
repeats. Any other failure (network timeout, 5xx, unknown provider) is swallowed and
the wizard falls back to the hardcoded `MODELS` list — the user is not warned.

This means:
- Auth errors → infinite retry until the key works.
- Non-auth errors → silent fallback, wizard continues.

### Phase 3 — Model selection
`select()` with the list returned by `fetch_provider_models()`. If the live fetch failed
(non-auth error), the list comes from `MODELS[provider_key]`. If the provider key is
completely unknown, `MODELS.get(provider_key, [])` returns an empty list — `select()`
would be called with an empty options list (a latent edge-case bug).

### Phase 4 — Playwright check and optional install
`check_playwright_browsers()` probes the filesystem for a Chromium executable via
the `playwright` Python API. If found, the wizard prints a green confirmation and
moves on. If not found, it offers a yes/no prompt. "Yes" runs
`playwright install chromium` as a subprocess with all output suppressed
(`subprocess.run(..., check=True, stdout=DEVNULL, stderr=DEVNULL)`).
`check=True` means a non-zero exit code raises `subprocess.CalledProcessError` —
that exception propagates unhandled (no recovery path in the wizard).

Skipping ("No") leaves the wizard to continue and write config. The user can always
run `playwright install chromium` later; `_run_preflight()` will catch the missing
browser at run time.

### Save
`save_config()` receives exactly three keys: `provider`, `api_key`, `model`. The
remaining defaults (`default_agents`, `max_steps`, `viewport_width`, etc.) are NOT
written to disk — they live only in `DEFAULTS` inside `config_store.py` and are
injected at load time.

---

## 3. Arrow-key Menu System (`menu.py:10–52`)

### `navigate(key, selected_index, max_index)`

Maps escape sequences to direction:
- `\x1b[A` / `\x1bOA` → up (VT100 and application cursor-key variants)
- `\x1b[B` / `\x1bOB` → down

Returns a new index clamped to `[0, max_index]`. Any other key is a no-op (same
index returned). There is no wrapping — up at 0 stays at 0, down at max stays at max.

### `select(label, options, default_index=0)`

Full in-place redrawing menu:
1. Renders the full option list to stdout using Rich console.
2. Reads a single character via `click.getchar()` (blocking, raw mode).
3. On Enter (`\r` or `\n`): writes an ANSI escape to move the cursor up `lines_drawn`
   lines and erase to end-of-screen (`\x1b[{n}A\x1b[J`), then prints a collapsed
   one-line summary (`Label: chosen_option`) and returns the string.
4. On any other key: same cursor-up/erase, then redraws with the new index.

The number of lines drawn is recalculated each redraw cycle as `len(options) + 4`
(label + blank line + options + blank line + hint line). The cursor arithmetic depends
on this count being exact — a Rich markup line that wraps to two terminal lines would
desync the redraw. Since option strings in practice are short model names and provider
names, this is not a problem in production.

There is no Ctrl-C handler in `select()`. `click.getchar()` delivers `\x03` as a raw
byte — it is passed to `navigate()`, which ignores it (not an arrow key), returning
the same index. The menu loops indefinitely on Ctrl-C. The `atexit` handler registered
in cli.py (`stty sane`) restores the terminal on process exit regardless.

---

## 4. Provider and Model Constants (`types.py:50–86`)

### `PROVIDERS`

```python
PROVIDERS: list[dict[str, str]] = [
    {"name": "OpenAI",        "key": "openai",    "env": "OPENAI_API_KEY",    "model": "gpt-4o"},
    {"name": "Anthropic",     "key": "anthropic", "env": "ANTHROPIC_API_KEY", "model": "claude-sonnet-4-20250514"},
    {"name": "Google Gemini", "key": "gemini",    "env": "GEMINI_API_KEY",    "model": "gemini/gemini-2.5-flash"},
    {"name": "DeepSeek",      "key": "deepseek",  "env": "DEEPSEEK_API_KEY",  "model": "deepseek/deepseek-chat"},
]
```

Each entry has four fields:
- `name`: display string in the provider menu.
- `key`: internal identifier threaded through config files, env-var lookups, and API dispatch.
- `env`: the environment variable LiteLLM reads for that provider's key.
- `model`: a per-provider default model (not currently used by the wizard — the wizard
  uses `MODELS` for the fallback list and the live API for the real list).

### `MODELS`

```python
MODELS: dict[str, list[str]] = {
    "openai":    ["gpt-4o", "gpt-4o-mini", "o3", "o4-mini"],
    "anthropic": ["claude-sonnet-4-20250514", "claude-opus-4-20250514", "claude-haiku-4-5-20251001"],
    "gemini":    ["gemini/gemini-2.5-flash", "gemini/gemini-2.5-pro"],
    "deepseek":  ["deepseek/deepseek-chat",  "deepseek/deepseek-reasoner"],
}
```

These are the static fallbacks used when a live API fetch fails for any non-auth reason.
The ordering is intentional — the wizard's `select()` will default to `index=0`, so the
first item in each list is the implicit recommended default for that provider.

---

## 5. API Key Validation (`utils.py:50–115`)

`fetch_provider_models(provider_key, api_key)` performs a live HTTP request for each
supported provider. All requests use `urllib.request` (stdlib only, no extra deps).

| Provider  | Endpoint                                                       | Auth header                         | Filter logic                                                |
|-----------|----------------------------------------------------------------|-------------------------------------|-------------------------------------------------------------|
| anthropic | `api.anthropic.com/v1/models`                                  | `x-api-key: <key>` + version header | None — all models returned                                  |
| openai    | `api.openai.com/v1/models`                                     | `Authorization: Bearer <key>`       | Only IDs without `:`, starting with `gpt-` or `o[digit]`   |
| gemini    | `generativelanguage.googleapis.com/v1beta/openai/models`       | `Authorization: Bearer <key>`       | IDs starting with `gemini-`, prefixed with `gemini/`       |
| deepseek  | `api.deepseek.com/models`                                      | `Authorization: Bearer <key>`       | All IDs, prefixed with `deepseek/`                          |

Timeout is 5 seconds for all requests.

**Auth failure path**: `urllib.error.HTTPError` with status `401` or `403` →
raises `ProviderAuthError`. The wizard catches this, prints the red error, and loops.

**All other failures** (network unreachable, timeout, malformed JSON, unexpected
response shape, unsupported `provider_key` reaching the end without hitting any branch)
→ caught by the bare `except Exception: pass` block → returns `fallback` (`MODELS.get(provider_key, [])`).

The OpenAI filter (`":" not in m["id"]`) strips fine-tuned models (IDs like
`ft:gpt-4o:org:custom:abc123`). The `gpt-` / `o[digit]` filter further strips
embedding, Whisper, DALL-E, and other non-chat models.

---

## 6. Config Storage (`config_store.py`)

### Paths (from `types.py`)

```
LOCAL_CONFIG  = .swarm/config.json           # project-local
GLOBAL_CONFIG = ~/.config/uxswarm/config.json  # machine-wide
```

### `DEFAULTS`

```python
DEFAULTS = {
    "provider":                "openai",
    "api_key":                 "",
    "model":                   "gpt-4o",
    "default_agents":          20,
    "max_steps":               3,
    "viewport_width":          1280,
    "max_concurrent_browser":  5,
    "max_concurrent_screenshot": 20,
}
```

The wizard only writes `provider`, `api_key`, and `model`. The other five keys are
never persisted by the wizard — they always come from `DEFAULTS` at load time unless
the user manually edits the JSON.

### `load_config()`

Applies a three-layer merge:
1. Start with `DEFAULTS`.
2. Apply global config (if it exists).
3. Apply local config (if it exists).

Later layers win. This means a local config that sets only `model` will inherit all
other values from the global config or defaults. `json.JSONDecodeError` in either file
raises `SwarmError` with a human-readable message and a "fix or delete" hint — no
silent fallback.

### `save_config(data, *, local=True)`

Always writes to `LOCAL_CONFIG` unless `local=False` is explicitly passed. The wizard
never passes `local=False`, so it always writes `.swarm/config.json`. The parent
directory is created with `mkdir(parents=True, exist_ok=True)` — first call will
create the `.swarm/` directory.

The file is written as pretty-printed JSON with a trailing newline.

### `ensure_swarm_structure()`

Creates `.swarm/reports/` with `mkdir(parents=True, exist_ok=True)`. Called by
`personas customize`, not by the config wizard itself. The wizard relies on
`save_config()` creating `.swarm/` as a side effect.

---

## 7. Playwright Browser Check (`utils.py:118–126`)

```python
def check_playwright_browsers() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).exists()
    except Exception:
        return False
```

The check:
1. Imports the `playwright` package — if not installed at all, `ImportError` is caught
   and `False` is returned.
2. Starts a Playwright context manager (which initialises the browser registry).
3. Reads `p.chromium.executable_path` — the expected filesystem location of the
   Chromium binary — and checks whether it exists.

This is a filesystem check, not a launch test. A corrupt or incomplete Chromium
installation that exists on disk but fails to start would pass this check.

The `with sync_playwright() as p:` context manager spins up a subprocess briefly.
In practice this is fast but it does fork a process.

---

## 8. Provider Env Var Mapping (`utils.py:13–18`)

```python
def provider_env_var(provider: str) -> str:
    return next(
        (entry["env"] for entry in PROVIDERS if entry["key"] == provider),
        "OPENAI_API_KEY",
    )
```

Maps a provider key to its env var string. The fallback is `OPENAI_API_KEY` — so any
unknown provider key will be treated as OpenAI from LiteLLM's perspective. This function
is used in two places:

1. `_run_preflight()` — to tell the user which env var to set when the key is missing.
2. `_inject_api_key()` — to know which `os.environ` key to populate for LiteLLM.

---

## 9. Preflight Validation (`_run_preflight` — cli.py:88–113)

Not part of the wizard itself, but the wizard produces the config that `_run_preflight`
consumes. The preflight runs before every `swarm run` and enforces:

1. **Config exists** — either local or global. If neither exists, directs user to run bare `swarm`.
2. **Playwright installed** — only checked when `require_browser=True` (skipped for
   image/screenshot mode).
3. **API key present** — checks `config["api_key"]` first, then `os.environ[env_var]`.
   The env var override means CI pipelines can inject keys without touching the config
   file. If neither source has a key, exits with a message pointing to `swarm config`.

The preflight reads config through `load_config()`, so it picks up the full merged
DEFAULTS + global + local stack.

---

## 10. Full Data Flow Diagram

```
swarm (bare)                    swarm config
     │                               │
     ▼                               │
 home screen                         │
     │                               │
 config exists? ─── no ──► select("Run setup?")
                                     │
                              ◄──────┘  (both entry points converge here)
                                     │
                            ┌────────▼────────────────────────────────┐
                            │         _run_config_wizard()             │
                            │                                          │
                            │  1. select("LLM Provider")               │
                            │     → reads PROVIDERS list               │
                            │     → returns provider_key               │
                            │                                          │
                            │  2. click.prompt("API Key")  ◄──────┐   │
                            │     → fetch_provider_models()        │   │
                            │        → HTTP GET to provider API    │   │
                            │        → 401/403 → ProviderAuthError─┘   │
                            │        → other error → use MODELS[]      │
                            │        → success → live model list        │
                            │                                          │
                            │  3. select("Model")                      │
                            │     → options from step 2                │
                            │                                          │
                            │  4. check_playwright_browsers()          │
                            │     → found → green tick, continue       │
                            │     → missing → select("Install?")       │
                            │        → Yes → subprocess playwright     │
                            │        → No → continue                   │
                            │                                          │
                            │  5. save_config({provider, api_key, model})
                            │     → writes .swarm/config.json          │
                            └──────────────────────────────────────────┘
```

---

## 11. What the Wizard Does NOT Configure

These values exist in `DEFAULTS` and can appear in config files, but the wizard never
asks for them:

| Key                          | Default | Description                               |
|------------------------------|---------|-------------------------------------------|
| `default_agents`             | 20      | Number of simulated users per run         |
| `max_steps`                  | 3       | Max browser interaction steps per agent   |
| `viewport_width`             | 1280    | Browser viewport width in pixels          |
| `max_concurrent_browser`     | 5       | Parallel browser sessions                 |
| `max_concurrent_screenshot`  | 20      | Parallel screenshot analysis calls        |

Users who want to change these must edit `.swarm/config.json` by hand. All five can
also be overridden at run time via CLI flags on `swarm run`.

---

## 12. Key Invariants and Edge Cases

- **Config precedence**: global < local < CLI flags. The wizard always writes local.
- **Three-key write**: `save_config` in the wizard writes only `provider`, `api_key`,
  `model`. Prior local config is fully replaced (no merge on write, only on read).
- **API key not hidden**: `click.prompt()` without `hide_input=True` — the key is
  visible in the terminal while typing.
- **Empty model list**: If `fetch_provider_models()` returns `[]` (unknown provider key
  with no fallback in `MODELS`), `select("Model", [])` is called. `select()` would
  attempt `options[0]` on commit with an empty list — IndexError. This cannot happen
  with the current four supported providers but is a latent bug for future providers.
- **Playwright install failure**: `subprocess.run(..., check=True)` propagates
  `CalledProcessError` unhandled. Config is not saved if this crashes.
- **Ctrl-C in `select()`**: Delivered as `\x03` raw byte; treated as unknown key
  (no-op). Terminal is restored by the `atexit` handler, but the wizard hangs until
  Enter is pressed. There is no escape hatch from an arrow-key menu without pressing Enter.
- **Corrupt config file**: `load_config()` raises `SwarmError` with a message and
  "fix or delete" hint. `_run_config_wizard()` does not call `load_config()` — it
  writes a fresh file — so running `swarm config` is the recovery path for a corrupt config.
- **Global config path**: `~/.config/uxswarm/config.json`. The wizard never writes here
  (no `local=False` call). Manual editing or a future `--global` flag would be needed.
