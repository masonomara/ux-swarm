import json
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import click
from rich.console import Console

from ux_swarm.menu import GoBack, select

LOCAL_DIR = Path(".swarm")
LOCAL_CONFIG = LOCAL_DIR / "config.json"
GLOBAL_CONFIG = Path.home() / ".config" / "ux-swarm" / "config.json"

PROVIDERS: list[dict[str, str]] = [
    {
        "name": "OpenAI",
        "key": "openai",
        "env": "OPENAI_API_KEY"
    },
    {
        "name": "Anthropic",
        "key": "anthropic",
        "env": "ANTHROPIC_API_KEY"
    },
    {
        "name": "Google Gemini",
        "key": "gemini",
        "env": "GEMINI_API_KEY"
    },
]

console = Console()


class ProviderAuthError(Exception):
    """Raised when a provider rejects an API key (HTTP 401/403)."""


def fetch_provider_models(provider_key: str, api_key: str) -> list[str]:
    """Fetch available models from the provider API.

    Raises ProviderAuthError on 401/403.
    Raises urllib.error.URLError / urllib.error.HTTPError on other failures.
    """
    try:
        if provider_key == "openai":
            req = urllib.request.Request(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return sorted(
                f"openai/{m['id']}" for m in data.get("data", [])
                if ":" not in m["id"] and
                (m["id"].startswith("gpt-") or m["id"].startswith("chatgpt-")
                 or (m["id"][:1] == "o" and m["id"][1:2].isdigit())))

        if provider_key == "anthropic":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models?limit=1000",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01"
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return [f"anthropic/{m['id']}" for m in data.get("data", [])]

        if provider_key == "gemini":
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/openai/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return sorted(f"gemini/{m['id']}" for m in data.get("data", [])
                          if m["id"].startswith("gemini-")
                          and "embedding" not in m["id"])

        raise ValueError(f"Unknown provider: {provider_key}")

    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderAuthError from exc
        raise


def load_config() -> dict:
    """Load and merge global and local config files. Local keys override global keys."""
    resolved: dict = {}
    for path in (GLOBAL_CONFIG, LOCAL_CONFIG):
        if path.exists():
            try:
                resolved.update(json.loads(path.read_text()))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Config file is not valid JSON: {path}\n{exc}\nFix or delete it and run again."
                ) from exc
    return resolved


def save_config(data: dict, *, local: bool = True) -> Path:
    """Write config to the local config file; pass local=False to write to the global config instead."""
    target = LOCAL_CONFIG if local else GLOBAL_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")
    return target


def _wizard_step_provider(state: dict) -> None:
    """Wizard step: prompt the user to select an LLM provider."""
    names = [p["name"] for p in PROVIDERS]
    default = next(
        (i for i, p in enumerate(PROVIDERS)
         if p["key"] == state.get("provider_key")),
        0,
    )
    chosen_name = select("Which LLM provider would you like to use?",
                         names,
                         default_index=default)
    provider = next(p for p in PROVIDERS if p["name"] == chosen_name)
    state["provider_key"] = provider["key"]
    state["provider_name"] = provider["name"]


def _wizard_step_api_key(state: dict) -> None:
    """Wizard step: prompt for and validate an API key against the selected provider."""
    error_lines = 0
    while True:
        api_key = click.prompt("\033[1mAPI Key\033[0m",
                               default="",
                               show_default=False).strip()
        cols = shutil.get_terminal_size().columns
        wrapped = max(1, (len("API Key: ") + len(api_key) + cols - 1) // cols)
        sys.stdout.write(f"\x1b[{wrapped + error_lines}A\x1b[J")
        sys.stdout.flush()
        error_lines = 0

        if not api_key:
            console.print("[red]API key is required.[/]")
            error_lines = 1
            continue

        try:
            model_options = fetch_provider_models(state["provider_key"],
                                                  api_key)
            state["api_key"] = api_key
            state["model_options"] = model_options
            return
        except ProviderAuthError:
            console.print("[red]Invalid API key, please try again.[/]")
            error_lines = 1
        except Exception as exc:
            console.print(
                f"[red]Could not reach {state['provider_name']} API.[/]")
            console.print(f"[dim]{exc}[/]")
            console.print("[dim]Check your network and try again.[/]")
            raise SystemExit(1)


def _wizard_step_model(state: dict) -> None:
    """Wizard step: prompt the user to select a model from the provider's available list."""
    options = state["model_options"]
    default = next(
        (i for i, m in enumerate(options) if m == state.get("model")),
        0,
    )
    state["model"] = select("Which model would you like to use?",
                            options,
                            default_index=default)


def playwright_state() -> tuple[bool, bool]:
    """Return (playwright_installed, chromium_installed) as a pair of booleans."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, False
    try:
        with sync_playwright() as p:
            return True, Path(p.chromium.executable_path).exists()
    except Exception:
        return True, False


def _wizard_step_playwright(state: dict) -> None:
    """Wizard step: offer to install Playwright and Chromium if not already present."""
    playwright_ok, chromium_ok = playwright_state()

    if playwright_ok and chromium_ok:
        console.print(
            "[bold]Would you like to install Chromium for Playwright?[/] [dim]Installed[/]"
        )
        state["playwright_ok"] = True
        return

    if not playwright_ok:
        choice = select("Would you like to install Playwright?", ["Yes", "No"],
                        echo=False)
        if choice == "No":
            state["playwright_ok"] = False
            return
        _install_playwright()
        _, chromium_ok = playwright_state()

    if not chromium_ok:
        choice = select("Would you like to install Chromium for Playwright?",
                        ["Yes", "No"],
                        echo=False)
        if choice == "No":
            state["playwright_ok"] = False
            return
        _install_chromium()
        console.print(
            "[bold]Would you like to install Chromium for Playwright?:[/] [dim]Installed[/]"
        )

    state["playwright_ok"] = True


def _install_playwright() -> None:
    """Run pip install playwright as a subprocess, printing any errors."""
    with console.status("Installing Playwright…"):
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "playwright"],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        console.print("[red]Playwright install failed.[/]")
        if result.stderr.strip():
            console.print(f"[dim]{result.stderr.strip()}[/]")


def _install_chromium() -> None:
    """Run playwright install chromium as a subprocess, printing any errors."""
    console.print("")
    with console.status("Installing Chromium…"):
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        console.print("[red]Chromium install failed.[/]")
        if result.stderr.strip():
            console.print(f"[dim]{result.stderr.strip()}[/]")
        console.print("[dim]Run: playwright install chromium[/]")


def run_config_wizard() -> None:
    """Run the interactive setup wizard, collecting provider, API key, model, and Playwright config."""
    state: dict = {}
    steps = [
        _wizard_step_provider,
        _wizard_step_api_key,
        _wizard_step_model,
        _wizard_step_playwright,
    ]
    i = 0
    while i < len(steps):
        try:
            steps[i](state)
            i += 1
        except GoBack:
            i = max(0, i - 1)

    saved_path = save_config({
        "provider": state["provider_key"],
        "api_key": state["api_key"],
        "model": state["model"],
    })
    console.print(f"\n[green]Config saved →[/] {saved_path}\n")
