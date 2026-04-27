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
ux-swarm = "main:cli"
swarm = "main:cli"
```

and then installed in editable mode:

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

```toml
[project.scripts]
ux-swarm = "ux_swarm.main:cli"
swarm = "ux_swarm.main:cli"
```

2. Moved `main.py` and added `__init__.py`:

```
main.py  →  src/ux_swarm/main.py
```

The naming feels weird because Python has two conventions that don't match: folders and imports use underscores (`ux_swarm`), install names and CLI commands use hyphens (`ux-swarm`, `swarm`).

## Recap — Done with Scaffolding

- `UV` is set up for package management
- `Click` is wired up
- `run` command is working implicitly, no need to type it in
- Argument surfaces defined: `target`, `task`, plus `--users`, `--max-steps`, `--viewport`, `--verbose`
- `swarm` and `ux-swarm` are installed as real CLI commands via `pyproject.toml` + `uv pip install -e .`
- Folder structure is aligned with UV best practices

## Pydantic and BaseModel

I installed Pydantic with `uv add pydantic` because the app receives responses from LLM models and external APIs — data we don't control. Pydantic catches malformed or unexpected types at runtime, at the boundary where that data enters the system.

`BaseModel` is the type blueprint. Each field and its type is declared at runtime, then Pydantic enforces them at the moment an object is created.

Pydantic library provides `model_validate()` for parsing raw JSON into a proper Python object, and `model_json_schema()` for sending the schema to the LLM so it knows exactly what shape to return.

The data shapes will evolve and that's ok. What matters now is that we now have the "three systems of governance" in place — the docs, the models, and the code. Each one describes the same thing at a different level. We'll eventually add tests so all three stay in sync.

Models live in `src/ux_swarm/models.py`.

## Config Wizard

Started with adding two dependencies: **Rich** for stylized terminal output (colored text, spinners, etc.) and **Playwright** to check whether the Chromium browser binary is installed, and because it's used during `run`.

The first thing I built was the menu navigation function. It's complex enough — and reusable enough — that it lives in its own `menu.py` file.

Then I built out `config.py` with the provider list, config handling, and error handling. Once the basics were in place, I added a function that dynamically fetches the available model list from Anthropic, OpenAI, DeepSeek, and Google, so the wizard always shows current models rather than a hardcoded list.

Lastly in config, I set up save and load functions for the local disk, and a Playwright check function.

After a UX pass, a few things worth noting:

Playwright (the Python package) is always installed when a user downloads ux-swarm from PyPI. Because it's listed as a dependency, pip installs it automatically. Chromium is not. Chromium is a browser binary, not a Python package, so it doesn't come along for the ride. Users have to install it separately via `playwright install chromium`, which is why the config wizard prompts them to do it.

Because of this, I only expect users to have trouble with Chromium, not Playwright itself. That's why the wizard combines both into a single "Playwright status" step rather than separating them — Playwright is the recognizable name, and Chromium is its add-on.

DeepSeek doesn't support vision models, so it was removed from the project entirely.

## Agent Setup

The first consideration for the agent was image encoding. LLM APIs won't accept a file path, so we have to encode the image before sending it to the API. `_load_image` reads and encodes the image, then tells the API what format it's in with `_MIME_TYPES` + `_media_type`.

The second consideration was how the agent was going to be instructed. Every agent call has two turns. The first is the system prompt taht is set once and defines the model's role, rules, and output format - standing instructions that frame every response. The second turn is the actual request: the task plus the image.

I kept them separated because the first is constant — it applies to every agent the same way. The second is per-call — it's what the agent is being asked to do right now.

Then I wrote separate request formatters for Anthropic and OpenAI/Gemini to translate everything into each provider's expected format, send the request, and extract the response.

## Multi Agent Setup

Started with setting up the user types in `personas.py`, removed the default hardcoded persona we had wired up in main. `personas.py` also handles weight distribution and reading the `.swarm/users.json` file. It exists as its own file to separate everything "user types" related.

Then, I worked on removing the custom request formatters for LiteLLM in `agents.py`. We are doing a single uniform task over multiple providers, no need for a channel adapter for different models when there's a thrid-party service that can manage them all. This makes edge cases such as Rate Limit errors all uniformly handled, normalized. Way less maintence unless LiteLLm itself breaks, it is now a single point of failue. Lets make sure that LiteLLM stays siloed.

In `swarm.py`, TaskGroup makes sure all N agents are in flight, then we wait for all of them to complete before moving on to the next step. We have a silent agent drop setup so if a single agent throws an error like a network blip or malformed response, we catch it and skip appending a result for that agent. We don't crash the whole run. We do have an `_aggregate` function in case every single agent fails, for a clean error message.

We had to add `asyncio.Semaphore` to cap concurrent API calls so all agents wouldn't send their requests at the same moment and saturate the rate limit. The semaphore makes sure only 5 agents can be working at once. We also added random jitter to retry delays — without it, all agents that hit a rate limit would wait the same fixed delay and retry simultaneously, immediately hitting the limit again. This is called the thundering herd problem, jitter staggers them so they don't pile up.

The swarm coordinator in `swarm.py` is completely isolated from the terminal. It accepts an optional `on_agent_done(done, total)` callback. `main.py` passes in the function that updates the live display. This keeps `swarm.py` output-agnostic — it could run in a web server, a test, or a CI pipeline without modification.

When there was one agent, showing its individual comment, target element, and reasoning made sense. With 20 agents, we want a single number that tells you how the flow performed. We switched to completion rate, margin of error, and the top friction points ranked by frequency. The completion rate is a proportion (X out of N agents completed the task). `1.96 * sqrt(p*(1-p)/n)` gives the 95% confidence interval for a proportion. The 1.96 is the z-score for 95%. This means if you see "75% ±9%", the true rate is likely between 66% and 84%.

Each agent reports a list of friction points as free text. We mgiht look into consolidating this further at some pont, but for now `Counter` counts how many times each unique string appears and `.most_common(5)` returns the top five.

We are storign the results in an append-only array in `results.json` - an array of all past runs - instead of individual timestamped files. A single json file means reading everything is one `json.loads` call. Simple to implement, simple to read, simple to export.
