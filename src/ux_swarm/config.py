import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

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
    {
        "name": "DeepSeek",
        "key": "deepseek",
        "env": "DEEPSEEK_API_KEY"
    },
]

console = Console()


class ProviderAuthError(Exception):
    """Raised when a provider rejects an API key (HTTP 401/403)."""


def provider_env_var(provider_key: str) -> str:
    return next(p["env"] for p in PROVIDERS if p["key"] == provider_key)


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

        if provider_key == "deepseek":
            req = urllib.request.Request(
                "https://api.deepseek.com/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return [f"deepseek/{m['id']}" for m in data.get("data", [])]

        raise ValueError(f"Unknown provider: {provider_key}")

    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderAuthError from exc
        raise


def load_config() -> dict:
    resolved: dict = {}
    for path in (GLOBAL_CONFIG, LOCAL_CONFIG):
        if path.exists():
            try:
                resolved.update(json.loads(path.read_text()))
            except json.JSONDecodeError as exc:
                raise click.ClickException(
                    f"Config file is not valid JSON: {path}\n{exc}\nFix or delete it and run again."
                ) from exc
    return resolved


def save_config(data: dict, *, local: bool = True) -> Path:
    target = LOCAL_CONFIG if local else GLOBAL_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")
    return target


def check_chromium_installed() -> bool:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        return Path(p.chromium.executable_path).exists()


def _wizard_step_provider(state: dict) -> None:
    names = [p["name"] for p in PROVIDERS]
    default = next(
        (i for i, p in enumerate(PROVIDERS)
         if p["key"] == state.get("provider_key")),
        0,
    )
    chosen_name = select("LLM Provider", names, default_index=default)
    provider = next(p for p in PROVIDERS if p["name"] == chosen_name)
    state["provider_key"] = provider["key"]
    state["provider_env"] = provider["env"]
    state["provider_name"] = provider["name"]


def _wizard_step_api_key(state: dict) -> None:
    while True:
        api_key = click.prompt("API Key", default="",
                               show_default=False).strip()

        if not api_key:
            console.print("[red]API key is required.[/]")
            continue

        try:
            model_options = fetch_provider_models(state["provider_key"],
                                                  api_key)
            state["api_key"] = api_key
            state["model_options"] = model_options
            return
        except ProviderAuthError:
            console.print("[red]Invalid API key, please try again.[/]")
        except Exception as exc:
            console.print(
                f"[red]Could not reach {state['provider_name']} API.[/]")
            console.print(f"[dim]{exc}[/]")
            console.print("[dim]Check your network and try again.[/]")
            raise SystemExit(1)


def _wizard_step_model(state: dict) -> None:
    options = state["model_options"]
    default = next(
        (i for i, m in enumerate(options) if m == state.get("model")),
        0,
    )
    state["model"] = select("Model", options, default_index=default)


def _wizard_step_playwright(state: dict) -> None:
    console.print("\n[bold]Playwright[/]")
    console.print("─" * 40)

    if check_chromium_installed():
        console.print("  [green]•[/] Chromium installed")
        state["playwright_ok"] = True
        return

    console.print(
        "  [yellow]•[/] Chromium not found — required for browser mode\n")
    choice = select("Install Chromium now?", ["Yes", "No, skip for now"])

    if choice.startswith("No"):
        state["playwright_ok"] = False
        return

    state["playwright_ok"] = _install_chromium()


def _install_chromium() -> bool:
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
        return False
    console.print("[green]✓[/] Chromium installed")
    return True


def _wizard_step_confirm(state: dict) -> None:
    key = state["api_key"]
    masked = key[:8] + "…" + key[-4:] if len(key) > 12 else "•" * len(key)
    chromium_status = "[green]✓ installed[/]" if state.get(
        "playwright_ok") else "[yellow]not installed[/]"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Provider", state["provider_name"])
    table.add_row("Model", state["model"])
    table.add_row("API Key", masked)
    table.add_row("Chromium", chromium_status)

    console.print()
    console.print(table)
    console.print()

    choice = select("Save config?", ["Yes, save", "Go back"])
    if choice == "Go back":
        raise GoBack


def run_config_wizard() -> None:
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
            if i == 3 and state.get("playwright_ok"):
                i = max(0, i - 1)

    saved_path = save_config({
        "provider": state["provider_key"],
        "api_key": state["api_key"],
        "model": state["model"],
    })
    console.print(f"\n[green]Config saved →[/] {saved_path}\n")
