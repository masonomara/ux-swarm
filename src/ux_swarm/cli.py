import re

import click

# TODO: this file and/or lone SmartGroup may be renamed or merged elsewhere as the CLI grows


class CliError(click.ClickException):
    """ClickException styled with red text and trailing whitespace."""

    def show(self, file=None) -> None:
        click.echo(click.style(f"Error: {self.message}", fg="red"))
        click.echo()
        click.echo()


class SmartGroup(click.Group):
    """Routes bare URLs/image paths to `run` without requiring 'run' explicitly."""

    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    # Bare domain/IP without scheme: masonomara.com, sub.domain.co.uk, localhost:3000, 192.168.1.1:8080
    _BARE_DOMAIN_RE = re.compile(
        r'^(?:[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}(?::\d+)?(?:/.*)?$'
        r'|^localhost(?::\d+)?(?:/.*)?$'
        r'|^\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?(?:/.*)?$'
    )

    def parse_args(self, ctx, args):
        """Intercept args before Click sees them; prepend 'run' if the first arg looks like a URL or image path."""
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            reconstructed = self._try_reconstruct(args)
            if reconstructed:
                args = reconstructed
        return super().parse_args(ctx, args)

    def _try_reconstruct(self, args: list[str]) -> list[str] | None:
        """Return ['run', target, task, ...flags] if args start with a URL or image path, otherwise None."""
        # Full URL or bare domain: first token is target, rest is unquoted task + flags
        if args[0].startswith(("http://", "https://")) or self._BARE_DOMAIN_RE.match(args[0]):
            return self._build_run_args(args[0], args[1:])

        # Image path: may span multiple tokens if the filename has spaces.
        # Scan for the token ending with a known image extension.
        for i, arg in enumerate(args):
            if any(arg.lower().endswith(ext) for ext in self._IMAGE_EXTENSIONS):
                target = " ".join(args[:i + 1])
                return self._build_run_args(target, args[i + 1:])

        return None

    def _build_run_args(self, target: str, rest: list[str]) -> list[str] | None:
        """Split rest into task words and flags, return ['run', target, task, ...flags]."""
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
