import click


@click.command()
@click.argument("target")
@click.argument("task")
def cli(target, task):
    """Simulate a swarm of users completing TASK on TARGET.

    TARGET can be a URL or a path to a screenshot.
    TASK is a natural-language description of what users should complete.

    Example: ux-swarm https://myapp.com "complete the checkout flow"
    """


if __name__ == "__main__":
    cli()
