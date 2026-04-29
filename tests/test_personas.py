import json

import pytest

import ux_swarm.personas as personas_mod
from ux_swarm.cli import CliError
from ux_swarm.models import UserType
from ux_swarm.personas import DEFAULT_USERS, distribute_users, load_users, write_default_users


def test_load_users_no_file_returns_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(personas_mod, "USERS_JSON", tmp_path / "users.json")
    assert load_users() == list(DEFAULT_USERS)


def test_load_users_empty_list_returns_defaults(monkeypatch, tmp_path):
    users_json = tmp_path / "users.json"
    users_json.write_text("[]")
    monkeypatch.setattr(personas_mod, "USERS_JSON", users_json)
    assert load_users() == list(DEFAULT_USERS)


def test_load_users_valid_file(monkeypatch, tmp_path):
    users_json = tmp_path / "users.json"
    data = [{"label": "Power User", "weight": 2.0, "description": "Expert user who reads everything"}]
    users_json.write_text(json.dumps(data))
    monkeypatch.setattr(personas_mod, "USERS_JSON", users_json)
    result = load_users()
    assert len(result) == 1
    assert result[0].label == "Power User"
    assert result[0].weight == 2.0


def test_load_users_invalid_json_raises(monkeypatch, tmp_path):
    users_json = tmp_path / "users.json"
    users_json.write_text("not valid json {{{")
    monkeypatch.setattr(personas_mod, "USERS_JSON", users_json)
    with pytest.raises(CliError, match="users.json is invalid"):
        load_users()


def test_load_users_invalid_schema_raises(monkeypatch, tmp_path):
    users_json = tmp_path / "users.json"
    users_json.write_text(json.dumps([{"bad_field": "no label or weight"}]))
    monkeypatch.setattr(personas_mod, "USERS_JSON", users_json)
    with pytest.raises(CliError, match="users.json is invalid"):
        load_users()


def test_distribute_users_length_equals_n():
    users = [
        UserType(label="A", weight=1.0, description="a"),
        UserType(label="B", weight=1.0, description="b"),
    ]
    for n in [1, 5, 10, 13, 100]:
        assert len(distribute_users(users, n)) == n


def test_distribute_users_equal_weights():
    users = [
        UserType(label="A", weight=1.0, description="a"),
        UserType(label="B", weight=1.0, description="b"),
    ]
    result = distribute_users(users, 10)
    assert result.count(users[0]) == 5
    assert result.count(users[1]) == 5


def test_distribute_users_proportional_weights():
    users = [
        UserType(label="A", weight=3.0, description="a"),
        UserType(label="B", weight=1.0, description="b"),
    ]
    result = distribute_users(users, 8)
    assert result.count(users[0]) == 6
    assert result.count(users[1]) == 2


def test_distribute_users_undershoot_pads_with_first():
    # weight=0.01 for B causes round() to give 0, but max(1,...) gives 1
    # overshoot is trimmed, undershoot is padded from users[0]
    users = [
        UserType(label="A", weight=1.0, description="a"),
        UserType(label="B", weight=0.001, description="b"),
    ]
    result = distribute_users(users, 10)
    assert len(result) == 10
    assert all(u in users for u in result)


def test_distribute_users_overshoot_trims_to_n():
    users = [UserType(label=str(i), weight=1.0, description="x") for i in range(5)]
    result = distribute_users(users, 3)
    assert len(result) == 3


def test_default_user_label_and_weight():
    user = DEFAULT_USERS[0]
    assert user.label == "Default User"
    assert user.weight == 1.0


def test_default_user_has_non_empty_description():
    assert DEFAULT_USERS[0].description.strip()


def test_write_default_users_creates_file(monkeypatch, tmp_path):
    monkeypatch.setattr(personas_mod, "USERS_JSON", tmp_path / "users.json")
    monkeypatch.setattr(personas_mod, "LOCAL_DIR", tmp_path)
    path = write_default_users()
    assert path.exists()


def test_write_default_users_valid_json(monkeypatch, tmp_path):
    monkeypatch.setattr(personas_mod, "USERS_JSON", tmp_path / "users.json")
    monkeypatch.setattr(personas_mod, "LOCAL_DIR", tmp_path)
    path = write_default_users()
    data = json.loads(path.read_text())
    assert isinstance(data, list)
    assert data[0]["label"] == "Default User"


def test_write_default_users_trailing_newline(monkeypatch, tmp_path):
    monkeypatch.setattr(personas_mod, "USERS_JSON", tmp_path / "users.json")
    monkeypatch.setattr(personas_mod, "LOCAL_DIR", tmp_path)
    path = write_default_users()
    assert path.read_text().endswith("\n")
