import time
import textwrap
from collections import Counter
from importlib.metadata import metadata, PackageNotFoundError

from rich.console import Group
from rich.table import Table
from rich.text import Text

from ux_swarm.config import PROVIDERS, RESULTS_JSON, console as _console, load_config, playwright_state
from ux_swarm.models import AgentResult, SwarmResult
from ux_swarm.users import distribute_users

try:
    __version__ = metadata("ux-swarm")["Version"]
except PackageNotFoundError:
    __version__ = "unknown"

ASCII_ART = ("\n[cyan]  ▄       ▄      ▄▄▄▄▄    ▄ ▄   ██   █▄▄▄▄ █▀▄▀█ \n"
             "   █  ▀▄   █ ▄▄ █     ▀▄ █   █  █ █  █  ▄▀ █ █ █ \n"
             "█   █   █ ▀      ▀▀▀▀▄  █ ▄   █ █▄▄█ █▀▀▌  █ ▄ █ \n"
             "█   █  ▄ █     ▀▄▄▄▄▀   █  █  █ █  █ █  █  █   █ \n"
             "█▄ ▄█ █   ▀▄             █ █ █     █   █      █  \n"
             " ▀▀▀   ▀                  ▀ ▀     █   ▀      ▀   \n"
             f"[/]v{__version__} - genesis                  [cyan]▀[/]\n")

_STATUS_COLORS = {
    "waiting": "dim",
    "running": "blue",
    "complete": "green",
    "failed": "red",
}


def _print_config_status() -> None:
    config = load_config()
    _, chromium_ok = playwright_state()

    provider_key = config.get("provider")
    provider_name = (next(
        (p["name"] for p in PROVIDERS if p["key"] == provider_key),
        provider_key) if provider_key else "Needs configuration")
    model_display = config.get("model", "").split("/", 1)[-1]
    model_label = model_display if model_display else "Needs configuration"
    playwright_label = "Enabled" if chromium_ok else "Needs configuration"

    pw_dot = "[green]•[/]" if chromium_ok else "[red]•[/]"
    provider_dot = "[green]•[/]" if provider_key else "[red]•[/]"
    model_dot = "[green]•[/]" if model_display else "[red]•[/]"

    _console.print(f"{provider_dot} LLM Provider: {provider_name}",
                   highlight=False)
    _console.print(f"{model_dot} Model: {model_label}", highlight=False)
    _console.print(f"{pw_dot} Playwright: {playwright_label}\n",
                   highlight=False)


def _print_header() -> None:
    _console.print(ASCII_ART, highlight=False)
    _console.print(
        "Simulate a swarm of synthetic users analyzing a screenshot or\n"
        "browsing a url to complete a task, then aggregate UX findings.\n",
        highlight=False)
    _print_config_status()


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

    max_detail = max(_console.width - (44 if max_steps is not None else 32),
                     10)
    for agent_id, label in sorted(agent_labels.items()):
        status, step, detail = agent_states.get(agent_id, ("waiting", 0, ""))
        color = _STATUS_COLORS.get(status, "white")
        detail_display = detail if len(
            detail) <= max_detail else detail[:max_detail - 1] + "…"
        row: list = [Text.from_markup(label), Text(status, style=color)]
        if max_steps is not None:
            row.append(f"step {step}/{max_steps}" if status ==
                       "running" else "")
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


def _make_agent_labels(user_types: list, num_agents: int) -> dict[int, str]:
    assigned = distribute_users(user_types, num_agents)
    counts: Counter[str] = Counter()
    labels: dict[int, str] = {}
    for i, u in enumerate(assigned):
        counts[u.label] += 1
        a11y = " [blue]a11y[/]" if u.accessibility else ""
        labels[i] = f"{u.label} {counts[u.label]}{a11y}"
    return labels


def _print_swarm_result(result: SwarmResult,
                        show_header: bool = False) -> None:
    rate_pct = f"{result.completion_rate:.0%}"
    moe_pct = f"±{result.margin_of_error:.0%}"

    if show_header:
        from pathlib import Path
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
            _console.print(
                f"  {label:<{col}}  {rate:.0%}  [dim]{done}/{total} users[/]",
                highlight=False,
            )

    if result.friction_points:
        _console.print()
        _console.print("Most common pain points:", highlight=False)
        _console.print()
        pairs = list(zip(result.friction_points, result.friction_counts or []))
        for point, count in pairs[:5]:
            if point:
                prefix = f"{count}×  "
                max_point = _console.width - 2 - len(prefix) - 1
                display = point if len(point) <= max_point else point[:max_point - 1] + "…"
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


def _expand_result(result: SwarmResult) -> None:
    from pathlib import Path
    filename = Path(result.target).name

    _console.print()
    _console.print(
        f"[bold underline]{filename}[/bold underline] {result.task}",
        highlight=False)
    _console.print()

    w = _console.width
    from ux_swarm.users import load_users
    users_by_label = {u.label: u for u in load_users()}
    label_counts: Counter[str] = Counter()
    for r in result.individual_results:
        label_counts[r.user_type] += 1
        numbered = f"{r.user_type} {label_counts[r.user_type]}"
        u = users_by_label.get(r.user_type)
        a11y = " [blue]a11y[/]" if u and u.accessibility else ""
        status_str = "[green]Completed[/]" if r.status == "completed" else "[red]Failed[/]"
        comment = (
            f"User ran out of steps. {r.comment}"
            if r.status == "timeout" and r.comment else
            "User ran out of steps." if r.status == "timeout" else r.comment)
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
        pairs = list(zip(result.friction_points, result.friction_counts or []))
        for point, count in pairs:
            if point:
                _console.print(f"  {count}×  {point}", highlight=False)
        _console.print()
