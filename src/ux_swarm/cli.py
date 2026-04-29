import inspect
import re

import click

# TODO: this file and/or lone SmartGroup may be renamed or merged elsewhere as the CLI grows


_UPPER_WORD_RE = re.compile(r'\b[A-Z][A-Z0-9_]+\b')
_LONG_OPT_RE = re.compile(r'--([a-z][-a-z]*)')
# Minimum column width — matches main help's widest key: "swarm <target> <task> [options]"
_COL_MIN = 31


def _opt_sort_key(row: tuple[str, str]) -> str:
    m = _LONG_OPT_RE.search(row[0])
    return m.group(1) if m else row[0]


def _parse_arg_rows(block: str) -> list[tuple[str, str]]:
    """Parse a \\b block of 'name  description\\n    continuation' into (name, desc) pairs."""
    rows = []
    current_name: str | None = None
    current_parts: list[str] = []
    for line in block.splitlines():
        if line.strip() in ("\b", "\x08", ""):
            continue
        m = re.match(r'^(<\S+>|"[^"]+":)\s{2,}(.*)', line)
        if m:
            if current_name:
                rows.append((current_name, " ".join(current_parts)))
            current_name = m.group(1)
            current_parts = [m.group(2).strip()]
        elif current_name and line[:1] in (" ", "\t"):
            current_parts.append(line.strip())
    if current_name:
        rows.append((current_name, " ".join(current_parts)))
    return rows


def _fmt_meta(s: str) -> str:
    """Convert ALL_CAPS metavars to <lowercase> — e.g. TARGET → <target>, INTEGER → <integer>."""
    return _UPPER_WORD_RE.sub(lambda m: f"<{m.group().lower()}>", s)


class CliError(click.ClickException):
    """ClickException styled with red text and trailing whitespace."""

    def show(self, file=None) -> None:
        click.echo(click.style(f"Error: {self.message}", fg="red"))
        click.echo()
        click.echo()


class SwarmCommand(click.Command):
    """click.Command subclass help formatting."""

    def __init__(self, *args, fields_section: str = "Fields", **kwargs):
        self.fields_section = fields_section
        super().__init__(*args, **kwargs)

    def _real_opts(self, ctx):
        """Return option help records excluding the bare help flag."""
        opts = []
        for param in self.get_params(ctx):
            rv = param.get_help_record(ctx)
            if rv is not None and "--help" not in rv[0]:
                opts.append(rv)
        return opts

    def format_help(self, ctx, formatter):
        pieces = self.collect_usage_pieces(ctx)
        opts = sorted(self._real_opts(ctx), key=_opt_sort_key)
        if not opts:
            pieces = [p for p in pieces if p != "[OPTIONS]"]
        else:
            pieces = [p for p in pieces if p != "[OPTIONS]"] + ["[options]"]
        pieces = [_fmt_meta(p) for p in pieces]
        usage_line = f"{ctx.command_path} {' '.join(pieces)}".strip()

        desc = ""
        field_rows: list[tuple[str, str]] = []
        if self.help:
            sections = inspect.cleandoc(self.help).split("\n\n")
            desc = sections[0].replace("\n", " ").strip()
            if len(sections) > 1:
                field_rows = _parse_arg_rows(sections[1])

        opt_keys = [_fmt_meta(k) for k, _ in opts]
        col = max(
            [len(usage_line)]
            + [len(k) for k in opt_keys]
            + [len(k) for k, _ in field_rows]
            + [_COL_MIN]
        )

        formatter.write("\n")
        formatter.write(f"Usage: {usage_line}\n")
        if desc:
            formatter.write(f"\n{desc} \n")

        if opts:
            with formatter.section("Options"):
                formatter.write_dl([(_fmt_meta(k).ljust(col), v) for k, v in opts], col_max=col)

        if field_rows:
            with formatter.section(self.fields_section):
                formatter.write_dl([(k.ljust(col), v) for k, v in field_rows], col_max=col)

    def get_help(self, ctx):
        return super().get_help(ctx) + "\n"


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
            if cmd is None or cmd.hidden or subcommand == "run":
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
            (f"{prog} <target> <task> [options]", "Shortcut to run swarm immediately"),
            (f"{prog} [options] [command]",        "Properly use all options and commands"),
        ]

        def _norm(s: str) -> str:
            return s.rstrip(".")

        # Run options shown directly since `swarm <target> <task>` is the primary usage
        run_opt_rows = []
        run_cmd = self.commands.get("run")
        if run_cmd:
            with click.Context(run_cmd, parent=ctx, info_name="run") as run_ctx:
                for param in run_cmd.get_params(run_ctx):
                    rv = param.get_help_record(run_ctx)
                    if rv is not None:
                        key, val = rv
                        if "--help" not in key:
                            run_opt_rows.append((_fmt_meta(key), _norm(val)))

        group_opt_rows = [
            (k, "Display help for this command" if "--help" in k else _norm(v))
            for p in self.get_params(ctx)
            if (rv := p.get_help_record(ctx)) is not None
            for k, v in [rv]
        ]

        opt_rows = sorted(run_opt_rows + group_opt_rows, key=_opt_sort_key)
        cmd_rows = [(k, _norm(v)) for k, v in self._command_rows(ctx, formatter)]

        # Parse arg rows early so they contribute to unified col calculation
        arg_rows: list[tuple[str, str]] = []
        example_sections: list[str] = []
        if run_cmd and run_cmd.help:
            sections = inspect.cleandoc(run_cmd.help).split("\n\n")
            if len(sections) > 1:
                arg_rows = _parse_arg_rows(sections[1])
            example_sections = sections[2:]

        col = max(
            (len(k) for k, _ in usage_pairs + opt_rows + cmd_rows + arg_rows),
            default=20,
        )

        formatter.write("\n")
        formatter.write("Usage:\n")
        for key, desc in usage_pairs:
            formatter.write(f"  {key:<{col}}  {desc}\n")

        if arg_rows:
            formatter.write_paragraph()
            formatter.indent()
            formatter.write_dl([(k.ljust(col), v) for k, v in arg_rows], col_max=col)
            formatter.dedent()

        for section in example_sections:
            formatter.write_paragraph()
            formatter.write_text(section)

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
