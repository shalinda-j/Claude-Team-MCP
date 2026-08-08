"""State layer: defaults, atomic writes, rotation, backups, restore, reset."""

import json
import sys
from pathlib import Path

import pytest

import team_coordinator as tc


@pytest.mark.skipif(sys.platform == "win32", reason="D: is a real drive here")
def test_second_brain_default_is_not_a_windows_drive_letter():
    # Before v8.3 this defaulted to the literal "D:/mcp/second_brain" on every
    # platform, which created a directory named "D:" wherever the server was
    # started from instead of a real home-relative path.
    default = Path(tc._DEFAULT_BRAIN)
    assert "D:" not in default.parts
    assert default.is_absolute()
    assert default.is_relative_to(Path.home())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only default")
def test_second_brain_default_on_windows_keeps_the_shared_drive_path():
    assert tc._DEFAULT_BRAIN == "D:/mcp/second_brain"


def test_state_and_brain_defaults_agree_on_platform_style():
    # Both paths are platform-aware, so neither should be absolute-Windows on
    # POSIX or home-relative on Windows.
    assert (Path(tc._DEFAULT_STATE).drive == "") == (Path(tc._DEFAULT_BRAIN).drive == "")


def test_default_state_has_all_keys():
    state = tc._default_state()
    for key in ("agents", "messages", "tasks", "notes", "facts", "summaries",
                "activity_log", "spawned", "debate", "archived_messages",
                "read_state", "skills", "templates", "findings"):
        assert key in state


def test_atomic_write_creates_parents_and_content(tmp_path):
    target = tmp_path / "deep" / "nested" / "out.json"
    tc._atomic_write(target, '{"ok": true}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    # No stray .tmp files left behind.
    assert not list(target.parent.glob("*.tmp"))


def test_read_state_survives_corrupt_file():
    tc.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tc.STATE_FILE.write_text("{not valid json", encoding="utf-8")
    state = tc._read_state_unlocked()
    assert state["messages"] == []
    assert state["agents"] == {}


def test_read_state_backfills_missing_keys():
    tc.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tc.STATE_FILE.write_text(json.dumps({"messages": [{"from": "x", "text": "hi", "time": "0"}]}),
                             encoding="utf-8")
    state = tc._read_state_unlocked()
    assert len(state["messages"]) == 1
    assert "findings" in state and "templates" in state


def test_message_rotation(monkeypatch):
    monkeypatch.setattr(tc, "MSG_ROTATE_LIMIT", 10)
    state = tc._default_state()
    state["messages"] = [{"from": "a", "text": str(i), "time": "0"} for i in range(25)]
    tc._save(state)
    reloaded = tc._load()
    assert len(reloaded["messages"]) == 10
    assert reloaded["archived_messages"] == 15
    assert reloaded["messages"][-1]["text"] == "24"


def test_mutate_persists_result():
    def op(state):
        state["facts"]["lang"] = "python"
        return "done"
    assert tc._mutate(op) == "done"
    assert tc._load()["facts"]["lang"] == "python"


def test_backup_now_and_list_backups():
    tc.post_message("PM", "before backup")
    out = tc.backup_now(by_role="PM")
    assert "Backup saved" in out
    assert len(tc._list_backups()) >= 1
    assert "state_" in tc.list_backups()


def test_restore_backup_round_trip():
    tc.post_message("PM", "first message")
    tc.backup_now()
    tc.post_message("PM", "second message")
    assert len(tc._load()["messages"]) == 2
    out = tc.restore_backup(index=0)
    assert "Restored team state" in out
    msgs = tc._load()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["text"] == "first message"


def test_restore_backup_without_backups():
    assert "No backups available" in tc.restore_backup()


def test_reset_team_full():
    tc.join_team("PM")
    tc.save_note("remember me", by_role="PM")
    tc.reset_team(keep_memory=False)
    state = tc._load()
    assert state["agents"] == {}
    assert state["notes"] == []


def test_reset_team_keep_memory():
    tc.join_team("PM")
    tc.save_note("remember me", by_role="PM")
    tc.reset_team(keep_memory=True)
    state = tc._load()
    assert state["agents"] == {}
    assert state["messages"] == []
    assert len(state["notes"]) == 1
