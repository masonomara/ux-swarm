import click
from importlib.metadata import metadata, PackageNotFoundError

try:
    _meta = metadata("ux-swarm")
    __version__ = _meta["Version"]
    __description__ = _meta["Summary"]
except PackageNotFoundError:
    __version__ = "unknown"
    __description__ = ""


class SmartGroup(click.Group):
    """Routes bare URLs/image paths to `run` without requiring 'run' explicitly."""

    def parse_args(self, ctx, args):
        if args and self._looks_like_target(args[0]):
            args = ["run"] + args
        return super().parse_args(ctx, args)

    @staticmethod
    def _looks_like_target(arg):
        extensions = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        return (
            arg.startswith(("http://", "https://"))
            or any(arg.endswith(ext) for ext in extensions)
        )


@click.group(cls=SmartGroup, invoke_without_command=True, help=__description__)
@click.version_option(version=__version__, message="ux-swarm v%(version)s")
@click.pass_context
def cli(ctx):
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@cli.command(hidden=True)
@click.argument("url")
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
def run(url, task, users, max_steps, viewport, verbose):
    """Run a swarm of simulated users against a URL or screenshot image."""
    pass


if __name__ == "__main__":
    cli()
