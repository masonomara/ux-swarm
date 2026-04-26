import click
from importlib.metadata import metadata, PackageNotFoundError
from rich.console import Console

from ux_swarm.cli import SmartGroup
from ux_swarm.config import GLOBAL_CONFIG, LOCAL_CONFIG, PROVIDERS, check_chromium_installed, load_config, run_config_wizard
from ux_swarm.menu import select

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


def _print_home() -> None:
    _console.print("\n" + ASCII_ART, highlight=False)

    _console.print(f"\nux-swarm - v{__version__}\n", highlight=False)

    _console.print(
        "Synthetic user testing. Simulates a swarm of users who intereact with your target URL or screenshot to complete a specific task.\n"
    )

    config = load_config()

    try:
        playwright_ok = check_chromium_installed()
    except Exception:
        playwright_ok = False
    playwright_label = "Enabled" if playwright_ok else "Needs configuration"

    provider_key = config.get("provider")
    provider_name = (next(
        (p["name"] for p in PROVIDERS if p["key"] == provider_key),
        provider_key) if provider_key else "[dim]Not configured[/]")

    raw_model = config.get("model", "")
    model_display = raw_model.split("/",
                                    1)[-1] if "/" in raw_model else raw_model
    model_label = model_display if model_display else "Needs configuration"

    pw_dot = "[green]•[/]" if playwright_ok else "[red]•[/]"
    provider_dot = "[green]•[/]" if provider_key else "[red]•[/]"
    model_dot = "[green]•[/]" if model_display else "[red]•[/]"

    _console.print(f"{pw_dot} Playwright: {playwright_label}", highlight=False)
    _console.print(f"{provider_dot} LLM Provider: {provider_name}",
                   highlight=False)
    _console.print(f"{model_dot} Model: {model_label}\n", highlight=False)

    _console.print("---\n")
    _console.print("Usage:\n")
    _console.print("  swarm <target> <task>\n", highlight=False)
    _console.print("Commands:\n")
    _console.print(
        "  config   Run setup wizard")
    _console.print("  help     View all commands")
    _console.print("\n---\n")


@click.group(cls=SmartGroup, invoke_without_command=True, help=__description__)
@click.version_option(version=__version__, message="ux-swarm v%(version)s")
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        _print_home()
        if not (LOCAL_CONFIG.exists() or GLOBAL_CONFIG.exists()):
            if select("Get started?", ["Yes", "No"]) == "Yes":
                run_config_wizard()


@cli.command()
def config():
    """Run the setup wizard: provider, API key, model, Chromium."""
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


# TODO: move alongside run command output logic once that is built out
def ensure_swarm_structure() -> None:
    from ux_swarm.config import LOCAL_DIR
    (LOCAL_DIR / "reports").mkdir(parents=True, exist_ok=True)


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
    pass


if __name__ == "__main__":
    cli()
