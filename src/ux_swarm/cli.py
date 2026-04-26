import click

# TODO: this file and/or lone SmartGroup may be renamed or merged elsewhere as the CLI grows


class SmartGroup(click.Group):
    """Routes bare URLs/image paths to `run` without requiring 'run' explicitly."""

    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

    def parse_args(self, ctx, args):
        """Intercept args before Click sees them; prepend 'run' if the first arg looks like a URL or image path."""
        if args and not args[0].startswith(
                "-") and args[0] not in self.commands:
            reconstructed = self._try_reconstruct(args)
            if reconstructed:
                args = reconstructed
        return super().parse_args(ctx, args)

    def _try_reconstruct(self, args: list[str]) -> list[str] | None:
        """Return ['run', target, task, ...flags] if args start with a URL or image path, otherwise None."""
        # URL: already a single token, no reconstruction needed
        if args[0].startswith(("http://", "https://")):
            return ["run"] + args

        # Image path: may span multiple tokens if the filename has spaces.
        # Scan for the token ending with a known image extension, then join
        # everything up to it as target and the rest as task.
        for i, arg in enumerate(args):
            if any(arg.lower().endswith(ext)
                   for ext in self._IMAGE_EXTENSIONS):
                target = " ".join(args[:i + 1])
                rest = args[i + 1:]
                # Separate unquoted task words from any flags (--users 5, etc.)
                task_words, flags = [], []
                j = 0
                while j < len(rest):
                    if rest[j].startswith("-"):
                        flags.append(rest[j])
                        if j + 1 < len(rest) and not rest[j +
                                                          1].startswith("-"):
                            flags.append(rest[j + 1])
                            j += 2
                        else:
                            j += 1
                    else:
                        task_words.append(rest[j])
                        j += 1
                task = " ".join(task_words)
                return ["run", target, task] + flags if task else None

        return None
