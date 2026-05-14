import click


class CliError(click.ClickException):

    def show(self, file=None) -> None:
        click.echo(click.style(f"Error: {self.message}", fg="red"))
        click.echo()
        click.echo()
