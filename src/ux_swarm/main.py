import os

import click
from importlib.metadata import metadata, PackageNotFoundError
from rich.console import Console

from ux_swarm.cli import SmartGroup
from ux_swarm.config import (
    GLOBAL_CONFIG,
    LOCAL_CONFIG,
    PROVIDERS,
    ProviderAuthError,
    check_chromium_installed,
    fetch_provider_models,
    save_config,
)
from ux_swarm.menu import GoBack, select

console = Console()

try:
    _meta = metadata("ux-swarm")
    __version__ = _meta["Version"]
    __description__ = _meta["Summary"]
except PackageNotFoundError:
    __version__ = "unknown"
    __description__ = ""


@click.group(cls=SmartGroup, invoke_without_command=True, help=__description__)
@click.version_option(version=__version__, message="ux-swarm v%(version)s")
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


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

    console.print("  [yellow]•[/] Chromium not found — required for browser mode\n")
    choice = select("Install Chromium now?", ["Yes", "No, skip for now"])

    if choice.startswith("No"):
        state["playwright_ok"] = False
        return

    _install_chromium()
    state["playwright_ok"] = True


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


def _wizard_step_confirm(state: dict) -> None:
    from rich.table import Table

    key = state["api_key"]
    masked = key[:8] + "…" + key[-4:] if len(key) > 12 else "•" * len(key)
    source_note = " [dim](from environment)[/]" if state.get("api_key_source") == "env" else ""
    chromium_status = "[green]✓ installed[/]" if state.get("playwright_ok") else "[yellow]not installed[/]"

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="dim")
    table.add_column()
    table.add_row("Provider", state["provider_name"])
    table.add_row("Model", state["model"])
    table.add_row("API Key", f"{masked}{source_note}")
    table.add_row("Chromium", chromium_status)

    console.print()
    console.print(table)
    console.print()

    choice = select("Save config?", ["Yes, save", "Go back"])
    if choice == "Go back":
        raise GoBack


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

    saved_path = save_config({
        "provider": state["provider_key"],
        "api_key": state["api_key"],
        "model": state["model"],
    })
    console.print(f"\n[green]Config saved →[/] {saved_path}\n")


# TODO: find a permanent home for these once the run command is built out
RUN_DEFAULTS: dict[str, int | float] = {
    "default_users": 20,
    "max_steps": 3,
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 20,
}


# TODO: move alongside run command output logic once that is built out
def ensure_swarm_structure() -> None:
    from ux_swarm.config import LOCAL_DIR
    (LOCAL_DIR / "reports").mkdir(parents=True, exist_ok=True)


@cli.command(hidden=True)
@click.argument("target")
@click.argument("task")
@click.option(
    "--users",
    default=None,
    type=int,
    help="Number of simulated users")
@click.option(
    "--max-steps",
    default=None,
    type=int,
    help="Max interaction steps per agent (browser only)",
)
@click.option(
    "--viewport",
    default=None,
    type=int,
    help="Viewport width in pixels (browser only)",
)
@click.option(
    "--verbose",
    is_flag=True,
    help="Show full tracebacks on error"
)
@click.pass_context
def run(ctx, target, task, users, max_steps, viewport, verbose):
    """Run a swarm of simulated users against a URL or screenshot image."""
    pass


if __name__ == "__main__":
    cli()
