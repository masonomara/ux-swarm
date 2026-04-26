import click
from datetime import datetime, timezone
from importlib.metadata import metadata, PackageNotFoundError
from pathlib import Path
from rich.console import Console

from ux_swarm.agent import run_screenshot_agent
from ux_swarm.cli import SmartGroup
from ux_swarm.config import GLOBAL_CONFIG, LOCAL_CONFIG, LOCAL_DIR, PROVIDERS, playwright_state, load_config, run_config_wizard
from ux_swarm.menu import select
from ux_swarm.models import AgentResult, UserType

try:
    _meta = metadata("ux-swarm")
    __version__ = _meta["Version"]
    __description__ = _meta["Summary"]
except PackageNotFoundError:
    __version__ = "unknown"
    __description__ = ""

_console = Console()

ASCII_ART = ("  __  ___  __    _____      _____   ___  __  ___\n"
             " / / / / |/_/___/ __/ | /| / / _ | / _ \\/  |/  /\n"
             "/ /_/ />  </___/\\ \\ | |/ |/ / __ |/ , _/ /|_/ /\n"
             "\\____/_/|_|   /___/ |__/|__/_/ |_/_/|_/_/  /_/")


def _print_header() -> None:
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
    _print_header()
    _console.print("Usage:\n")
    _console.print("  swarm <target> <task>\n", highlight=False)
    _console.print("Commands:\n")
    _console.print("  config   Run setup wizard")
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
RUN_DEFAULTS: dict[str, int | float] = {
    "default_users": 20,
    "max_steps": 3,
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 20,
}


# TODO: find a permanent home for this once a storage/reports layer exists
def ensure_swarm_structure() -> None:
    (LOCAL_DIR / "reports").mkdir(parents=True, exist_ok=True)


def _print_result(
    target: str,
    task: str,
    model_id: str,
    result: AgentResult,
) -> None:
    filename = Path(target).name

    _console.print()
    _console.rule(style="dim")
    _console.print()
    _console.print(f"  {filename} — \"{task}\"", highlight=False)
    _console.print()
    _console.print(f"  [bold]\"{result.comment}\"[/]", highlight=False)
    _console.print()
    _console.print(f"  [dim]Target[/]   {result.target_element}",
                   highlight=False)
    _console.print(f"  [dim]Reason[/]   {result.reasoning}", highlight=False)

    if result.friction_points:
        _console.print()
        _console.print("  Friction", highlight=False)
        for point in result.friction_points:
            _console.print(f"  [dim]•[/] {point}", highlight=False)

    _console.print()
    completed = "Yes" if result.completed else "[dim]No[/]"
    abandoned = "Yes" if result.abandoned else "[dim]No[/]"
    _console.print(f"  Completed {completed}   ·   Abandoned {abandoned}",
                   highlight=False)

    if result.abandoned and result.abandonment_reason:
        _console.print(f"  [dim]Reason[/]   {result.abandonment_reason}",
                       highlight=False)

    _console.print()
    _console.print(
        f"  [dim]{model_id}  ·  {result.input_tokens} in / {result.output_tokens} out tokens[/]",
        highlight=False)
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

    config = load_config()
    model_full = config.get("model", "")
    api_key = config.get("api_key", "")

    if not model_full:
        raise click.ClickException(
            "No model configured — run `swarm config` to set one.")
    if not api_key:
        raise click.ClickException(
            "No API key configured — run `swarm config` to set one.")

    provider, model_id = model_full.split("/", 1)

    # TODO: move to personas.py once multiple user types exist
    user_type = UserType(
        label="Default User",
        weight=1.0,
        description=
        ("You scan rather than read. You satisfice — you pick the first option that "
         "seems good enough rather than evaluating everything. You muddle through: you "
         "rarely read instructions and rely on guessing what things do. You have low "
         "tolerance for friction and give up quickly when confused."),
    )

    filename = Path(target).name
    try:
        with _console.status(f"  {filename} — \"{task}\""):
            decision, in_tok, out_tok = run_screenshot_agent(
                target, task, user_type, provider, model_id, api_key)
    except Exception as exc:
        if verbose:
            raise
        raise click.ClickException(str(exc)) from exc

    result = AgentResult(
        agent_index=0,
        user_type=user_type.label,
        completed=decision.completed,
        abandoned=decision.abandoned,
        abandonment_reason=decision.abandonment_reason,
        friction_points=decision.friction_observed,
        comment=decision.comment,
        target_element=decision.target_element,
        reasoning=decision.reasoning,
        steps_taken=1,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cost=0.0,
    )

    ensure_swarm_structure()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = LOCAL_DIR / "reports" / f"{timestamp}_screenshot.json"
    report_path.write_text(result.model_dump_json(indent=2) + "\n")

    _print_result(target, task, model_id, result)


if __name__ == "__main__":
    cli()
