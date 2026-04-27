import json
from pathlib import Path

import click

from ux_swarm.config import LOCAL_DIR
from ux_swarm.models import UserType

USERS_JSON = LOCAL_DIR / "users.json"

DEFAULT_USERS: list[UserType] = [
    UserType(
        label="Default User",
        weight=1.0,
        description=
        ("In a hurry and doesn't read pages — scans them quickly, looking for words "
         "or links that match the task. Doesn't weigh options or look for the best "
         "choice; clicks the first thing that looks reasonable enough to work "
         "(satisficing). Doesn't try to understand how the site is structured or how "
         "things work — muddles through, and if something seems to work, sticks with "
         "it without figuring out why. Has low tolerance for friction: any moment "
         "that requires stopping to think, read instructions, or decode an interface "
         "increases the chance of giving up and abandoning the task."),
    )
]


def load_users() -> list[UserType]:
    """Load user types from .swarm/users.json, falling back to DEFAULT_USERS if the file doesn't exist or is empty."""
    if not USERS_JSON.exists():
        return list(DEFAULT_USERS)

    try:
        raw = json.loads(USERS_JSON.read_text())
        users = [UserType.model_validate(entry) for entry in raw]
        return users if users else list(DEFAULT_USERS)
    except Exception as exc:
        raise click.ClickException(f"users.json is invalid: {exc}") from exc


def distribute_users(users: list[UserType], n: int) -> list[UserType]:
    """Assign user types to exactly n agent slots, proportional to weight. Every type gets at least one slot."""
    total_weight = sum(u.weight for u in users)
    slots: list[UserType] = []

    for u in users:
        count = max(1, round((u.weight / total_weight) * n))
        slots.extend([u] * count)

    if len(slots) > n:
        slots = slots[:n]
    while len(slots) < n:
        slots.append(users[0])

    return slots


def write_default_users() -> Path:
    """Write DEFAULT_USERS to .swarm/users.json as a starting point for customization."""
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    data = [u.model_dump() for u in DEFAULT_USERS]
    USERS_JSON.write_text(json.dumps(data, indent=2) + "\n")
    return USERS_JSON
