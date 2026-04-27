import asyncio
import base64
import json
import random
from pathlib import Path
from typing import cast

import click
import litellm
from litellm import ModelResponse, acompletion, completion_cost
from litellm.types.utils import Usage
from litellm.exceptions import RateLimitError

from ux_swarm.models import ScreenshotDecision, UserType

litellm.suppress_debug_info = True

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_RETRY_DELAYS = (60, 120, 240)


def _media_type(path: Path) -> str:
    """Return the MIME type for a path, defaulting to image/png for unrecognized extensions."""
    return _MIME_TYPES.get(path.suffix.lower(), "image/png")


def _load_image(target: str) -> tuple[str, str]:
    """Return (base64_data, mime_type) for an image file; raises FileNotFoundError if missing."""
    path = Path(target)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {target}")
    media = _media_type(path)
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    return data, media


def _build_system_prompt(user_type: UserType) -> str:
    """Build the system prompt for a screenshot agent: persona, UX instruction, and JSON schema."""
    schema = json.dumps(ScreenshotDecision.model_json_schema(), indent=2)
    return (
        "You are a synthetic user in a UX test.\n\n"
        f"You are playing the role of: {user_type.label}\n"
        f"{user_type.description}\n\n"
        "Important: if you feel confused or uncertain about what to do, that confusion is the finding — record it in friction_observed.\n\n"
        "Return a JSON object matching this schema exactly:\n"
        f"{schema}\n\n"
        "Return ONLY valid JSON. No markdown fences, no explanation, no preamble."
    )


async def _call_with_retry(model: str, messages: list[dict]) -> ModelResponse:
    """Call acompletion with exponential backoff on rate limits; raises ClickException after all retries exhausted."""
    for delay in (*_RETRY_DELAYS, None):
        try:
            return cast(
                ModelResponse, await acompletion(
                    model=model,
                    messages=messages,
                    max_tokens=1024,
                    response_format={"type": "json_object"},
                ))
        except RateLimitError as exc:
            if delay is None:
                raise click.ClickException(
                    f"Rate limited after {len(_RETRY_DELAYS) + 1} attempts. "
                    "Try again later or reduce --users.") from exc
            await asyncio.sleep(delay + random.uniform(0, delay * 0.5))
    raise AssertionError("unreachable")


async def run_screenshot_agent(
    target: str,
    task: str,
    user_type: UserType,
    model: str,
) -> tuple[ScreenshotDecision, int, int, float]:
    """Public entry point for a single screenshot agent. Returns (ScreenshotDecision, input_tokens, output_tokens, cost)."""
    image_data, media_type = _load_image(target)
    system = _build_system_prompt(user_type)

    messages = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Task: {task}"},
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{image_data}"}},
            ],
        },
    ]

    response = await _call_with_retry(model, messages)
    raw = response.choices[0].message.content or ""
    usage = cast(Usage, getattr(response, "usage"))
    in_tok = usage.prompt_tokens
    out_tok = usage.completion_tokens

    try:
        cost = completion_cost(completion_response=response, model=model)
    except Exception:
        cost = 0.0

    try:
        decision = ScreenshotDecision.model_validate_json(raw)
    except Exception:
        decision = ScreenshotDecision(
            target_element="unknown",
            reasoning=raw[:500],
            comment="Agent response was not valid JSON.",
            friction_observed=["Agent response was not valid JSON"],
            completed=False,
            abandoned=True,
            abandonment_reason="parse failure",
        )

    return decision, in_tok, out_tok, cost
