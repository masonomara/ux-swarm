import asyncio
import json
import os
import textwrap
from collections import Counter, deque
from importlib.metadata import metadata, PackageNotFoundError
from pathlib import Path

import click
from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from ux_swarm.cli import SmartGroup
from ux_swarm.config import GLOBAL_CONFIG, LOCAL_CONFIG, LOCAL_DIR, PROVIDERS, playwright_state, load_config, run_config_wizard
from ux_swarm.menu import select
from ux_swarm.models import AgentResult, SwarmResult
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


def _print_swarm_result(result: SwarmResult,
                        show_header: bool = False) -> None:
    """Render aggregated swarm results to the terminal."""
    rate_pct = f"{result.completion_rate:.0%}"
    moe_pct = f"±{result.margin_of_error:.0%}"

    if show_header:
        filename = Path(result.target).name
        _console.print()
        _console.print(f"{filename}: {result.task}", highlight=False)
        _console.print()
        _console.print("---", highlight=False)

    _console.print(f"{rate_pct} of agents completed the task:",
                   highlight=False)

    if len(result.user_breakdown) > 1:
        _console.print()
        for label, rate in result.user_breakdown.items():
            _console.print(f"  {label:<20}  {rate:.0%}", highlight=False)

    if result.friction_points:
        top = Counter(fp for fp in result.friction_points if fp).most_common(5)
        _console.print()
        _console.print("Pain points:", highlight=False)
        _console.print()
        for point, count in top:
            prefix = f"  {count}x "
            max_point = _console.width - len(prefix) - 1
            display = point if len(point) <= max_point else point[:max_point -
                                                                  1] + "…"
            _console.print(f"{prefix}{display}", highlight=False)

    _console.print()
    _console.print("---", highlight=False)
    _console.print()
    _console.print(f"{result.users} agents run ({moe_pct} margin of error)",
                   highlight=False)
    _console.print(f"Report saved to [dim]{RESULTS_JSON}[/]", highlight=False)
    _console.print()
    if not show_header:
        _console.print("[dim]run `swarm expand` to see full swarm details[/]",
                       highlight=False)
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

    filename = Path(target).name
    _console.print()
    _console.print(f"{filename}: {task}", highlight=False)
    _console.print()
    _console.print("---", highlight=False)
    _console.print()

    # (numbered_label, completed, body)
    recent: deque[tuple[str, bool | None, str]] = deque(maxlen=5)
    label_counts: Counter[str] = Counter()

    def _build_display(done: int, total: int) -> Group:
        running = min(total - done, max_concurrent)
        spinner = Spinner(
            "dots",
            text=f" {running} agents running ({done}/{total} complete)",
            style="dim",
        )
        lines: list[Text] = [Text("")]
        pad = max((len(lbl) for lbl, _, _ in recent), default=0) + 2
        for numbered_label, completed, body in recent:
            color = "green" if completed else "red"
            label_str = numbered_label.ljust(pad)
            max_body = max(_console.width - pad - 4, 10)
            body_display = body if len(body) <= max_body else body[:max_body -
                                                                   1] + "…"
            lines.append(
                Text.from_markup(f"[{color}]{label_str}[/] {body_display}"))
        lines.append(Text(""))

        lines.append(
            Text.from_markup(
                "[dim]run `swarm expand` to see full swarm details[/]"))
        lines.append(Text(""))
        lines.append(Text(""))
        return Group(spinner, *lines)

    try:
        with Live(
                _build_display(0, num_agents),
                console=_console,
                refresh_per_second=10,
                transient=True,
        ) as live:

            def on_done(done: int, total: int,
                        agent_result: AgentResult | None) -> None:
                if agent_result is not None:
                    label_counts[agent_result.user_type] += 1
                    numbered_label = f"{agent_result.user_type} {label_counts[agent_result.user_type]}"
                    body = agent_result.comment or agent_result.abandonment_reason or ""
                    recent.append(
                        (numbered_label, agent_result.completed, body))
                live.update(_build_display(done, total))

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

    rows = []
    for entry in entries:
        r = SwarmResult.model_validate(entry)
        rows.append((
            Path(r.target).name,
            r.task,
            f"{r.completion_rate:.0%}",
            r.timestamp[:10],
        ))

    col_w = [max(len(row[i]) for row in rows) for i in range(4)]

    _console.print()
    for filename, task, rate, date in rows:
        _console.print(
            f"  {filename.ljust(col_w[0])}  {task.ljust(col_w[1])}  {rate.ljust(col_w[2])}  [dim]{date}[/]",
            highlight=False,
        )
    _console.print()


@cli.command()
def expand():
    """Show full agent-by-agent breakdown of the most recent result."""
    if not RESULTS_JSON.exists():
        _console.print("[dim]No results yet.[/]")
        return

    try:
        entries = json.loads(RESULTS_JSON.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(
            f"Could not read results.json: {exc}") from exc

    if not entries:
        _console.print("[dim]No results yet.[/]")
        return

    result = SwarmResult.model_validate(entries[-1])
    filename = Path(result.target).name

    _console.print()
    _console.print(f"{filename}: {result.task}", highlight=False)
    _console.print()
    _console.print("---", highlight=False)
    _console.print()

    w = _console.width
    label_counts: Counter[str] = Counter()
    for r in result.individual_results:
        label_counts[r.user_type] += 1
        numbered = f"{r.user_type} {label_counts[r.user_type]}"
        color = "green" if r.completed else "red"
        comment = r.comment or ""
        _console.print(f"[{color}]{numbered}[/] - {comment}", highlight=False)
        bullets = ([r.abandonment_reason]
                   if r.abandonment_reason else []) + r.friction_points
        for bullet in bullets:
            bullet_lines = textwrap.wrap(bullet, width=w - 6)
            for i, line in enumerate(bullet_lines):
                prefix = "[dim]· " if i == 0 else "[dim]  "
                _console.print(f"{prefix}{line}[/]", highlight=False)
        _console.print()

    if result.friction_points:
        _console.print("---", highlight=False)
        _console.print()
        _console.print("All pain points:", highlight=False)
        _console.print()
        for point, count in Counter(fp for fp in result.friction_points
                                    if fp).most_common():
            _console.print(f"  {count}x {point}", highlight=False)
        _console.print()


if __name__ == "__main__":
    cli()
