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


The forst considerationf or the agent was the screenshot swarm, a little simperl to set up. The first considerationf or the screenshot swarm were the image types, LLM API's wont accept a file paths, so we have to encode the image before sending it to the API. `_load_image` reads and encodes the image, then tells the api what format its in with `_MIME_TYPES` + `_media_type`.

The second consideration was how the agent was goign to be instructed. every agent call has two turns, first is the system prompt that is set once and defines the model's role, rules, and output format — standing instructions that frame every response. second turn is the actual request: the task plus the image

I had to keep them seperated because the first is epehreal, applies to every agent, the second is stateful, what the agent is beign asked to do right now.