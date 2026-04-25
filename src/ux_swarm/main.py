import click
from importlib.metadata import metadata, PackageNotFoundError

from ux_swarm.cli import SmartGroup

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


# TODO: find a permanent home for these once the run command is built out
RUN_DEFAULTS: dict[str, int | float] = {
    "default_users": 20,
    "max_steps": 3,
    "viewport_width": 1280,
    "max_concurrent_browser": 5,
    "max_concurrent_screenshot": 20,
}


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
