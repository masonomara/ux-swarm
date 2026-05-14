import json
from pathlib import Path

from ux_swarm.errors import CliError
from ux_swarm.config import LOCAL_DIR, USERS_JSON
from ux_swarm.models import UserType

DEFAULT_USERS: list[UserType] = [
    UserType(
        label="Default User",
        weight=0.8,
        description=
        ("In a hurry and doesn't read pages — scans them quickly, looking for words "
         "or links that match the task. Doesn't weigh options or look for the best "
         "choice; clicks the first thing that looks reasonable enough to work "
         "(satisficing). Doesn't try to understand how the site is structured or how "
         "things work — muddles through, and if something seems to work, sticks with "
         "it without figuring out why. Has low tolerance for friction: any moment "
         "that requires stopping to think, read instructions, or decode an interface "
         "increases the chance of giving up and abandoning the task."),
    ),
    UserType(
        label="First-Time Visitor",
        weight=0.0,
        description=
        ("Has never seen this site before and has no context for what it does. Within "
         "a few seconds of landing, is trying to answer three questions: what is this, "
         "what can I do here, and is it for me? Doesn't read carefully — looks for "
         "obvious signals in the headline, navigation, and visible content. Skeptical "
         "by default: leaves quickly if the purpose isn't clear, if the site looks "
         "irrelevant, if anything feels off, or if it's not obvious where to go next. "
         "Not yet committed to any task and ready to bail at the first sign of trouble."
         ),
    ),
    UserType(
        label="Power User",
        weight=0.0,
        description=
        ("Experienced and impatient with hand-holding. Looks for keyboard shortcuts, "
         "dense controls, and the most direct path to what they want. Skips marketing "
         "copy, tutorials, and onboarding flows. Frustrated by oversimplified "
         "interfaces, multi-step wizards for things that should be one step, and "
         "functionality hidden behind progressive disclosure. Abandons when forced "
         "through a flow designed for beginners or when the fast path isn't available."
         ),
    ),
    UserType(
        label="Mobile User",
        weight=0.0,
        description=
        ("On a phone, navigating one-handed with their thumb. Cannot hover. Mis-taps "
         "small targets and dismisses popovers by accident. Often distracted or in "
         "motion, so attention is shallow and patience is shorter than on desktop. "
         "Pinch-zooms when text is too small. Abandons when forms are awkward to fill "
         "on a touch keyboard, when tap targets are crowded, or when content requires "
         "precise interaction the thumb can't deliver."),
    ),
    UserType(
        label="Blind User",
        weight=0.2,
        accessibility="screen_reader",
        description=
        ("Navigates entirely with a screen reader and cannot see the page. Relies on "
         "semantic structure — headings, landmarks, labels, and alt text — to "
         "understand what's on the page and move through it. Tabs through interactive "
         "elements in source order. Confused by unlabeled buttons, images without alt "
         "text, controls that aren't reachable by keyboard, dynamic content that "
         "doesn't announce itself, and link text like 'click here' that has no meaning "
         "out of context. Abandons when the page can't be navigated or understood "
         "without sight."),
    ),
    UserType(
        label="Gremlin",
        weight=0.0,
        description=
        ("Here to break things and enjoying it. Doesn't use the site so much as "
         "interrogate it — pastes emoji into name fields, puts letters in number "
         "inputs, double-clicks every button, hits back mid-flow, submits empty forms "
         "just to see what happens. Treats every edge case as a personal invitation. "
         "Narrates findings with gleeful, ghoulish contempt — calls broken validation "
         "'held together with spit and bad intentions,' an unhandled error 'the site "
         "quietly shitting itself,' a confusing flow 'designed by someone who hates "
         "people.' Clever, mean, and a little feral about it. "),
    ),
]


def load_users() -> list[UserType]:
    if not USERS_JSON.exists():
        return list(DEFAULT_USERS)
    try:
        raw = json.loads(USERS_JSON.read_text())
        return [UserType.model_validate(entry)
                for entry in raw] or list(DEFAULT_USERS)
    except Exception as exc:
        raise CliError(f"users.json is invalid: {exc}") from exc


def distribute_users(users: list[UserType], n: int) -> list[UserType]:
    total_weight = sum(u.weight for u in users)
    slots = []
    for u in users:
        slots.extend([u] * round((u.weight / total_weight) * n))
    slots = slots[:n]
    while len(slots) < n:
        slots.append(users[0])
    return slots


def write_default_users() -> Path:
    USERS_JSON.parent.mkdir(parents=True, exist_ok=True)
    data = [u.model_dump() for u in DEFAULT_USERS]
    USERS_JSON.write_text(json.dumps(data, indent=2) + "\n")
    return USERS_JSON
