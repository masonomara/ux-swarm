import asyncio
import inspect
import json
import os
import re
import sys
import time
from pathlib import Path

import click
from rich.live import Live

from ux_swarm.config import (
    GLOBAL_CONFIG,
    LOCAL_CONFIG,
    LOCAL_DIR,
    PROVIDERS,
    RESULTS_JSON,
    console as _console,
    load_config,
    playwright_state,
)
from ux_swarm.display import (
    __version__,
    _build_display,
    _expand_result,
    _make_agent_labels,
    _print_config_status,
    _print_header,
    _print_swarm_result,
)
from ux_swarm.errors import CliError
from ux_swarm.menu import GoBack, select
from ux_swarm.models import AgentResult, SwarmResult
import ux_swarm.users as _users_mod
from ux_swarm.users import load_users, write_default_users
from ux_swarm.swarm import run_browser_swarm, run_screenshot_swarm
from ux_swarm.wizard import run_config_wizard

# ── Help formatting ───────────────────────────────────────────────────────────

_UPPER_WORD_RE = re.compile(r'\b[A-Z][A-Z0-9_]+\b')
_LONG_OPT_RE = re.compile(r'--([a-z][-a-z]*)')
_COL_MIN = 31

_BARE_URL_RE = re.compile(
    r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?::\d+)?(?:/.*)?$'
    r'|^localhost(?::\d+)?(?:/.*)?$'
    r'|^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/.*)?$')


def _opt_sort_key(row: tuple[str, str]) -> str:
    m = _LONG_OPT_RE.search(row[0])
    return m.group(1) if m else row[0]


def _parse_arg_rows(block: str) -> list[tuple[str, str]]:
    rows = []
    current_name: str | None = None
    current_parts: list[str] = []
    for line in block.splitlines():
        if line.strip() in ("\b", ""):
            continue
        m = re.match(r'^(<\S+>|"[^"]+":)\s{2,}(.*)', line)
        if m:
            if current_name:
                rows.append((current_name, " ".join(current_parts)))
            current_name = m.group(1)
            current_parts = [m.group(2).strip()]
        elif current_name and line[:1] in (" ", "\t"):
            current_parts.append(line.strip())
    if current_name:
        rows.append((current_name, " ".join(current_parts)))
    return rows


def _fmt_meta(s: str) -> str:
    return _UPPER_WORD_RE.sub(lambda m: f"<{m.group().lower()}>", s)


class SwarmCommand(click.Command):

    def __init__(self, *args, fields_section: str = "Fields", **kwargs):
        self.fields_section = fields_section
        super().__init__(*args, **kwargs)

    def _real_opts(self, ctx):
        return [
            rv for param in self.get_params(ctx)
            if (rv := param.get_help_record(ctx)) is not None
            and "--help" not in rv[0]
        ]

    def format_help(self, ctx, formatter):
        pieces = self.collect_usage_pieces(ctx)
        opts = sorted(self._real_opts(ctx), key=_opt_sort_key)
        pieces = [p for p in pieces if p != "[OPTIONS]"]
        if opts:
            pieces.append("[options]")
        pieces = [_fmt_meta(p) for p in pieces]
        usage_line = f"{ctx.command_path} {' '.join(pieces)}".strip()

        desc = ""
        field_rows: list[tuple[str, str]] = []
        if self.help:
            sections = inspect.cleandoc(self.help).split("\n\n")
            desc = sections[0].replace("\n", " ").strip()
            if len(sections) > 1:
                field_rows = _parse_arg_rows(sections[1])

        col = max([len(usage_line)] + [len(_fmt_meta(k)) for k, _ in opts] +
                  [len(k) for k, _ in field_rows] + [_COL_MIN])

        formatter.write("\n")
        formatter.write(f"Usage: {usage_line}\n")
        if desc:
            formatter.write(f"\n{desc} \n")

        if opts:
            with formatter.section("Options"):
                formatter.write_dl([(_fmt_meta(k).ljust(col), v)
                                    for k, v in opts],
                                   col_max=col)

        if field_rows:
            with formatter.section(self.fields_section):
                formatter.write_dl([(k.ljust(col), v) for k, v in field_rows],
                                   col_max=col)

    def get_help(self, ctx):
        return super().get_help(ctx) + "\n"


# routes bare URLs/image paths to `run` without requiring 'run' explicitly
class SmartGroup(click.Group):

    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    def parse_args(self, ctx, args):
        if args and not args[0].startswith(
                "-") and args[0] not in self.commands:
            reconstructed = self._try_reconstruct(args)
            if reconstructed:
                args = reconstructed
        return super().parse_args(ctx, args)

    def _try_reconstruct(self, args: list[str]) -> list[str] | None:
        if args[0].startswith(
            ("http://", "https://")) or _BARE_URL_RE.match(args[0]):
            return self._build_run_args(args[0], args[1:])

        for i, arg in enumerate(args):
            if any(arg.lower().endswith(ext)
                   for ext in self._IMAGE_EXTENSIONS):
                target = " ".join(args[:i + 1])
                return self._build_run_args(target, args[i + 1:])

        return None

    def get_help(self, ctx):
        return super().get_help(ctx) + "\n"

    def _command_rows(self, ctx, formatter):
        rows = []
        for subcommand in self.list_commands(ctx):
            cmd = self.commands.get(subcommand)
            if cmd is None or cmd.hidden or subcommand == "run":
                continue
            parts = [subcommand]
            has_options = any(
                isinstance(p, click.Option)
                and not (p.is_eager and not p.expose_value)
                for p in cmd.params)
            if has_options:
                parts.append("[options]")
            for p in cmd.params:
                if isinstance(p, click.Argument):
                    name = (p.name or "arg").lower().replace("_", "-")
                    parts.append(name if p.required else f"[{name}]")
            rows.append((" ".join(parts),
                         cmd.get_short_help_str(limit=formatter.width)))
        return rows

    def format_help(self, ctx, formatter):
        prog = ctx.command_path
        usage_pairs = [
            (f"{prog} <target> <task> [options]",
             "Shortcut to run swarm immediately"),
            (f"{prog} [options] [command]",
             "Properly use all options and commands"),
        ]

        def _norm(s: str) -> str:
            return s.rstrip(".")

        run_cmd = self.commands.get("run")
        run_opt_rows = []
        if run_cmd:
            with click.Context(run_cmd, parent=ctx,
                               info_name="run") as run_ctx:
                run_opt_rows = [
                    (_fmt_meta(key), _norm(val))
                    for param in run_cmd.get_params(run_ctx)
                    if (rv := param.get_help_record(run_ctx)) is not None
                    for key, val in [rv] if "--help" not in key
                ]

        group_opt_rows = [
            (k, "Display help for this command" if "--help" in k else _norm(v))
            for p in self.get_params(ctx)
            if (rv := p.get_help_record(ctx)) is not None for k, v in [rv]
        ]

        opt_rows = sorted(run_opt_rows + group_opt_rows, key=_opt_sort_key)
        cmd_rows = [(k, _norm(v))
                    for k, v in self._command_rows(ctx, formatter)]

        arg_rows: list[tuple[str, str]] = []
        example_sections: list[str] = []
        if run_cmd and run_cmd.help:
            sections = inspect.cleandoc(run_cmd.help).split("\n\n")
            if len(sections) > 1:
                arg_rows = _parse_arg_rows(sections[1])
            example_sections = sections[2:]

        col = max(
            (len(k) for k, _ in usage_pairs + opt_rows + cmd_rows + arg_rows),
            default=20,
        )

        formatter.write("\n")
        formatter.write("Usage:\n")
        for key, desc in usage_pairs:
            formatter.write(f"  {key:<{col}}  {desc}\n")

        if arg_rows:
            formatter.write_paragraph()
            formatter.indent()
            formatter.write_dl([(k.ljust(col), v) for k, v in arg_rows],
                               col_max=col)
            formatter.dedent()

        for section in example_sections:
            formatter.write_paragraph()
            formatter.write_text(section)

        if opt_rows:
            with formatter.section("Options"):
                formatter.write_dl([(k.ljust(col), v) for k, v in opt_rows],
                                   col_max=col)

        if cmd_rows:
            with formatter.section("Commands"):
                formatter.write_dl([(k.ljust(col), v) for k, v in cmd_rows],
                                   col_max=col)
            formatter.write("\n")

        self.format_epilog(ctx, formatter)

    def _build_run_args(self, target: str,
                        rest: list[str]) -> list[str] | None:
        task_words: list[str] = []
        flags: list[str] = []
        j = 0
        while j < len(rest):
            if rest[j].startswith("-"):
                flags.append(rest[j])
                if j + 1 < len(rest) and not rest[j + 1].startswith("-"):
                    flags.append(rest[j + 1])
                    j += 2
                else:
                    j += 1
            else:
                task_words.append(rest[j])
                j += 1
        task = " ".join(task_words)
        return ["run", target, task] + flags if task else None


# ── Run defaults and helpers ──────────────────────────────────────────────────

RUN_DEFAULTS: dict[str, int] = {
    "default_users": 20,
    "max_steps": 8,
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 5,
}


def _resolve_target(target: str) -> tuple[bool, str]:
    if target.startswith(("http://", "https://")):
        return True, target
    if Path(target).exists():
        return False, target
    if _BARE_URL_RE.match(target):
        return True, "https://" + target
    return False, target


def _inject_api_key(provider: str, api_key: str) -> None:
    env_var = next((p["env"] for p in PROVIDERS if p["key"] == provider), None)
    if env_var and api_key:
        os.environ[env_var] = api_key


def _save_result(result: SwarmResult) -> None:
    RESULTS_JSON.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(
            RESULTS_JSON.read_text()) if RESULTS_JSON.exists() else []
        existing.append(result.model_dump())
        RESULTS_JSON.write_text(json.dumps(existing, indent=2) + "\n")
    except (OSError, json.JSONDecodeError) as exc:
        _console.print(f"[dim]Warning: could not save results: {exc}[/]")


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
    _console.print(
        f"[bold underline]{Path(target).name}[/bold underline] {task}",
        highlight=False)

    agent_states: dict[int, tuple[str, int, str]] = {}
    done_count = 0
    start_time = time.monotonic()

    try:
        with Live(
                _build_display(agent_labels, agent_states, 0, num_agents,
                               start_time),
                console=_console,
                refresh_per_second=10,
                transient=True,
        ) as live:

            def on_done(done: int, _total: int,
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

    class _LiveRenderable:

        def __rich__(self):
            return _build_display(agent_labels,
                                  agent_states,
                                  done_count,
                                  num_agents,
                                  start_time,
                                  max_steps=steps)

    try:
        with Live(
                _LiveRenderable(),
                console=_console,
                refresh_per_second=1,
                transient=True,
        ):

            def on_step(agent_id: int, status: str, detail: str,
                        step: int) -> None:
                agent_states[agent_id] = (status, step, detail)

            def on_agent_done(done: int, _total: int,
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


# ── Commands ──────────────────────────────────────────────────────────────────


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
    try:
        choice = select("[green]?[/] [bold]Run setup wizard:[/]",
                        ["Proceed", "Cancel"],
                        echo=False)
    except GoBack:
        return
    if choice == "Proceed":
        # Erase: heading + "Press Ctrl+C..." + blank + 3 bullet lines + blank = 7 lines
        sys.stdout.write("\x1b[7A\x1b[J")
        sys.stdout.flush()
        _console.print(
            "Configure your LLM provider, model, and browser\n[dim]Press Ctrl+C to cancel.[/]\n",
            highlight=False)
        run_config_wizard()


@cli.command(cls=SwarmCommand, short_help="Display help for a command")
@click.argument("command", required=False, metavar="[command]")
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
def run(target, task, users, max_steps, width, browser, verbose):
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
    if write_config:
        if _users_mod.USERS_JSON.exists():
            _console.print(f"[dim]{_users_mod.USERS_JSON} already exists.[/]")
        else:
            path = write_default_users()
            _console.print(f"[green]Written →[/] {path}")
            _console.print(
                "[dim]Edit the file to define user types and weights.[/]")
        return

    active = load_users()
    total_weight = sum(u.weight for u in active)

    link = _users_mod.USERS_JSON.resolve().as_uri()
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
    col_w2 = max(20, _console.width - col_w0 - col_w1 - 6)

    descs = [
        u.description[:col_w2 - 1] +
        ("…" if len(u.description) > col_w2 - 1 else "") for u in active
    ]

    _console.print(f"{'User':<{col_w0}}  {'Weight':<{col_w1}}  Description",
                   highlight=False)
    _console.print(f"{'─' * col_w0}  {'─' * col_w1}  {'─' * col_w2}",
                   highlight=False)
    for i, _ in enumerate(active):
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

    _expand_result(SwarmResult.model_validate(entries[-1]))


if __name__ == "__main__":
    cli()
