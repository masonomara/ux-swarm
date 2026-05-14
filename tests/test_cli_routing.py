import click
import pytest
from click.testing import CliRunner
from unittest.mock import patch

from ux_swarm.cli import CliError, SmartGroup, _fmt_meta, _opt_sort_key, _parse_arg_rows
from ux_swarm.cli import cli


# --- _opt_sort_key ---

def test_opt_sort_key_extracts_long_flag():
    assert _opt_sort_key(("--users INTEGER", "Number of users")) == "users"


def test_opt_sort_key_short_and_long_flag():
    assert _opt_sort_key(("-u, --users INTEGER", "Number of users")) == "users"


def test_opt_sort_key_no_flag_returns_full_key():
    assert _opt_sort_key(("something", "description")) == "something"


def test_opt_sort_key_hyphenated_flag():
    assert _opt_sort_key(("--max-steps INTEGER", "desc")) == "max-steps"


# --- CliError ---

def test_cli_error_show_includes_message():
    @click.command()
    def cmd():
        raise CliError("something went wrong")

    result = CliRunner().invoke(cmd, color=False)
    assert "Error: something went wrong" in result.output


def test_cli_error_show_exits_nonzero():
    @click.command()
    def cmd():
        raise CliError("oops")

    result = CliRunner().invoke(cmd)
    assert result.exit_code != 0


# --- _fmt_meta ---

def test_fmt_meta_converts_all_caps():
    assert _fmt_meta("TARGET") == "<target>"


def test_fmt_meta_converts_word_in_context():
    assert _fmt_meta("INTEGER") == "<integer>"


def test_fmt_meta_preserves_lowercase():
    assert _fmt_meta("target") == "target"


def test_fmt_meta_mixed_line():
    assert _fmt_meta("--users INTEGER") == "--users <integer>"


def test_fmt_meta_multiple_metavars():
    assert _fmt_meta("FILE URL") == "<file> <url>"


def test_fmt_meta_already_bracketed_unchanged():
    assert _fmt_meta("<target>") == "<target>"


# --- _parse_arg_rows ---

def test_parse_arg_rows_basic():
    block = "<target>  url or image path\n<task>    goal for agents"
    rows = _parse_arg_rows(block)
    assert rows == [("<target>", "url or image path"), ("<task>", "goal for agents")]


def test_parse_arg_rows_empty():
    assert _parse_arg_rows("") == []


def test_parse_arg_rows_continuation_line():
    block = '<target>  first line\n    continuation'
    rows = _parse_arg_rows(block)
    assert rows[0][1] == "first line continuation"


def test_parse_arg_rows_skips_escape_lines():
    block = "\b\n<target>  value"
    rows = _parse_arg_rows(block)
    assert rows == [("<target>", "value")]


# --- SmartGroup._try_reconstruct ---

@pytest.fixture
def group():
    g = SmartGroup(name="swarm")
    return g


def test_reconstruct_https_url(group):
    result = group._try_reconstruct(["https://example.com", "find", "the", "login"])
    assert result == ["run", "https://example.com", "find the login"]


def test_reconstruct_http_url(group):
    result = group._try_reconstruct(["http://localhost:3000", "sign", "up"])
    assert result == ["run", "http://localhost:3000", "sign up"]


def test_reconstruct_bare_domain(group):
    result = group._try_reconstruct(["example.com", "find", "pricing"])
    assert result == ["run", "example.com", "find pricing"]


def test_reconstruct_bare_domain_with_port(group):
    result = group._try_reconstruct(["localhost:3000", "log", "in"])
    assert result == ["run", "localhost:3000", "log in"]


def test_reconstruct_image_path(group):
    result = group._try_reconstruct(["screenshot.png", "check", "the", "button"])
    assert result == ["run", "screenshot.png", "check the button"]


def test_reconstruct_all_image_extensions(group):
    for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]:
        result = group._try_reconstruct([f"shot{ext}", "find", "nav"])
        assert result is not None
        assert result[0] == "run"
        assert result[1] == f"shot{ext}"


def test_reconstruct_image_case_insensitive(group):
    result = group._try_reconstruct(["shot.PNG", "task"])
    assert result == ["run", "shot.PNG", "task"]


def test_reconstruct_flags_preserved(group):
    result = group._try_reconstruct(["example.com", "find", "nav", "--users", "5"])
    assert result == ["run", "example.com", "find nav", "--users", "5"]


def test_reconstruct_short_flag_preserved(group):
    result = group._try_reconstruct(["example.com", "task", "-u", "10"])
    assert result == ["run", "example.com", "task", "-u", "10"]


def test_reconstruct_no_task_returns_none(group):
    assert group._try_reconstruct(["example.com"]) is None


def test_reconstruct_no_task_with_flags_only_returns_none(group):
    assert group._try_reconstruct(["example.com", "--users", "5"]) is None


def test_reconstruct_unknown_arg_returns_none(group):
    assert group._try_reconstruct(["notaurl", "task"]) is None


def test_reconstruct_image_with_spaces_in_path(group):
    result = group._try_reconstruct(["my", "screenshot.png", "find", "nav"])
    assert result == ["run", "my screenshot.png", "find nav"]


# --- SmartGroup.parse_args end-to-end routing ---

def test_parse_args_routes_url_to_run_command():
    """A bare URL as the first argument should be routed to the run subcommand."""
    with patch("ux_swarm.cli.load_config", return_value={}):
        result = CliRunner().invoke(cli, ["example.com", "find", "the", "login"])
    # If routing failed Click would say "No such command 'example.com'" → exit code 2
    assert result.exit_code != 2, result.output


def test_parse_args_routes_https_url_to_run_command():
    with patch("ux_swarm.cli.load_config", return_value={}):
        result = CliRunner().invoke(cli, ["https://example.com", "sign", "up"])
    assert result.exit_code != 2, result.output


def test_parse_args_routes_image_path_to_run_command(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"")
    with patch("ux_swarm.cli.load_config", return_value={}):
        result = CliRunner().invoke(cli, [str(img), "find", "nav"])
    assert result.exit_code != 2, result.output
