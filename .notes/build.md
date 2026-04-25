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
uv add requests
```

**3. Install dependencies (fast, cached)**

```bash
uv sync
```

**4. Run inside the environment**

```bash
uv run main.py
```

> Note: UV keeps the virtual environment in `.venv` and doesn't auto-activate it during development. Dependencies automatically go into `.venv`, and `uv run` uses it automatically. This keeps the environment sperated and isolated, which is good.

## Click Conventions

```bash
uv add click
```

I added conventions to `CLAUDE.md` based on Simon Willison's [blog post](https://simonwillison.net/2023/Sep/30/cli-tools-python/).

**Arguments** are good for happy paths - required, positional inputs - things every run needs. For this project, that's `target` (URL or screenshot path) and `task`.

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

and then I installed in editable mode:

```bash
uv pip install -e .
```

So now both `swarm` and `ux-swarm` work anywhere in the terminal. Editable mode means changes to `main.py` take effect immediately without reinstalling.

Examples:

```bash
swarm https://example.com "complete the checkout flow"
swarm screenshot.png "find and submit the contact form"
```

## Build Backend

We need to have this project be a distributable package. The build backend is responsible for that. Traditionally this was done by Hatchling or other tools, but UV now ships its own native build backend (`uv_build`).

uv's default convention for installable packages with CLI entrypoints is `src/<package>` — it's what `uv init --package` generates out of the box. We restructured the folder structure to follow that convention.

1. We added a build system block to `pyproject.toml` and updated the entry points to reflect the new location:

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

2. We moved main.py and added `__init__.py`:

```
main.py  →  src/ux_swarm/main.py
```

The naming feels weird because Python has two conventions that don't match: Folders and imports use underscores (`ux_swarm`), install names and CLI commands use hyphens (`ux-swarm`, `swarm`)

## Recap - Done with Scaffolding

- `UV` is set up for package management
- `Click` is wired up
- `run` command is working implicitly, no need to type it in
- We have a few argument surfaces defined: `url`, `task`, plus `--users`, `--max-steps`, `--viewport`, `--verbose`
- `swarm` and `ux-swarm` are installed as real CLI commands via `pyproject.toml` + `uv pip install -e .`
- Folder structure is aligned with uv best practices

## Pydantic and BaseModel

I installed Pydantic with `uv add pydantic` because the app receives responses from LLM models and external APIs — data we don't control. Pydantic catches malformed or unexpected types at runtime, at the boundary where that data enters the system.

`BaseModel` is the type blueprint. Each field and its type is declared at runtime, then Pydantic enforces them at the moment an object is created.

Pydantic library provides `model_validate()` for parsing raw JSON into a proper Python object, and `model_json_schema()` for sending the schema to the LLM so it knows exactly what shape to return.

The data shapes will evolve and that's ok. What matters now is that we now have the "three systems of governance" in place - the docs, the models, and the code. Each one describes the same thing at a different level. We'll eventually add tests so all three stay in sync.

Models were created in `src/ux_swarm/models.py`


## Config Wizard

Started with adding two dependencies: **Rich** for stylized terminal output (colored text, spinner, etc) and **Playwright** so we can run checks on teh Chromium browser binary is already installed, and becuase it;s used during `run`


