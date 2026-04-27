import asyncio
import json
import os
from collections import Counter
from importlib.metadata import metadata, PackageNotFoundError
from pathlib import Path

import click
from rich.console import Console
from rich.live import Live
from rich.spinner import Spinner

from ux_swarm.cli import SmartGroup
from ux_swarm.config import GLOBAL_CONFIG, LOCAL_CONFIG, LOCAL_DIR, PROVIDERS, playwright_state, load_config, run_config_wizard
from ux_swarm.menu import select
from ux_swarm.models import SwarmResult
from ux_swarm.personas import load_users
from ux_swarm.swarm import run_screenshot_swarm

try:
    _meta = metadata("ux-swarm")
    __version__ = _meta["Version"]
    __description__ = _meta["Summary"]
except PackageNotFoundError:
    __version__ = "unknown"
    __description__ = ""

_console = Console()
RESULTS_JSON = LOCAL_DIR / "results.json"


def _inject_api_key(provider: str, api_key: str) -> None:
    """Write the configured API key into the environment so LiteLLM can read it."""
    env_var = next((p["env"] for p in PROVIDERS if p["key"] == provider), None)
    if env_var and api_key:
        os.environ[env_var] = api_key


ASCII_ART = ("  __  ___  __    _____      _____   ___  __  ___\n"
             " / / / / |/_/___/ __/ | /| / / _ | / _ \\/  |/  /\n"
             "/ /_/ />  </___/\\ \\ | |/ |/ / __ |/ , _/ /|_/ /\n"
             "\\____/_/|_|   /___/ |__/|__/_/ |_/_/|_/_/  /_/")


def _print_header() -> None:
    """Print the ASCII banner, version, description, and current config status."""
    _console.print("\n" + ASCII_ART, highlight=False)
    _console.print(f"\nux-swarm - v{__version__}\n", highlight=False)
    _console.print(
        "Synthetic user testing. Simulates a swarm of users who intereact with your target URL or screenshot to complete a specific task.\n"
    )

    config = load_config()

    _, chromium_ok = playwright_state()
    playwright_label = "Enabled" if chromium_ok else "Needs configuration"

    provider_key = config.get("provider")
    provider_name = (next(
        (p["name"] for p in PROVIDERS if p["key"] == provider_key),
        provider_key) if provider_key else "Needs configuration")

    raw_model = config.get("model", "")
    model_display = raw_model.split("/",
                                    1)[-1] if "/" in raw_model else raw_model
    model_label = model_display if model_display else "Needs configuration"

    pw_dot = "[green]•[/]" if chromium_ok else "[red]•[/]"
    provider_dot = "[green]•[/]" if provider_key else "[red]•[/]"
    model_dot = "[green]•[/]" if model_display else "[red]•[/]"

    _console.print(f"{provider_dot} LLM Provider: {provider_name}",
                   highlight=False)
    _console.print(f"{model_dot} Model: {model_label}", highlight=False)
    _console.print(f"{pw_dot} Playwright: {playwright_label}\n",
                   highlight=False)
    _console.print("---\n")


def _print_home() -> None:
    """Print the home screen: header plus usage and command hints."""
    _print_header()
    _console.print("Usage:\n")
    _console.print("  swarm <target> <task>\n", highlight=False)
    _console.print("Commands:\n")
    _console.print("  config   Run setup wizard")
    _console.print("  users    List active user types")
    _console.print("  results  View saved results")
    _console.print("  help     View all commands")
    _console.print("\n---\n")


@click.group(cls=SmartGroup, invoke_without_command=True, help=__description__)
@click.version_option(version=__version__, message="ux-swarm v%(version)s")
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        if not (LOCAL_CONFIG.exists() or GLOBAL_CONFIG.exists()):
            _print_header()
            if select("Run setup wizard?", ["Yes", "No"], echo=False) == "Yes":
                run_config_wizard()
        else:
            _print_home()


@cli.command()
def config():
    """Run the setup wizard: provider, API key, model, Chromium."""
    _print_header()
    if select("Run setup wizard?", ["Yes", "No"], echo=False) == "Yes":
        run_config_wizard()


@cli.command()
@click.pass_context
def help(ctx):
    """Show this help message."""
    click.echo(ctx.parent.get_help())


# TODO: find a permanent home for these once the run command is built out
RUN_DEFAULTS: dict[str, int] = {
    "default_users": 20,
    "max_steps": 3,
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 5,
}


def _print_swarm_result(result: SwarmResult) -> None:
    """Render aggregated swarm results to the terminal between rule dividers."""
    filename = Path(result.target).name
    rate_pct = f"{result.completion_rate:.0%}"
    moe_pct = f"±{result.margin_of_error:.0%}"

    if result.completion_rate >= 0.8:
        rate_style = "green"
    elif result.completion_rate >= 0.5:
        rate_style = "yellow"
    else:
        rate_style = "red"

    _console.print()
    _console.rule(style="dim")
    _console.print()
    _console.print(f"  {filename} — \"{result.task}\"", highlight=False)
    _console.print()
    _console.print(
        f"  [{rate_style}][bold]{rate_pct}[/bold][/]  {moe_pct}  ·  {result.users} agents",
        highlight=False,
    )

    if len(result.user_breakdown) > 1:
        _console.print()
        _console.print("  User Breakdown", highlight=False)
        for label, rate in result.user_breakdown.items():
            _console.print(f"  [dim]{label:<20}[/]  {rate:.0%}",
                           highlight=False)

    if result.friction_points:
        top = Counter(fp for fp in result.friction_points if fp).most_common(5)
        _console.print()
        _console.print("  Friction", highlight=False)
        for point, _ in top:
            _console.print(f"  [dim]•[/] {point}", highlight=False)

    _console.print()
    model_line = result.model
    if result.total_cost:
        model_line += f"  ·  ${result.total_cost:.4f}"
    timestamp = result.timestamp[:16].replace("T", "  ")
    model_line += f"  ·  {timestamp}  ·  {result.mode}"
    _console.print(f"  [dim]{model_line}[/]", highlight=False)
    _console.print()
    _console.rule(style="dim")
    _console.print()


@cli.command(hidden=True)
@click.argument("target")
@click.argument("task")
@click.option("--users",
              default=None,
              type=int,
              help="Number of simulated users")
@click.option("--max-steps",
              default=None,
              type=int,
              help="Max interaction steps per agent (browser only)")
@click.option("--viewport",
              default=None,
              type=int,
              help="Viewport width in pixels (browser only)")
@click.option("--verbose", is_flag=True, help="Show full tracebacks on error")
@click.pass_context
def run(ctx, target, task, users, max_steps, viewport, verbose):
    """Run a swarm of simulated users against a URL or screenshot image."""
    if target.startswith("http://") or target.startswith("https://"):
        raise click.ClickException(
            "URL targets require browser mode, which is not yet available. "
            "Pass a screenshot image path instead.")

    if not Path(target).exists():
        raise click.ClickException(f"Image not found: {target}")

    config = load_config()
    model_full = config.get("model", "")
    api_key = config.get("api_key", "")
    provider = config.get("provider", "")

    if not model_full:
        raise click.ClickException(
            "No model configured — run `swarm config` to set one.")
    if not api_key:
        raise click.ClickException(
            "No API key configured — run `swarm config` to set one.")

    _inject_api_key(provider, api_key)

    num_agents = users or RUN_DEFAULTS["default_users"]
    max_concurrent = RUN_DEFAULTS["max_concurrent_screenshot"]

    user_types = load_users()

    try:
        with Live(
                Spinner("dots",
                        text=f"  0 / {num_agents}  agents running",
                        style="dim"),
                console=_console,
                refresh_per_second=10,
        ) as live:

            def on_done(done: int, total: int) -> None:
                live.update(
                    Spinner("dots",
                            text=f"  {done} / {total}  agents complete",
                            style="dim"), )

            result = asyncio.run(
                run_screenshot_swarm(
                    target=target,
                    task=task,
                    users=user_types,
                    num_agents=num_agents,
                    model=model_full,
                    max_concurrent=max_concurrent,
                    on_agent_done=on_done,
                ))
    except click.ClickException:
        raise
    except Exception as exc:
        if verbose:
            raise
        raise click.ClickException(str(exc)) from exc

    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(
            RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else []
        existing.append(result.model_dump())
        RESULTS_JSON.write_text(json.dumps(existing, indent=2) + "\n")
    except (OSError, json.JSONDecodeError) as exc:
        _console.print(f"[dim]Warning: could not save results: {exc}[/]")

    _print_swarm_result(result)


@cli.command()
@click.option("--config",
              "write_config",
              is_flag=True,
              help="Write .swarm/users.json for editing")
def users(write_config):
    """List active user types and weights. Use --config to write .swarm/users.json."""
    from ux_swarm.personas import USERS_JSON, write_default_users

    if write_config:
        if USERS_JSON.exists():
            _console.print(f"[dim]{USERS_JSON} already exists.[/]")
        else:
            path = write_default_users()
            _console.print(f"[green]Written →[/] {path}")
            _console.print(
                "[dim]Edit the file to define user types and weights.[/]")
        return

    active = load_users()
    total_weight = sum(u.weight for u in active)
    _console.print()
    for u in active:
        share = u.weight / total_weight
        _console.print(f"  [bold]{u.label}[/]  [dim]{share:.0%}[/]",
                       highlight=False)
        _console.print(
            f"  [dim]{u.description[:120]}{'…' if len(u.description) > 120 else ''}[/]",
            highlight=False,
        )
        _console.print()


@cli.command()
@click.option("-n", default=None, type=int, help="Show last N results")
def results(n):
    """List saved swarm results."""
    if not RESULTS_JSON.exists():
        _console.print(
            "[dim]No results yet. Run `swarm <target> <task>` to get started.[/]"
        )
        return

    try:
        entries = json.loads(RESULTS_JSON.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"Could not read results.json: {exc}") from exc

    if not entries:
        _console.print("[dim]No results yet.[/]")
        return

    if n:
        entries = entries[-n:]

    for entry in entries:
        _print_swarm_result(SwarmResult.model_validate(entry))


if __name__ == "__main__":
    cli()
