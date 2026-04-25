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

## Recap — Done with Scaffolding

- UV is set up for package management
- Click is wired up
- `run` command is working implicitly — bare URLs/image paths route to it without typing `run`
- Argument surface defined: `url`, `task`, plus `--users`, `--max-steps`, `--viewport`, `--verbose`
- `swarm` and `ux-swarm` are installed as real CLI commands via `pyproject.toml` + `uv pip install -e .`

## Recap - Done with Scaffolding

- `UV` is set up for package management
- `Click` is wired up
- `run` command is working implicitly, no need to type it in
- We have a few argument surfaces defined: `url`, `task`, plus `--users`, `--max-steps`, `--viewport`, `--verbose`
- `swarm` and `ux-swarm` are installed as real CLI commands via `pyproject.toml` + `uv pip install -e .`
