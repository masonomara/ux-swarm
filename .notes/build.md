## Library of resources

https://medium.com/@hitorunajp/poetry-vs-uv-which-python-package-manager-should-you-use-in-2025-4212cb5e0a14

https://www.youtube.com/watch?v=FWacanslfFM

## Creating UX Swarm

First thing I did was install UV, I wanted a package manager and version control tool, at first aI looked at poetry but UV seemed to do everything faster, the head of peotry actually let the project so I was worried about maintenance, general reviews of UV seemed pretty great, and the tools were all simply mapped as noted below. It also worked with click, which I wanted to use to keep the argument, commands, subcommands conventions I had set up during my ux swarm mvp

### Poetry to UV cheatsheet:

Poetry -> UV
Poetry new pyproject -> Uv init myproject
Poetry add click -> Uv add click
Poetry add —group dev pytest -> Uv add --dev pytest
poetry remove click -> uv remove click
poetry install -> Uv sync
poetry run python script.py -> uv run python script.py
poetry shell source .venv/bin/activate (uv doesn't have a shell command, you just activate the venv directly)
poetry lock -> Uv lock
Poetry build -> Uv build
Poetry publish -> Uv publish

### Setting up UV

So so far, we have **click** and **uv** as our tools

Hers the setup workflow:

**1. Initializing a new project**

```bash
uv init
```

**2. Add dependencies**

```bash
uv add requests
```

**3. Install dependencies (super fast, cached)**

```bash
uv sync
```

**4. Run inside environment**

```bash
uv run main.py
```

Note, somehtign dfferetn abotu UV from other projects, the virtual environment in in .venv, its not autorunning whenever im doign developing, dependencies are automatically put into .venv and then when i run the command through uv it autmacally runs it from the venv. keeps it seerated, which I like.

## Click COnventions

lets run :

```bash
uv add click
```

I added conventiosn to CLAUDE.md based on Simon Willison;s [blog post](https://simonwillison.net/2023/Sep/30/cli-tools-python/) for my own notes anduseful for claude to know. I have not yet nstalled click, but I am committed to using it/

**Arguments** are good for happy paths, thigns that every run needs to run, for my build, the arguments will be items like the target (URL or screenshot path) and the task. so my argmeuents are target and task.

ux-swarm [target] [task]

ex.
ux-swarm https://example.com "complete the checkout flow"
ux-swarm screenshot.png "find and submit the contact form"

**Options** re good for modifying behavior and defaults
