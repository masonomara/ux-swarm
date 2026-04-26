import base64
import json
import urllib.request
from pathlib import Path

from ux_swarm.models import ScreenshotDecision, UserType

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


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
        f"You are a synthetic user in a UX test.\n\n"
        f"You are playing the role of: {user_type.label}\n"
        f"{user_type.description}\n\n"
        "Important: if you feel confused or uncertain about what to do, that confusion is the finding — record it in friction_observed.\n\n"
        "Return a JSON object matching this schema exactly:\n"
        f"{schema}\n\n"
        "Return ONLY valid JSON. No markdown fences, no explanation, no preamble."
    )


_OPENAI_COMPAT_ENDPOINTS = {
    "openai":
    "https://api.openai.com/v1/chat/completions",
    "gemini":
    "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
}


def _call_anthropic(
    model_id: str,
    api_key: str,
    system: str,
    image_data: str,
    media_type: str,
    user_prompt: str,
) -> tuple[str, int, int]:
    """POST a vision request to the Anthropic messages API. Returns (response_text, input_tokens, output_tokens)."""
    body = json.dumps({
        "model":
        model_id,
        "max_tokens":
        1024,
        "system":
        system,
        "messages": [{
            "role":
            "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_data,
                    },
                },
                {
                    "type": "text",
                    "text": user_prompt
                },
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return (
        data["content"][0]["text"],
        data["usage"]["input_tokens"],
        data["usage"]["output_tokens"],
    )


def _call_openai_compat(
    endpoint: str,
    model_id: str,
    api_key: str,
    system: str,
    image_data: str,
    media_type: str,
    user_prompt: str,
) -> tuple[str, int, int]:
    """POST a vision request to an OpenAI-compatible endpoint. Returns (response_text, input_tokens, output_tokens)."""
    body = json.dumps({
        "model":
        model_id,
        "max_tokens":
        1024,
        "response_format": {
            "type": "json_object"
        },
        "messages": [
            {
                "role": "system",
                "content": system
            },
            {
                "role":
                "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{image_data}"
                        },
                    },
                    {
                        "type": "text",
                        "text": user_prompt
                    },
                ],
            },
        ],
    }).encode()
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return (
        data["choices"][0]["message"]["content"],
        data["usage"]["prompt_tokens"],
        data["usage"]["completion_tokens"],
    )


def _call_llm(
    provider: str,
    model_id: str,
    api_key: str,
    system: str,
    image_data: str,
    media_type: str,
    user_prompt: str,
) -> tuple[str, int, int]:
    """Route a vision LLM call to the correct provider and return (response_text, input_tokens, output_tokens)."""
    if provider == "anthropic":
        return _call_anthropic(model_id, api_key, system, image_data,
                               media_type, user_prompt)
    if provider in _OPENAI_COMPAT_ENDPOINTS:
        return _call_openai_compat(
            _OPENAI_COMPAT_ENDPOINTS[provider],
            model_id,
            api_key,
            system,
            image_data,
            media_type,
            user_prompt,
        )
    raise ValueError(
        f"Unknown provider: {provider!r} — run `swarm config` to reconfigure")


def run_screenshot_agent(
    target: str,
    task: str,
    user_type: UserType,
    provider: str,
    model_id: str,
    api_key: str,
) -> tuple[ScreenshotDecision, int, int]:
    """Public entry point for a single screenshot agent. Returns (ScreenshotDecision, input_tokens, output_tokens)."""
    image_data, media_type = _load_image(target)
    system = _build_system_prompt(user_type)
    user_prompt = f"Task: {task}"
    raw, in_tok, out_tok = _call_llm(provider, model_id, api_key, system,
                                     image_data, media_type, user_prompt)
    decision = ScreenshotDecision.model_validate_json(raw)
    return decision, in_tok, out_tok
