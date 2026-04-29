import pytest

import ux_swarm.config as cfg
import ux_swarm.main as main_mod
import ux_swarm.personas as personas_mod


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Redirect all config and data paths to tmp_path so tests never touch real files."""
    monkeypatch.setattr(cfg, "LOCAL_CONFIG", tmp_path / "local.json")
    monkeypatch.setattr(cfg, "GLOBAL_CONFIG", tmp_path / "global.json")
    monkeypatch.setattr(main_mod, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main_mod, "RESULTS_JSON", tmp_path / "results.json")
    monkeypatch.setattr(personas_mod, "USERS_JSON", tmp_path / "users.json")
    monkeypatch.setattr(personas_mod, "LOCAL_DIR", tmp_path)
    return tmp_path
