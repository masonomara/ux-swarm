# Build Notes

## Resources

- [Poetry vs UV: Which Python Package Manager Should You Use in 2025?](https://medium.com/@hitorunajp/poetry-vs-uv-which-python-package-manager-should-you-use-in-2025-4212cb5e0a14)
- [How to Build a Python CLI Tool People Actually Want to Use](https://www.youtube.com/watch?v=FWacanslfFM)

## Creating UX Swarm

First thing I did was install UV. I wanted a package manager and version control tool. At first I looked at Poetry, but UV seemed faster and better maintained. The head of Poetry had left the project so I was worried about maintenance. UV also worked well with Click, which I wanted to use to keep the argument/command/subcommand conventions I had established in the UX Swarm MVP.

### Poetry → UV Cheatsheet

| Poetry                                       | UV                                                    |
| -------------------------------------------- | ----------------------------------------------------- |
| `poetry new pyproject`                       | `uv init myproject`                                   |
| `poetry add click`                           | `uv add click`                                        |
| `poetry add --group dev pytest`              | `uv add --dev pytest`                                 |
| `poetry remove click`                        | `uv remove click`                                     |
| `poetry install`                             | `uv sync`                                             |
| `poetry run python script.py`                | `uv run python script.py`                             |
| `poetry shell` / `source .venv/bin/activate` | `source .venv/bin/activate` (UV has no shell command) |
| `poetry lock`                                | `uv lock`                                             |
| `poetry build`                               | `uv build`                                            |
| `poetry publish`                             | `uv publish`                                          |

### Setting Up UV

**1. Initialize a new project**

```bash
uv init
```

**2. Add dependencies**

```bash
uv add click
```

**3. Install dependencies (fast, cached)**

```bash
uv sync
```

**4. Run inside the environment**

```bash
uv run main.py
```

> Note: UV keeps the virtual environment in `.venv` and doesn't auto-activate it during development. Dependencies automatically go into `.venv`, and `uv run` uses it automatically. This keeps the environment separated and isolated, which is good.

## Click Conventions

```bash
uv add click
```

I added conventions to `CLAUDE.md` based on Simon Willison's [blog post](https://simonwillison.net/2023/Sep/30/cli-tools-python/).

**Arguments** are good for happy paths — required, positional inputs - things that every run needs. For this project, that's `target` (URL or screenshot path) and `task`.

```
ux-swarm [target] [task]
```

Examples:

```bash
ux-swarm https://example.com "complete the checkout flow"
ux-swarm screenshot.png "find and submit the contact form"
```

**Options** are good for modifying behavior and can have defaults.

## Entry Points

I set up two entry point scripts in `pyproject.toml`:

```toml
[project.scripts]
ux-swarm = "ux_swarm.main:cli"
swarm = "ux_swarm.main:cli"
```

Then installed in editable mode:

```bash
uv pip install -e .
```

Both `swarm` and `ux-swarm` now work anywhere in the terminal. Editable mode means changes to `main.py` take effect immediately without reinstalling.

Examples:

```bash
swarm https://example.com "complete the checkout flow"
swarm screenshot.png "find and submit the contact form"
```

## Build Backend

We need this project to be a distributable package. The build backend is responsible for that. Traditionally this was done by Hatchling or other tools, but UV now ships its own native build backend (`uv_build`).

UV's default convention for installable packages with CLI entry points is `src/<package>` — it's what `uv init --package` generates out of the box. We restructured the folder to follow that convention.

1. Added a build system block to `pyproject.toml` and updated the entry points to reflect the new location:

```toml
[build-system]
requires = ["uv_build>=0.11.7,<0.12.0"]
build-backend = "uv_build"
```

2. Moved `main.py` and added `__init__.py`:

```
main.py  →  src/ux_swarm/main.py
```

The naming feels odd because Python has two conventions that don't match: folders and imports use underscores (`ux_swarm`), install names and CLI commands use hyphens (`ux-swarm`, `swarm`).

## Recap — Done with Scaffolding

- `UV` is set up for package management
- `Click` is wired up
- `run` command works implicitly - no need to type it
- Argument surfaces defined: `target`, `task`, plus `--users`, `--max-steps`, `--viewport`, `--verbose`
- `swarm` and `ux-swarm` are installed as real CLI commands via `pyproject.toml` + `uv pip install -e .`
- Folder structure is aligned with UV best practices

## Pydantic and BaseModel

I installed Pydantic with `uv add pydantic` because the app receives responses from LLM models and external APIs — data we don't control. Pydantic catches malformed or unexpected types at runtime, at the boundary where that data enters the system.

`BaseModel` is the type blueprint. Each field and its type is declared at class definition, then Pydantic enforces them at the moment an object is created.

Pydantic provides `model_validate()` for parsing raw JSON into a proper Python object, and `model_json_schema()` for sending the schema to the LLM so it knows exactly what shape to return.

The data shapes will evolve and that's fine. What matters now is that we have the three systems of governance in place — the docs, the models, and the code. Each one describes the same thing at a different level. Tests come later to keep all three in sync.

Models live in `src/ux_swarm/models.py`.

## Config Wizard

Started with two dependencies: **Rich** for styled terminal output (colored text, spinners, etc.) and **Playwright** to check whether the Chromium browser binary is installed, since it's also used during `run`.

The first thing I built was the menu navigation function. It's complex and reusable enough to live in its own file: `menu.py`.

Then I built out `config.py` with the provider list, config handling, and error handling. Once the basics were in place, I added a function that dynamically fetches the available model list from Anthropic, OpenAI, and Google so the wizard always shows current models rather than a hardcoded list.

Lastly, I set up save and load functions for the local disk and a Playwright check function.

A few things worth noting after a UX pass:

Playwright (the Python package) is always installed when a user downloads ux-swarm from PyPI — it's listed as a dependency, so pip installs it automatically. Chromium is not. Chromium is a browser binary, not a Python package, so it doesn't come along for the ride. Users have to install it separately via `playwright install chromium`, which is why the config wizard prompts them to do it.

Because of this, I only expect users to have trouble with Chromium, not Playwright itself. That's why the wizard combines both into a single "Playwright status" step rather than separating them — Playwright is the recognizable name, and Chromium is its add-on.

DeepSeek doesn't support vision models, so it was removed from the project entirely.

## Agent Setup

The first consideration for the agent was image encoding. LLM APIs won't accept a file path, so we have to encode the image before sending it to the API. `_load_image` reads and encodes the image, then tells the API what format it's in with `_MIME_TYPES` and `_media_type`.

The second consideration was how the agent would be instructed. Every agent call has two parts. The first is the system prompt — set once, it defines the model's role, rules, and output format. The second is the actual request: the task plus the image.

I kept them separate because the system prompt is constant — it applies to every agent the same way. The request is per-call — it's what the agent is being asked to do right now.

Then I wrote separate request formatters for Anthropic and OpenAI/Gemini to translate everything into each provider's expected format, send the request, and extract the response. Agent logic lives in `src/ux_swarm/agent.py`.

## Multi-Agent Setup

I started by setting up user types in `personas.py`, removing the default hardcoded persona that had been wired up in `main.py`. `personas.py` also handles weight distribution and reads the `.swarm/users.json` file. It exists as its own file to keep everything user-type-related together.

Then I replaced the custom request formatters with LiteLLM in `agent.py`. We're running a single uniform task across multiple providers, so there's no need for custom channel adapters when a third-party service can manage them all. This normalizes edge cases like rate limit errors. LiteLLM is now a single point of failure, so we keep it siloed.

In `swarm.py`, `TaskGroup` sends all N agents in flight simultaneously, then waits for all of them to complete before moving on. We have silent agent drop: if a single agent throws an error — a network blip or malformed response — we catch it and skip appending a result for that agent. The whole run doesn't crash. We have an `_aggregate` function that fires if every agent fails, for a clean error message.

We added `asyncio.Semaphore` to cap concurrent API calls so all agents don't send requests simultaneously and saturate the rate limit. The semaphore holds only 5 agents working at once. We also added random jitter to retry delays — without it, all agents that hit a rate limit would wait the same fixed delay and retry simultaneously, immediately hitting the limit again. This is the thundering herd problem. Jitter staggers the retries so they don't pile up.

The swarm coordinator in `swarm.py` is completely isolated from the terminal. It accepts an optional `on_agent_done(done, total)` callback. `main.py` passes in the function that updates the live display. This keeps `swarm.py` output-agnostic — it could run in a web server, a test, or a CI pipeline without modification.

When there was one agent, showing its individual comment, target element, and reasoning made sense. With 20 agents, we want a single number that tells you how the flow performed. We switched to completion rate, margin of error, and the top friction points ranked by frequency. The completion rate is a proportion (X out of N agents completed the task). `1.96 * sqrt(p*(1-p)/n)` gives the 95% confidence interval for a proportion — the 1.96 is the z-score for 95%. If you see "75% ±9%", the true rate is likely between 66% and 84%.

Each agent reports a list of friction points as free text. `Counter` counts how many times each unique string appears and `.most_common(5)` returns the top five.

Results are stored in an append-only array in `results.json` — an array of all past runs — rather than individual timestamped files. A single JSON file means reading everything is one `json.loads` call. Simple to implement, simple to read, simple to export.

## Pain Point Aggregator

A lot of friction points were showing almost exactly the same result, just one character or word different. I added a lightweight LLM call in `swarm.py` that aggregates similar pain points into one to avoid redundancy and noise.

## Browser Mode

Screenshot mode was complete: one LLM call per agent with vision inference, one decision, done. Lightweight and working. Browser mode is more involved — it's a Playwright loop where each agent opens a Chromium browser, navigates step by step to complete the task, takes a screenshot after each action, then calls the LLM again until it records `done` or `give_up`, or until the step budget runs out.

Browser mode is the richer version: it has action history, URLs visited, wall-clock duration, and step count. But it comes at a cost — actual API cost and runtime are both higher than screenshot mode.

Browser mode needs two independent resource limits. We created two semaphores: `browser_sem` limits open browser contexts (held in memory for the full run), and `llm_sem` limits concurrent LLM API calls. They operate independently.

### The Browser Agent Loop

The core of browser mode is a stateful step loop inside `browser_agent.py`. Each iteration takes a screenshot, extracts interactive elements from the DOM, calls the LLM with the screenshot plus element list plus recent action history, executes the returned action via Playwright, then repeats. The loop exits when the LLM returns `done` or `give_up`, or when the step budget runs out (`time_out`).

The LLM's `thinking` field is placed first in the `BrowserStep` JSON schema on purpose — LLMs generate tokens left to right, so chain-of-thought before `action` means the model reasons before committing to a move. The rest of the model is flat (`action`, `element_index`, `text`, `friction_observed`, `success`). Earlier drafts split intent and terminal verdict into two separate models, but collapsing them into one removed a lot of unnecessary overhead.

### Technical Notes

Most of this was engineered by Claude Code with me following along doing manual tests. I don't want to present like I fully understand or made the decisions on what is going on, but the following subsections describe what was built that is a bit beyond my scope of involvement in this project:

#### Indexed Element Extraction

The original approach asked the LLM to invent CSS selectors — it hallucinated selectors constantly. The fix: before each LLM call, extract all visible interactive elements from the page via `page.evaluate()` and assign each a numeric index. The LLM receives a numbered list (`[0] BUTTON "Sign Up"`, `[1] INPUT type="email"`) and returns a number from that list. Playwright then dispatches the action with `page.locator(INTERACTIVE_SELECTOR).nth(raw_index)` — no second DOM query.

Two indexes exist internally: `raw_index` (position in the full `querySelectorAll` result, including invisible elements) and `logical_index` (what the LLM sees — capped at 50, invisible elements skipped). The LLM references logical; the agent executes by raw. Visibility is checked via `getBoundingClientRect()` — non-zero width and height means visible, the same check Playwright uses internally.

#### What Counts as Friction

Friction comes from the LLM's `friction_observed` field — subjective observations the synthetic user reports while navigating. Raw Playwright exception strings (`TimeoutError`, element not found) are explicitly not friction. If an action fails, the exception is swallowed, the agent loops, and the LLM sees the unchanged page on the next step. Mixing Playwright internals into the friction list would contaminate UX signal with infrastructure noise.

#### Accessibility Mode

Browser agents have a second rendering path for screen reader personas. When `user_type.accessibility == "screen_reader"`, the agent skips the screenshot entirely, passes only the element list, and restricts available actions to keyboard-only (`press_key`, `type`, `done`, `give_up`). The system prompt is also swapped for a screen-reader-specific version that explains no visual layout is available.

This was worth building because testing keyboard-only navigation is a real accessibility audit need, and the swarm already knows each agent's persona — adding the mode switch required almost no additional plumbing.

#### CLI Parsing and Bare Command Shortcuts

Standard Click requires subcommands to be named explicitly: `swarm run example.com "find prices"`. Getting `swarm example.com find prices` to work — no `run`, no quotes around a multi-word task — required a custom `SmartGroup` class in `cli.py` that intercepts raw `argv` before Click's parser sees it.

`SmartGroup.parse_args()` inspects the argument list, detects whether the first token looks like a URL, bare domain, or image path, and silently prepends `run` if so. It also reconstructs multi-word tasks that would otherwise be split by the shell. `_resolve_target()` in `main.py` handles the second half: normalizing bare domains to `https://` URLs and distinguishing file paths from domain names (file existence check runs first).

#### Config Architecture

Config lives in two places: `~/.config/ux-swarm/config.json` (global, shared across projects) and `.swarm/config.json` (local, per-project). Local overrides global, so teams can commit a shared model choice while individuals keep their own API keys out of the repo.

The model list shown in the config wizard is fetched live from each provider's API using stdlib `urllib` — no hardcoded list. This means the wizard always shows current models and catches bad API keys immediately. Provider-specific filtering is applied (OpenAI skips fine-tunes, Gemini skips embedding models). API keys are written to the corresponding environment variables at runtime so LiteLLM can find them without any extra wiring.

## Designing the Interface

I spent some time in Figma copying and pasting terminal output, then read [10 Design Principles for Delightful CLIs](https://www.atlassian.com/blog/it-teams/10-design-principles-for-delightful-clis) and applied ideas from the article and examples to make the interfaces — especially the help screens — more intuitive. I showed it to a few users and got really positive feedback.

## New Users

I added accessibility options and a badge for users who are using accessibility tools (a11y). I also added default personas that people can use as templates.

## Uploading to PyPI and NPM

This CLI tool was built for PyPI upload. For NPM, I wrapped it in a Python wrapper so users operating on Node can also use it. Both have successfully been downloadable and usable — now on to the README so I can share these packages before building them into a much deeper app.

## The README

This is what 90% of people will look at when deciding whether to use the packages or not — I need to put a lot of care into this.

This is going to be an open-source repo, a PyPI package, and an NPM package, so I need either a great README for all three or separate READMEs for each.

I decided to follow this template: https://github.com/banesullivan/README/blob/main/TEMPLATE.md
