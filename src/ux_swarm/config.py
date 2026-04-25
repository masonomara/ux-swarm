import json as _json
import urllib.error
import urllib.request
from pathlib import Path

LOCAL_DIR = Path(".swarm")
LOCAL_CONFIG = LOCAL_DIR / "config.json"
GLOBAL_CONFIG = Path.home() / ".config" / "ux-swarm" / "config.json"

PROVIDERS: list[dict[str, str]] = [
    {"name": "OpenAI",        "key": "openai",    "env": "OPENAI_API_KEY"},
    {"name": "Anthropic",     "key": "anthropic", "env": "ANTHROPIC_API_KEY"},
    {"name": "Google Gemini", "key": "gemini",    "env": "GEMINI_API_KEY"},
    {"name": "DeepSeek",      "key": "deepseek",  "env": "DEEPSEEK_API_KEY"},
]

class ProviderAuthError(Exception):
    """Raised when a provider rejects an API key (HTTP 401/403)."""


def provider_env_var(provider_key: str) -> str:
    return next(p["env"] for p in PROVIDERS if p["key"] == provider_key)


def fetch_provider_models(provider_key: str, api_key: str) -> list[str]:
    """Fetch available models from the provider API.

    Raises ProviderAuthError on 401/403.
    Raises urllib.error.URLError / urllib.error.HTTPError on other failures.
    """
    try:
        if provider_key == "openai":
            req = urllib.request.Request(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
            return sorted(
                f"openai/{m['id']}" for m in data.get("data", [])
                if ":" not in m["id"]
                and (
                    m["id"].startswith("gpt-")
                    or m["id"].startswith("chatgpt-")
                    or (m["id"][:1] == "o" and m["id"][1:2].isdigit())
                )
            )

        if provider_key == "anthropic":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models?limit=1000",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
            return [f"anthropic/{m['id']}" for m in data.get("data", [])]

        if provider_key == "gemini":
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/openai/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
            return sorted(
                f"gemini/{m['id']}" for m in data.get("data", [])
                if m["id"].startswith("gemini-") and "embedding" not in m["id"]
            )

        if provider_key == "deepseek":
            req = urllib.request.Request(
                "https://api.deepseek.com/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
            return [f"deepseek/{m['id']}" for m in data.get("data", [])]

        raise ValueError(f"Unknown provider: {provider_key}")

    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderAuthError from exc
        raise
