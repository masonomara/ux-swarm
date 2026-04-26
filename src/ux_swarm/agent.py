import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

import click

from ux_swarm.models import ScreenshotDecision, UserType

_MIME_TYPES = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif":  "image/gif",
}


def _media_type(path: Path) -> str:
    return _MIME_TYPES.get(path.suffix.lower(), "image/png")


def _load_image(target: str) -> tuple[str, str]:
    path = Path(target)
    if not path.exists():
        raise click.ClickException(f"Image not found: {target}")
    media = _media_type(path)
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return data, media


def _build_system_prompt(user_type: UserType) -> str:
    schema = json.dumps(ScreenshotDecision.model_json_schema(), indent=2)
    return (
        f"You are a synthetic user in a UX test.\n\n"
        f"You are playing the role of: {user_type.label}\n"
        f"{user_type.description}\n\n"
        "Important: if you feel confused or uncertain about what to do, that confusion is the finding — record it in friction_observed.\n\n"
        "Return a JSON object matching this schema exactly:\n"
        f"{schema}\n\n"
        "Return ONLY valid JSON. No markdown fences, no explanation, no preamble."
    )
