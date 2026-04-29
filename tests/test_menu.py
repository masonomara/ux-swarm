from unittest.mock import patch

import pytest

from ux_swarm.menu import GoBack, navigate, select


def test_navigate_up():
    assert navigate("\x1b[A", 3, 5) == 2


def test_navigate_down():
    assert navigate("\x1b[B", 2, 5) == 3


def test_navigate_up_clamps_at_zero():
    assert navigate("\x1b[A", 0, 5) == 0


def test_navigate_down_clamps_at_max():
    assert navigate("\x1b[B", 5, 5) == 5


def test_navigate_unknown_key_unchanged():
    assert navigate("x", 2, 5) == 2


def test_navigate_alt_up_escape_sequence():
    assert navigate("\x1bOA", 3, 5) == 2


def test_navigate_alt_down_escape_sequence():
    assert navigate("\x1bOB", 2, 5) == 3


def test_navigate_unrelated_escape_unchanged():
    assert navigate("\x1b[C", 2, 5) == 2


# --- select() ---

_OPTIONS = ["Apple", "Banana", "Cherry"]


def test_select_returns_first_option_on_enter():
    with patch("click.getchar", return_value="\r"):
        result = select("Choose:", _OPTIONS)
    assert result == "Apple"


def test_select_down_arrow_then_enter():
    with patch("click.getchar", side_effect=["\x1b[B", "\r"]):
        result = select("Choose:", _OPTIONS)
    assert result == "Banana"


def test_select_default_index_respected():
    with patch("click.getchar", return_value="\r"):
        result = select("Choose:", _OPTIONS, default_index=2)
    assert result == "Cherry"


def test_select_escape_raises_go_back():
    with patch("click.getchar", return_value="\x1b"):
        with pytest.raises(GoBack):
            select("Choose:", _OPTIONS)


def test_select_ctrl_c_raises_system_exit_130():
    with patch("click.getchar", return_value="\x03"):
        with pytest.raises(SystemExit) as exc_info:
            select("Choose:", _OPTIONS)
    assert exc_info.value.code == 130
