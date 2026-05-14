import json
import urllib.error
import urllib.request
from pathlib import Path

from rich.console import Console


LOCAL_DIR = Path(".swarm")
LOCAL_CONFIG = LOCAL_DIR / "config.json"
USERS_JSON = LOCAL_DIR / "users.json"
RESULTS_JSON = LOCAL_DIR / "results.json"
GLOBAL_CONFIG = Path.home() / ".config" / "ux-swarm" / "config.json"

PROVIDERS: list[dict[str, str]] = [
    {
        "name": "OpenAI",
        "key": "openai",
        "env": "OPENAI_API_KEY"
    },
    {
        "name": "Anthropic",
        "key": "anthropic",
        "env": "ANTHROPIC_API_KEY"
    },
    {
        "name": "Google Gemini",
        "key": "gemini",
        "env": "GEMINI_API_KEY"
    },
]

console = Console()


class ProviderAuthError(Exception):
    pass


def fetch_provider_models(provider_key: str, api_key: str) -> list[str]:
    try:
        if provider_key == "openai":
            req = urllib.request.Request(
                "https://api.openai.com/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return sorted(
                f"openai/{m['id']}" for m in data.get("data", [])
                if ":" not in m["id"] and
                (m["id"].startswith("gpt-") or m["id"].startswith("chatgpt-")
                 or (m["id"][:1] == "o" and m["id"][1:2].isdigit())))

        if provider_key == "anthropic":
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/models?limit=1000",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01"
                },
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return [f"anthropic/{m['id']}" for m in data.get("data", [])]

        if provider_key == "gemini":
            req = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/openai/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
            return sorted(f"gemini/{m['id']}" for m in data.get("data", [])
                          if m["id"].startswith("gemini-")
                          and "embedding" not in m["id"])

        raise ValueError(f"Unknown provider: {provider_key}")

    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ProviderAuthError from exc
        raise


def load_config() -> dict:
    resolved: dict = {}
    for path in (GLOBAL_CONFIG, LOCAL_CONFIG):
        if path.exists():
            try:
                resolved.update(json.loads(path.read_text()))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Config file is not valid JSON: {path}\n{exc}\nFix or delete it and run again."
                ) from exc
    return resolved


def save_config(data: dict, *, local: bool = True) -> Path:
    target = LOCAL_CONFIG if local else GLOBAL_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data, indent=2) + "\n")
    return target


def playwright_state() -> tuple[bool, bool]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False, False
    try:
        with sync_playwright() as p:
            return True, Path(p.chromium.executable_path).exists()
    except Exception:
        return True, False
