import click

# TODO: this file and/or lone SmartGroup may be renamed or merged elsewhere as the CLI grows


class SmartGroup(click.Group):
    """Routes bare URLs/image paths to `run` without requiring 'run' explicitly."""

    def parse_args(self, ctx, args):
        if args and self._looks_like_target(args[0]):
            args = ["run"] + args
        return super().parse_args(ctx, args)

    @staticmethod
    def _looks_like_target(arg: str) -> bool:
        # TODO: may need smarter detection (MIME sniffing, HEAD request for URLs, etc.)
        extensions = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        return arg.startswith(("http://", "https://")) or any(
            arg.endswith(ext) for ext in extensions)
