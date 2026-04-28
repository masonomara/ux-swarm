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

    def get_help(self, ctx):
        # Click's get_help() strips all trailing newlines; restore one so the
        # blank line after the last command is visible in the terminal.
        return super().get_help(ctx) + "\n"

    def _command_rows(self, ctx, formatter):
        rows = []
        for subcommand in self.list_commands(ctx):
            cmd = self.commands.get(subcommand)
            if cmd is None or cmd.hidden:
                continue
            parts = [subcommand]
            has_options = any(
                isinstance(p, click.Option) and not (p.is_eager and not p.expose_value)
                for p in cmd.params
            )
            if has_options:
                parts.append("[options]")
            # run's positional args are documented in the usage line; skip them here
            if subcommand != "run":
                for p in cmd.params:
                    if isinstance(p, click.Argument):
                        name = (p.name or "arg").lower().replace("_", "-")
                        parts.append(name if p.required else f"[{name}]")
            rows.append((" ".join(parts), cmd.get_short_help_str(limit=formatter.width)))
        return rows

    def format_help(self, ctx, formatter):
        prog = ctx.command_path
        usage_pairs = [
            (f"{prog} <target> <task> [options]", "shortcut to run swarm immediately"),
            (f"{prog} [options] [command]",        "properly use all options and commands"),
        ]

        opt_rows = [
            rv for p in self.get_params(ctx)
            if (rv := p.get_help_record(ctx)) is not None
        ]
        cmd_rows = self._command_rows(ctx, formatter)

        def _norm(s: str) -> str:
            s = s.rstrip(".")
            return s[:1].lower() + s[1:] if s else s

        opt_rows = [(k, "display help for command" if "--help" in k else _norm(v)) for k, v in opt_rows]
        cmd_rows = [(k, _norm(v)) for k, v in cmd_rows]

        # Single column width across Usage, Options, and Commands so all
        # descriptions align at the same position.
        all_keys = [r[0] for r in usage_pairs] + [r[0] for r in opt_rows] + [r[0] for r in cmd_rows]
        col = max(len(k) for k in all_keys) if all_keys else 30

        formatter.write("\n")
        formatter.write("Usage:\n")
        for key, desc in usage_pairs:
            formatter.write(f"  {key:<{col}}  {desc}\n")

        if opt_rows:
            with formatter.section("Options"):
                formatter.write_dl([(k.ljust(col), v) for k, v in opt_rows], col_max=col)

        if cmd_rows:
            with formatter.section("Commands"):
                formatter.write_dl([(k.ljust(col), v) for k, v in cmd_rows], col_max=col)
            formatter.write("\n")

        self.format_epilog(ctx, formatter)

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
