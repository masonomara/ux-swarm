import itertools
import shutil
import subprocess
import sys
import threading

import click

from ux_swarm.config import (
    PROVIDERS,
    ProviderAuthError,
    console,
    fetch_provider_models,
    playwright_state,
    save_config,
)
from ux_swarm.menu import GoBack, select


# uses sys.stdout directly; Rich's Console would desync cursor tracking
class _Spinner:
    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, msg: str) -> None:
        self._msg = msg
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "_Spinner":
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join()
        sys.stdout.write("\r\x1b[2K")
        sys.stdout.flush()

    def _run(self) -> None:
        for frame in itertools.cycle(self._FRAMES):
            if self._stop.wait(0.08):
                break
            sys.stdout.write(f"\r\033[36m{frame}\033[0m {self._msg}")
            sys.stdout.flush()


def _install_playwright() -> None:
    with console.status("Installing Playwright…", spinner_style="cyan"):
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "playwright"],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        console.print("[red]Playwright install failed.[/]")
        if stderr := result.stderr.strip():
            console.print(f"[dim]{stderr}[/]")


def _install_chromium() -> None:
    with console.status("Installing Chromium…", spinner_style="cyan"):
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
        )
    if result.returncode != 0:
        console.print("[red]Chromium install failed.[/]")
        if stderr := result.stderr.strip():
            console.print(f"[dim]{stderr}[/]")
        console.print("[dim]Run: playwright install chromium[/]")


def _wizard_step_provider(state: dict) -> None:
    default = next(
        (i for i, p in enumerate(PROVIDERS)
         if p["key"] == state.get("provider_key")),
        0,
    )
    chosen_name = select("[green]?[/] [bold]Select an LLM provider:[/]",
                         [p["name"] for p in PROVIDERS],
                         default_index=default)
    provider = next(p for p in PROVIDERS if p["name"] == chosen_name)
    state["provider_key"] = provider["key"]
    state["provider_name"] = provider["name"]


def _wizard_step_api_key(state: dict) -> None:
    lines_to_erase = 0
    error_message: str | None = None

    while True:
        if lines_to_erase:
            sys.stdout.write(f"\x1b[{lines_to_erase}A\x1b[J")
            sys.stdout.flush()

        if error_message:
            console.print(f"[red]{error_message}[/]")
            console.print()
        extra_lines = 2 if error_message else 0

        prompt_text = (
            f"\033[32m?\033[0m \033[1mEnter your {state['provider_name']} API key:\033[0m"
            f" \033[2minput is hidden\033[0m")
        try:
            api_key = click.prompt(prompt_text,
                                   default="",
                                   hide_input=True,
                                   prompt_suffix=" ",
                                   show_default=False).strip()
        except click.exceptions.Abort:
            console.print("\nAborted\n")
            raise SystemExit(130)

        visible_label = f"? Enter your {state['provider_name']} API key: input is hidden "
        cols = shutil.get_terminal_size().columns
        prompt_lines = max(1, (len(visible_label) + cols - 1) // cols)

        if not api_key:
            error_message = "API key is required."
            lines_to_erase = prompt_lines + extra_lines
            continue

        sys.stdout.write("\n")
        sys.stdout.flush()
        try:
            with _Spinner(f"Checking your {state['provider_name']} API key…"):
                model_options = fetch_provider_models(state["provider_key"],
                                                      api_key)
            sys.stdout.write(f"\x1b[{extra_lines + prompt_lines + 1}A\x1b[J")
            sys.stdout.flush()
            console.print(
                f"[green]?[/] [bold]Enter your {state['provider_name']} API key:[/] [cyan]input is hidden[/]\n",
                highlight=False)
            state["api_key"] = api_key
            state["model_options"] = model_options
            return
        except ProviderAuthError:
            error_message = "Invalid API key, please try again."
            lines_to_erase = prompt_lines + extra_lines + 1
        except Exception as exc:
            console.print(
                f"[red]Could not reach {state['provider_name']} API.[/]")
            console.print(f"[dim]{exc}[/]")
            console.print("[dim]Check your network and try again.[/]")
            raise SystemExit(1)


def _wizard_step_model(state: dict) -> None:
    options = state["model_options"]
    default = next(
        (i for i, m in enumerate(options) if m == state.get("model")),
        0,
    )
    state["model"] = select("[green]?[/] [bold]Select your LLM model:[/]",
                            options,
                            default_index=default)


def _wizard_step_playwright(state: dict) -> None:
    playwright_ok, chromium_ok = playwright_state()

    if playwright_ok and chromium_ok:
        console.print(
            "[green]?[/] [bold]Install Chromium:[/] [cyan]Installed[/]\n",
            highlight=False,
        )
        state["playwright_ok"] = True
        return

    console.print("ux-swarm requires Chromium to work.\n", highlight=False)

    if not playwright_ok:
        choice = select("[green]?[/] [bold]Install Playwright:[/]",
                        ["Proceed", "Cancel"],
                        echo=False)
        if choice == "Cancel":
            console.print(
                "[green]?[/] [bold]Install Playwright:[/] [dim]skipped[/]\n",
                highlight=False,
            )
            state["playwright_ok"] = False
            return
        console.print(
            "[green]?[/] [bold]Install Playwright:[/] [cyan]Proceed[/]\n",
            highlight=False,
        )
        _install_playwright()
        _, chromium_ok = playwright_state()

    if not chromium_ok:
        choice = select("[green]?[/] [bold]Install Chromium:[/]",
                        ["Proceed", "Cancel"],
                        echo=False)
        if choice == "Cancel":
            console.print(
                "[green]?[/] [bold]Install Chromium:[/] [dim]skipped[/]\n",
                highlight=False,
            )
            state["playwright_ok"] = False
            return
        console.print(
            "[green]?[/] [bold]Install Chromium:[/] [cyan]Proceed[/]\n",
            highlight=False,
        )
        _install_chromium()
        state["playwright_ok"] = True
    else:
        state["playwright_ok"] = True


def run_config_wizard() -> None:
    state: dict = {}
    steps = [
        _wizard_step_provider,
        _wizard_step_api_key,
        _wizard_step_model,
        _wizard_step_playwright,
    ]
    i = 0
    while i < len(steps):
        try:
            steps[i](state)
            i += 1
        except GoBack:
            i = max(0, i - 1)

    saved_path = save_config({
        "provider": state["provider_key"],
        "api_key": state["api_key"],
        "model": state["model"],
    })
    console.print(
        f"[green]✓[/] Setup wizard complete. Config saved: [bold][underline]{saved_path}[/][/]\n"
    )
