import sys

import click
from rich.console import Console

console = Console()


class GoBack(Exception):
    """Raised by select() when the user presses Escape to go back."""


def navigate(key: str, selected_index: int, max_index: int) -> int:
    """Return the new index after an up/down arrow keypress, clamped to [0, max_index]."""
    if key in ("\x1b[A", "\x1bOA"):
        return max(0, selected_index - 1)
    if key in ("\x1b[B", "\x1bOB"):
        return min(max_index, selected_index + 1)
    return selected_index


def select(label: str,
           options: list[str],
           default_index: int = 0,
           echo: bool = True) -> str:
    """Display an arrow-key menu, redrawing in-place. Collapses to a single line on commit."""
    selected_index = default_index

    def _draw() -> int:
        lines = [
            f"[bold]{label}[/]",
            *[
                f"{'[blue]> [/]' if i == selected_index else '  '} {'[blue]' + opt + '[/]' if i == selected_index else opt}"
                for i, opt in enumerate(options)
            ],
            "",
            "[dim]Use arrow keys. Return to submit[/]",
        ]
        for line in lines:
            console.print(line, highlight=False)
        return len(lines)

    lines_drawn = _draw()

    while True:
        key = click.getchar()

        if key in ("\r", "\n"):
            sys.stdout.write(f"\x1b[{lines_drawn}A\x1b[J")
            sys.stdout.flush()
            if echo:
                console.print(
                    f"[bold]{label}[/]: [dim]{options[selected_index]}[/]",
                    highlight=False)
            return options[selected_index]

        if key == "\x1b":
            sys.stdout.write(f"\x1b[{lines_drawn}A\x1b[J")
            sys.stdout.flush()
            console.print(f"{label}: [dim]← back[/]\n")
            raise GoBack

        if key == "\x03":
            sys.stdout.write(f"\x1b[{lines_drawn}A\x1b[J")
            sys.stdout.flush()
            console.print("\n[dim]interrupted[/]")
            raise SystemExit(130)

        selected_index = navigate(key, selected_index, len(options) - 1)
        sys.stdout.write(f"\x1b[{lines_drawn}A\x1b[J")
        sys.stdout.flush()
        lines_drawn = _draw()
