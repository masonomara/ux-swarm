import asyncio
import json
import os
import re
import sys
import textwrap
import time
from collections import Counter
from importlib.metadata import metadata, PackageNotFoundError
from pathlib import Path

import click
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from ux_swarm.cli import CliError, SmartGroup, SwarmCommand
from ux_swarm.config import GLOBAL_CONFIG, LOCAL_CONFIG, LOCAL_DIR, PROVIDERS, playwright_state, load_config, run_config_wizard
from ux_swarm.menu import select
from ux_swarm.models import AgentResult, SwarmResult, UserType
from ux_swarm.personas import distribute_users, load_users
from ux_swarm.swarm import run_browser_swarm, run_screenshot_swarm

try:
    _meta = metadata("ux-swarm")
    __version__ = _meta["Version"]
    __description__ = _meta["Summary"]
except PackageNotFoundError:
    __version__ = "unknown"
    __description__ = ""

_console = Console()
RESULTS_JSON = LOCAL_DIR / "results.json"

# Bare hostname/domain — no scheme required. Matches:
#   masonomara.com  sub.domain.co.uk  localhost:3000  192.168.1.1:8080  example.com/path
_BARE_URL_RE = re.compile(
    r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?::\d+)?(?:/.*)?$'
    r'|^localhost(?::\d+)?(?:/.*)?$'
    r'|^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/.*)?$')


def _resolve_target(target: str) -> tuple[bool, str]:
    """Return (is_url, normalised_target). Prepends https:// to bare domains."""
    if target.startswith("http://") or target.startswith("https://"):
        return True, target
    if Path(target).exists():
        return False, target
    if _BARE_URL_RE.match(target):
        return True, "https://" + target
    return False, target


def _inject_api_key(provider: str, api_key: str) -> None:
    """Write the configured API key into the environment so LiteLLM can read it."""
    env_var = next((p["env"] for p in PROVIDERS if p["key"] == provider), None)
    if env_var and api_key:
        os.environ[env_var] = api_key


ASCII_ART = ("\n[cyan]  ▄       ▄      ▄▄▄▄▄    ▄ ▄   ██   █▄▄▄▄ █▀▄▀█ \n"
             "   █  ▀▄   █ ▄▄ █     ▀▄ █   █  █ █  █  ▄▀ █ █ █ \n"
             "█   █   █ ▀      ▀▀▀▀▄  █ ▄   █ █▄▄█ █▀▀▌  █ ▄ █ \n"
             "█   █  ▄ █     ▀▄▄▄▄▀   █  █  █ █  █ █  █  █   █ \n"
             "█▄ ▄█ █   ▀▄             █ █ █     █   █      █  \n"
             " ▀▀▀   ▀                  ▀ ▀     █   ▀      ▀   \n"
             f"[/]v{__version__} - genesis                  [cyan]▀[/]\n")


def _print_config_status() -> None:
    """Print the current config status lines (provider, model, playwright)."""
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


def _print_header() -> None:
    """Print the ASCII banner, version, description, and current config status."""
    _console.print(ASCII_ART, highlight=False)
    _console.print(
        "Simulate a swarm of synthetic users analyzing a screenshot or\n"
        "browsing a url to complete a task, then aggregate UX findings.\n",
        highlight=False)
    _print_config_status()


@click.group(cls=SmartGroup,
             invoke_without_command=True,
             context_settings={
                 "help_option_names": ["-h", "--help"],
                 "max_content_width": 200
             })
@click.version_option(__version__,
                      "-V",
                      "--version",
                      message="\n%(version)s\n",
                      help="Output the version number")
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        _print_header()
        if not (LOCAL_CONFIG.exists() or GLOBAL_CONFIG.exists()):
            if select("[green]?[/] [bold]Run setup wizard:[/]",
                      ["Proceed", "Cancel"],
                      echo=False) == "Proceed":
                w = _console.width
                desc = (
                    "Simulate a swarm of synthetic users analyzing a screenshot or\n"
                    "browsing a url to complete a task, then aggregate UX findings."
                )
                desc_lines = sum(
                    max(1, (len(l) + w - 1) // w)
                    for l in desc.split('\n')) + 1
                sys.stdout.write(f"\x1b[{desc_lines + 4}A\x1b[J")
                sys.stdout.flush()
                run_config_wizard()
        else:
            click.echo(ctx.get_help().lstrip('\n'))


@cli.command(cls=SwarmCommand,
             short_help="Configure your LLM provider, model, and browser")
def config():
    """Configure your LLM provider, model, and browser"""
    _console.print()
    _console.print(
        "Configure your LLM provider, model, and browser\n[dim]Press Ctrl+C to cancel.[/]\n",
        highlight=False)
    _print_config_status()
    if select("[green]?[/] [bold]Run setup wizard:[/]", ["Proceed", "Cancel"],
              echo=False) == "Proceed":
        # Erase: "Configure your LLM provider, model, and browser" + "Press Ctrl+C..." + blank + 3 bullet lines + blank = 7 lines
        sys.stdout.write("\x1b[7A\x1b[J")
        sys.stdout.flush()
        _console.print(
            "Configure your LLM provider, model, and browser\n[dim]Press Ctrl+C to cancel.[/]\n",
            highlight=False)
        run_config_wizard()


@cli.command(cls=SwarmCommand, short_help="Display help for a command")
@click.argument("command", required=False, default=None, metavar="[command]")
@click.pass_context
def help(ctx, command):
    """Display help for a command"""
    if command:
        cmd = cli.commands.get(command)
        if cmd is None:
            raise CliError(f"No such command '{command}'.")
        with click.Context(cmd, parent=ctx.parent,
                           info_name=command) as sub_ctx:
            click.echo(cmd.get_help(sub_ctx))
    else:
        click.echo(ctx.parent.get_help())


# TODO: find a permanent home for these once the run command is built out
RUN_DEFAULTS: dict[str, int] = {
    "default_users": 20,
    "max_steps": 8,
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 5,
}

_STATUS_COLORS = {
    "waiting": "dim",
    "running": "blue",
    "complete": "green",
    "failed": "red",
}


def _build_display(
    agent_labels: dict[int, str],
    agent_states: dict[int, tuple[str, int, str]],
    done_count: int,
    num_agents: int,
    start_time: float,
    max_steps: int | None = None,
) -> Group:
    table = Table(show_header=False, box=None, padding=(0, 1, 0, 0))
    table.add_column(width=20, no_wrap=True)
    table.add_column(width=10, no_wrap=True)
    if max_steps is not None:
        table.add_column(width=12, no_wrap=True)
    table.add_column(no_wrap=True)

    for agent_id in sorted(agent_labels):
        label = agent_labels[agent_id]
        status, step, detail = agent_states.get(agent_id, ("waiting", 0, ""))
        color = _STATUS_COLORS.get(status, "white")
        is_active = status == "running"
        max_detail = max(
            _console.width - (44 if max_steps is not None else 32), 10)
        detail_display = detail if len(
            detail) <= max_detail else detail[:max_detail - 1] + "…"
        row: list = [Text(label), Text(status, style=color)]
        if max_steps is not None:
            row.append(f"step {step}/{max_steps}" if is_active else "")
        row.append(Text(detail_display, style="dim"))
        table.add_row(*row)

    elapsed = int(time.monotonic() - start_time)
    timer = f"{elapsed // 60}:{elapsed % 60:02d}"
    return Group(
        Text(""),
        table,
        Text(""),
        Text.from_markup(
            f"[dim]{timer}  {done_count}/{num_agents} agents complete[/]"),
        Text(""),
    )


def _save_result(result: SwarmResult) -> None:
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(
            RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else []
        existing.append(result.model_dump())
        RESULTS_JSON.write_text(json.dumps(existing, indent=2) + "\n")
    except (OSError, json.JSONDecodeError) as exc:
        _console.print(f"[dim]Warning: could not save results: {exc}[/]")


def _print_swarm_result(result: SwarmResult,
                        show_header: bool = False) -> None:
    """Render aggregated swarm results to the terminal."""
    rate_pct = f"{result.completion_rate:.0%}"
    moe_pct = f"±{result.margin_of_error:.0%}"

    if show_header:
        filename = Path(result.target).name
        _console.print()
        _console.print(f"[/bold][/underline]{filename}[/][/] {result.task}",
                       highlight=False)

    _console.print(
        f"{result.users} simulated users [dim]({moe_pct} margin of error)[/]",
        highlight=False)
    _console.print()

    completion_line = f"{rate_pct} of agents completed the task"
    if result.avg_steps_to_completion > 0:
        completion_line += f": Avg steps to completion: {result.avg_steps_to_completion:.1f}"
    _console.print(completion_line, highlight=False)

    if len(result.user_breakdown) > 1:
        _console.print()
        label_total: dict[str, int] = {}
        label_done: dict[str, int] = {}
        for r in result.individual_results:
            label_total[r.user_type] = label_total.get(r.user_type, 0) + 1
            if r.status == "completed":
                label_done[r.user_type] = label_done.get(r.user_type, 0) + 1
        col = max(len(l) for l in result.user_breakdown)
        for label, rate in result.user_breakdown.items():
            total = label_total.get(label, 0)
            done = label_done.get(label, 0)
            pct = f"{rate:.0%}"
            _console.print(
                f"  {label:<{col}}  {pct}  [dim]{done}/{total} users[/]",
                highlight=False,
            )

    if result.friction_points:
        agent_mentions: Counter[str] = Counter()
        for r in result.individual_results:
            for fp in set(fp for fp in r.friction_points if fp):
                agent_mentions[fp] += 1
        top = agent_mentions.most_common(5)
        _console.print()
        _console.print("Most common pain points:", highlight=False)
        _console.print()
        for point, count in top:
            prefix = f"{count}×  "
            max_point = _console.width - 2 - len(prefix) - 1
            display = point if len(point) <= max_point else point[:max_point -
                                                                   1] + "…"
            _console.print(f"  {prefix}{display}", highlight=False)

    results_link = RESULTS_JSON.resolve().as_uri()
    _console.print()
    _console.print(
        f"Report saved to [bold underline][link={results_link}]{RESULTS_JSON}[/link][/bold underline]",
        highlight=False)
    _console.print()
    if not show_header:
        _console.print("[dim]run `swarm expand` to see full results[/]",
                       highlight=False)
    _console.print()


def _make_agent_labels(user_types: list[UserType],
                       num_agents: int) -> dict[int, str]:
    assigned = distribute_users(user_types, num_agents)
    counts: Counter[str] = Counter()
    labels: dict[int, str] = {}
    for i, u in enumerate(assigned):
        counts[u.label] += 1
        a11y = " [blue]a11y[/]" if u.accessibility else ""
        labels[i] = f"{u.label} {counts[u.label]}{a11y}"
    return labels


def _run_screenshot(
    target: str,
    task: str,
    users: int | None,
    verbose: bool,
    model_full: str,
) -> None:
    num_agents = users or RUN_DEFAULTS["default_users"]
    max_concurrent = RUN_DEFAULTS["max_concurrent_screenshot"]
    user_types = load_users()

    agent_labels = _make_agent_labels(user_types, num_agents)

    _console.print()
    _console.print(f"[bold underline]{Path(target).name}[/bold underline] {task}", highlight=False)

    agent_states: dict[int, tuple[str, int, str]] = {}
    done_count = 0
    start_time = time.monotonic()

    try:
        with Live(
                _build_display(agent_labels,
                               agent_states,
                               0,
                               num_agents,
                               start_time,
                               max_steps=None),
                console=_console,
                refresh_per_second=10,
                transient=True,
        ) as live:

            def on_done(done: int, total: int,
                        agent_result: AgentResult | None) -> None:
                nonlocal done_count
                if agent_result is not None:
                    comment = agent_result.comment or agent_result.abandonment_reason or ""
                    agent_states[agent_result.agent_index] = (
                        "complete"
                        if agent_result.status == "completed" else "failed",
                        1,
                        comment,
                    )
                done_count = done
                live.update(
                    _build_display(agent_labels,
                                   agent_states,
                                   done_count,
                                   num_agents,
                                   start_time,
                                   max_steps=None))

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
        raise CliError(str(exc)) from exc

    _save_result(result)
    _print_swarm_result(result)


def _run_browser(
    url: str,
    task: str,
    users: int | None,
    max_steps: int | None,
    viewport: int | None,
    headed: bool,
    verbose: bool,
    model_full: str,
) -> None:
    _, chromium_ok = playwright_state()
    if not chromium_ok:
        raise CliError(
            "Chromium is not installed — run `swarm config` to install it.")

    num_agents = users or RUN_DEFAULTS["default_users"]
    steps = max_steps or RUN_DEFAULTS["max_steps"]
    vp = viewport or RUN_DEFAULTS["viewport_width"]
    max_concurrent = RUN_DEFAULTS["max_concurrent_browser"]
    user_types = load_users()

    agent_labels = _make_agent_labels(user_types, num_agents)

    _console.print()
    _console.print(
        f"[bold underline][link={url}]{url}[/link][/bold underline] {task}",
        highlight=False)

    agent_states: dict[int, tuple[str, int, str]] = {}
    done_count = 0
    start_time = time.monotonic()

    try:
        with Live(
                _build_display(agent_labels,
                               agent_states,
                               0,
                               num_agents,
                               start_time,
                               max_steps=steps),
                console=_console,
                refresh_per_second=4,
                transient=True,
        ) as live:

            def on_step(agent_id: int, status: str, detail: str,
                        step: int) -> None:
                agent_states[agent_id] = (status, step, detail)
                live.update(
                    _build_display(agent_labels,
                                   agent_states,
                                   done_count,
                                   num_agents,
                                   start_time,
                                   max_steps=steps))

            def on_agent_done(done: int, total: int,
                              agent_result: AgentResult | None) -> None:
                nonlocal done_count
                if agent_result is not None:
                    comment = agent_result.comment or agent_result.abandonment_reason or ""
                    agent_states[agent_result.agent_index] = (
                        "complete"
                        if agent_result.status == "completed" else "failed",
                        agent_result.steps_taken,
                        comment,
                    )
                done_count = done
                live.update(
                    _build_display(agent_labels,
                                   agent_states,
                                   done_count,
                                   num_agents,
                                   start_time,
                                   max_steps=steps))

            result = asyncio.run(
                run_browser_swarm(
                    url=url,
                    task=task,
                    users=user_types,
                    num_agents=num_agents,
                    model=model_full,
                    max_concurrent=max_concurrent,
                    max_steps=steps,
                    viewport=vp,
                    headed=headed,
                    on_agent_done=on_agent_done,
                    on_agent_step=on_step,
                ))
    except click.ClickException:
        raise
    except Exception as exc:
        if verbose:
            raise
        raise CliError(str(exc)) from exc

    _save_result(result)
    _print_swarm_result(result)


@cli.command(cls=SwarmCommand,
             short_help="Run a swarm against a URL or screenshot")
@click.argument("target")
@click.argument("task")
@click.option(
    "-u",
    "--users",
    default=None,
    type=int,
    help=f"Number of simulated users [default: {RUN_DEFAULTS['default_users']}]"
)
@click.option(
    "-m",
    "--max-steps",
    default=None,
    type=int,
    help=
    f"Max steps per agent (browser only) [default: {RUN_DEFAULTS['max_steps']}]"
)
@click.option(
    "-w",
    "--width",
    default=None,
    type=int,
    help=
    f"Viewport width in pixels (browser only) [default: {RUN_DEFAULTS['viewport_width']}]"
)
@click.option("-b",
              "--browser",
              is_flag=True,
              help="Show the browser window during the run")
@click.option("-v",
              "--verbose",
              is_flag=True,
              help="Show full tracebacks on error")
@click.pass_context
def run(ctx, target, task, users, max_steps, width, browser, verbose):
    """Run a swarm of synthetic users against a URL or screenshot.

    \b
    <target>  url or domain to open in a browser, or a path to a screenshot image
    <task>    plain-language goal for every agent

    \b
    Examples:
      swarm example.com find the pricing page
      swarm screenshot.png check out --users 5
      swarm https://app.example.com sign up -u 10 -m 12 -w 1440
    """
    is_url, target = _resolve_target(target)
    if not is_url and not Path(target).exists():
        raise CliError(f"Image not found: {target}")

    config = load_config()
    model_full = config.get("model", "")
    api_key = config.get("api_key", "")
    provider = config.get("provider", "")

    if not model_full:
        raise CliError("No model configured — run `swarm config` to set one.")
    if not api_key:
        raise CliError(
            "No API key configured — run `swarm config` to set one.")

    _inject_api_key(provider, api_key)

    if is_url:
        _run_browser(target, task, users, max_steps, width, browser, verbose,
                     model_full)
    else:
        _run_screenshot(target, task, users, verbose, model_full)


@cli.command(cls=SwarmCommand,
             short_help="Manage user personas",
             fields_section="users.json fields")
@click.option("-e",
              "--edit",
              "write_config",
              is_flag=True,
              help="Write .swarm/users.json to disk for manual editing")
def users(write_config):
    """List and manage the synthetic user personas agents simulate during a run

\b
"label":         Display name shown for this user type during a run.
"weight":        Relative sampling weight — higher means more agents of this type.
"description":   Behavioral prompt guiding each agent's decisions and style.
"accessibility":  Optional. Set to "screen_reader" to enable screen reader navigation.
"""
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

    link = USERS_JSON.resolve().as_uri()
    _console.print()
    _console.print(
        "Personas agents assume during a run. Weight sets each type's share of the swarm.",
        highlight=False,
    )
    _console.print(
        f"[dim]Navigate to [bold][link={link}][underline].swarm/users.json[/underline][/link][/bold] to edit users.[/]\n",
        highlight=False,
    )

    label_plain = [
        f"{u.label} a11y" if u.accessibility else u.label for u in active
    ]
    label_markup = [
        f"{u.label} [blue]a11y[/blue]" if u.accessibility else u.label
        for u in active
    ]
    weights = [f"{u.weight / total_weight:.0%}" for u in active]

    col_w0 = max(len("User"), max(len(l) for l in label_plain))
    col_w1 = max(len("Weight"), max(len(w) for w in weights))
    # -6: two 2-space separators + 2-char safety buffer; -1 reserves space for the "…" glyph
    col_w2 = max(20, _console.width - col_w0 - col_w1 - 6)

    descs = [
        u.description[:col_w2 - 1] +
        ("…" if len(u.description) > col_w2 - 1 else "") for u in active
    ]

    _console.print(f"{'User':<{col_w0}}  {'Weight':<{col_w1}}  Description",
                   highlight=False)
    _console.print(f"{'─' * col_w0}  {'─' * col_w1}  {'─' * col_w2}",
                   highlight=False)
    for i, u in enumerate(active):
        pad = " " * (col_w0 - len(label_plain[i]))
        _console.print(
            f"{label_markup[i]}{pad}  {weights[i].ljust(col_w1)}  {descs[i]}",
            highlight=False)
    _console.print()


@cli.command(cls=SwarmCommand,
             short_help="View saved results",
             fields_section="results.json fields")
@click.option("-n",
              "--number",
              default=None,
              type=int,
              help="Show only the last n results")
def results(number):
    """View saved swarm results from .swarm/results.json

\b
"timestamp":        ISO 8601 datetime when the run completed.
"mode":             Run mode: "screenshot" or "browser".
"target":           File path or URL that was tested.
"task":             The goal given to agents.
"model":            LLM model used for the run.
"users":            Total number of agents in the swarm.
"completion_rate":  Fraction of agents that completed the task (0–1).
"margin_of_error":  Statistical margin of error for the completion rate.
"user_breakdown":   Per-user-type completion rates as a JSON object.
"friction_points":  Repeated pain points mentioned by multiple agents.
"total_cost":       Total LLM API cost in USD for the run.
"""
    if not RESULTS_JSON.exists():
        _console.print(
            "[dim]No results yet. Run `swarm <target> <task>` to get started.[/]"
        )
        return

    try:
        entries = json.loads(RESULTS_JSON.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"Could not read results.json: {exc}") from exc

    if not entries:
        _console.print("[dim]No results yet.[/]")
        return

    entries = list(reversed(entries))
    if number:
        entries = entries[:number]

    rows = []
    for entry in entries:
        r = SwarmResult.model_validate(entry)
        rows.append((
            Path(r.target).name,
            r.task,
            f"{r.completion_rate:.0%}",
            r.timestamp[:10],
        ))

    link = RESULTS_JSON.resolve().as_uri()
    _console.print()
    _console.print(
        "A log of every completed swarm run.",
        highlight=False,
    )
    _console.print(
        f"[dim]Navigate to [bold][link={link}][underline].swarm/results.json[/underline][/link][/bold] to view raw results.[/]\n",
        highlight=False,
    )

    headers = ["Target", "Task", "Completion", "Date"]
    col_w = [
        max(len(h), max(len(row[i]) for row in rows))
        for i, h in enumerate(headers)
    ]
    sep = "  ".join("─" * w for w in col_w)
    header_line = "  ".join(h.ljust(col_w[i]) for i, h in enumerate(headers))

    _console.print(header_line, highlight=False)
    _console.print(sep, highlight=False)
    for filename, task, rate, date in rows:
        _console.print(
            f"{filename.ljust(col_w[0])}  {task.ljust(col_w[1])}  {rate.ljust(col_w[2])}  {date}",
            highlight=False,
        )
    _console.print()


@cli.command(cls=SwarmCommand,
             short_help="View per-agent details from the last run")
def expand():
    """View a full per-agent breakdown of the most recent swarm result"""
    if not RESULTS_JSON.exists():
        _console.print("[dim]No results yet.[/]")
        return

    try:
        entries = json.loads(RESULTS_JSON.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise CliError(f"Could not read results.json: {exc}") from exc

    if not entries:
        _console.print("[dim]No results yet.[/]")
        return

    result = SwarmResult.model_validate(entries[-1])
    filename = Path(result.target).name

    _console.print()
    _console.print(f"[bold underline]{filename}[/bold underline] {result.task}", highlight=False)
    _console.print()

    w = _console.width
    users_by_label = {u.label: u for u in load_users()}
    label_counts: Counter[str] = Counter()
    for r in result.individual_results:
        label_counts[r.user_type] += 1
        numbered = f"{r.user_type} {label_counts[r.user_type]}"
        u = users_by_label.get(r.user_type)
        a11y = " [blue]a11y[/]" if u and u.accessibility else ""
        status_str = "[green]Completed[/]" if r.status == "completed" else "[red]Failed[/]"
        comment = ("User ran out of steps. " + r.comment if r.status == "timeout" and r.comment else
                   "User ran out of steps." if r.status == "timeout" else
                   r.comment or "")
        _console.print(f"{numbered}{a11y} {status_str}", highlight=False)
        bullets = (([comment] if comment else []) +
                   ([r.abandonment_reason] if r.abandonment_reason else []) +
                   r.friction_points)
        for bullet in bullets:
            bullet_lines = textwrap.wrap(bullet, width=w - 6)
            for i, line in enumerate(bullet_lines):
                prefix = "[dim]· " if i == 0 else "[dim]  "
                _console.print(f"{prefix}{line}[/]", highlight=False)
        _console.print()

    if result.friction_points:
        _console.print("Pain points:", highlight=False)
        _console.print()
        for point, count in Counter(fp for fp in result.friction_points
                                    if fp).most_common():
            _console.print(f"  {count}×  {point}", highlight=False)
        _console.print()


if __name__ == "__main__":
    cli()
