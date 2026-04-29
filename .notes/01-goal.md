# UX-Swarm

## Overview

UX Swarm is a CLI tool for synthetic UX testing. It simulates a swarm of users interacting with a URL or screenshot to complete a task and returns a completion rate, comments, and friction points.

Run it again after frontend changes to compare results.

Two modes:

- **Browser swarm** — each agent controls a Playwright browser session against a live URL, completing multi-step tasks that reflect actual navigation behaviour. Examples:
  - "Create an account from the homepage"
  - "Find the best price option for this service"
  - "Locate the cancellation flow and cancel your subscription"
- **Screenshot swarm** — each agent inspects a UI screenshot with LLM vision and decides what they would do; faster and cheaper, useful for quick evaluations and mobile development without a live URL. Examples:
  - "Where would you click first to get started?"
  - "Which pricing plan would you choose and why?"
  - "Find where to contact support"

Built for frontend developers who want to validate design decisions and user flows with quantifiable metrics and repeatable testing.

## Prototype

I created a project prototype at [masonomara/ux-swarm--beta](https://github.com/masonomara/ux-swarm--beta)

## Tech Stack

- **Python 3.11+** — asyncio, ExceptionGroup, clean CancelledError propagation
- **Click** — CLI framework; SmartGroup for implicit URL/image routing
- **LiteLLM** — provider abstraction across OpenAI, Anthropic, Gemini
- **Playwright (async)** — browser automation; one browser instance, isolated contexts per agent
- **Pydantic** — data models and result serialization
- **Rich** — terminal output; live progress table during runs, formatted result display
- **asyncio + Semaphore** — concurrency; default 5 concurrent browser agents, 20 screenshot agents

## CLI Surface

```bash
swarm <target (url/image)> <task>          # implicit run — SmartGroup detects URL or image path
swarm run <target (url/image)> <task>      # explicit

swarm config                               # first-run onboarding wizard and config editor
swarm users                                # list active user types and weights
swarm users --config                       # write .swarm/users.json for editing
swarm results                              # list and view saved results - essentially puts .swarm/results.json into plain text in the terminal
```

**Options on `run`:**

| Option          | Default | Notes                     |
| --------------- | ------- | ------------------------- |
| `--users N`     | 20      | number of simulated users |
| `--max-steps N` | 3       | browser mode only         |
| `--viewport PX` | 1280    | browser mode only         |
| `--verbose`     | off     | full tracebacks on error  |

`--max-steps` and `--viewport` are browser-only; ux-swarm ignores them in screenshot mode.

## Configuration

On first run, `swarm config` walks through an interactive setup wizard with selectable list inputs and incremental rendering.

1. Select LLM provider (OpenAI, Anthropic, Gemini)
2. Enter API key — validated live against the provider
3. Select model — list generated dynamically from the provider
4. Install Playwright and Chromium — required to continue; the wizard does not complete without it

Config is saved as JSON; edit it directly or re-run `swarm config`. Resolution order (later wins):

1. Hardcoded defaults
2. `~/.config/uxswarm/config.json` — global
3. `.swarm/config.json` — project-local
4. CLI flags — highest priority
5. Environment variables — for API keys in CI (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, etc.)

## Core Concepts

```text
swarm <target> <task>
       │          │
       │          └── what each agent is trying to accomplish
       │
       ├── URL   → Browser Swarm
       │           └── Agent: screenshot → LLM decision → Playwright action → repeat
       │
       └── Image → Screenshot Swarm
                   └── Agent: image + task + user type → single LLM call → structured decision
```

**Target:** A URL or image path. The CLI parses it to determine whether to run a browser swarm or screenshot swarm.

**Task:** Command or goal for agents to execute.

**User Type:** The behavior and persona of a simulated user. Defaults to one type; edit `.swarm/users.json` to define types and distributions.

**Agent:** One synthetic user; receives a user type, target, and task.

**Step:** One interaction (click, type, or scroll) by an agent, followed by an LLM decision about the next action.

**Swarm:** A collection of agents performing the task.

**Swarm Result:** Completed swarm data including completion rates, comments, and friction points.

## User Type System

By default all agents share one user type (100% weight). Run `swarm users --config` to write `.swarm/users.json` and define custom types and distributions.

```json
[
  {
    "label": "Default",
    "weight": 1.0,
    "description": "In a hurry and doesn't read pages — scans them quickly, looking for words or links that match the task. Doesn't weigh options or look for the best choice; clicks the first thing that looks reasonable enough to work (satisficing). Doesn't try to understand how the site is structured or how things work — muddles through, and if something seems to work, sticks with it without figuring out why. Has low tolerance for friction: any moment that requires stopping to think, read instructions, or decode an interface increases the chance of giving up and abandoning the task."
  }
]
```

To split the swarm across multiple user types, add entries and adjust weights:

```json
[
  {
    "label": "Impatient",
    "weight": 0.6,
    "description": "In a hurry, low patience, clicks the first thing that looks relevant."
  },
  {
    "label": "Power User",
    "weight": 0.4,
    "description": "Looks for keyboard shortcuts and dense controls. Frustrated by oversimplified UIs."
  }
]
```

If `.swarm/users.json` exists, it replaces the default entirely. Weights are relative — they don't need to sum to 1.

## Results

Results are saved to `.swarm/results.json` as an append-only array. Each entry records the mode, target, task, and metrics.

```json
[
  {
    "timestamp": "2026-04-25T14:32:00",
    "mode": "browser",
    "target": "https://example.com",
    "task": "find and click the login button",
    "model": "claude-sonnet-4-20250514",
    "users": 20,
    "completion_rate": 0.75,
    "margin_of_error": 0.09,
    "user_breakdown": { "Default User": 0.75 },
    "friction_points": ["Login button not visible above fold"],
    "total_cost": 0.14,
    "individual_results": [...]
  }
]
```

`swarm results` lists all entries with timestamp, mode, target, and completion rate. Use `-n 3` to show the last three results.
