"""The `doctor` self-check: what it reports, and when it escalates to a warning
or a failure."""

import collections
import os
import sys

import pytest

from conftest import OPERATOR_TOKEN
import team_coordinator as tc


def _rows_by_label(rows):
    return {label: (status, detail) for status, label, detail in rows}


# --- Happy path ---

def test_reports_the_core_environment():
    rows = _rows_by_label(tc._doctor_rows())
    assert "Python" in rows and "mcp SDK" in rows and "State file" in rows
    # The shim's chosen import path is named, since that is the first thing to
    # check when the server won't start.
    assert tc._MCP_API in rows["mcp SDK"][1]


def test_clean_environment_passes():
    text = tc._doctor_text()
    assert "[FAIL]" not in text
    assert "All checks passed." in text or "warning(s) worth a look" in text


def test_doctor_tool_matches_the_report():
    assert tc.doctor() == tc._doctor_text()


def test_paths_point_at_the_isolated_temp_dir(tmp_path):
    rows = _rows_by_label(tc._doctor_rows())
    assert str(tc.STATE_FILE) in rows["State file"][1]
    assert str(tc.BRAIN_DIR) in rows["Second brain"][1]


# --- Failures ---

def test_unwritable_state_path_fails(monkeypatch, tmp_path):
    blocked = tmp_path / "nonexistent-device" / "state.json"
    monkeypatch.setattr(tc, "STATE_FILE", blocked)
    monkeypatch.setattr(tc, "_dr_writable", lambda p: "Read-only file system"
                        if p == blocked or p == blocked.parent else "")
    rows = _rows_by_label(tc._doctor_rows())
    assert rows["State file"][0] == tc._FAIL
    assert "TEAM_STATE_FILE" in rows["State file"][1]


def test_failures_are_summarised_and_exit_nonzero(monkeypatch):
    monkeypatch.setattr(tc, "_dr_writable", lambda p: "Read-only file system")
    text = tc._doctor_text()
    assert "[FAIL]" in text
    assert "problem(s)" in text


def test_old_python_is_flagged(monkeypatch):
    fake = collections.namedtuple(
        "version_info", "major minor micro releaselevel serial")(3, 9, 0, "final", 0)
    monkeypatch.setattr(sys, "version_info", fake)
    rows = _rows_by_label(tc._doctor_rows())
    assert rows["Python"][0] == tc._FAIL
    assert "3.10+" in rows["Python"][1]


# --- Warnings ---

def test_missing_filelock_warns(monkeypatch):
    monkeypatch.setattr(tc, "_HAS_FILELOCK", False)
    rows = _rows_by_label(tc._doctor_rows())
    assert rows["filelock"][0] == tc._WARN
    assert "pip install filelock" in rows["filelock"][1]


def test_allow_private_warns(monkeypatch):
    monkeypatch.setattr(tc, "GATEWAY_ALLOW_PRIVATE", True)
    rows = _rows_by_label(tc._doctor_rows())
    assert rows["SSRF guard"][0] == tc._WARN


def test_default_ssrf_mode_is_reported_as_ok():
    rows = _rows_by_label(tc._doctor_rows())
    assert rows["SSRF guard"][0] == tc._OK
    assert "refused" in rows["SSRF guard"][1]


def test_allowlist_is_shown(monkeypatch):
    monkeypatch.setattr(tc, "GATEWAY_ALLOWED_HOSTS", ["api.stripe.com"])
    rows = _rows_by_label(tc._doctor_rows())
    assert "api.stripe.com" in rows["SSRF guard"][1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_world_readable_vault_warns():
    tc.gateway_set_credential("k", "s3cret", targets="*", operator_token=OPERATOR_TOKEN)
    tc.GATEWAY_VAULT.chmod(0o644)
    rows = _rows_by_label(tc._doctor_rows())
    assert rows["Vault perms"][0] == tc._WARN
    assert "chmod 600" in rows["Vault perms"][1]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_owner_only_vault_passes():
    tc.gateway_set_credential("k", "s3cret", targets="*", operator_token=OPERATOR_TOKEN)
    tc.GATEWAY_VAULT.chmod(0o600)
    rows = _rows_by_label(tc._doctor_rows())
    assert rows["Vault perms"][0] == tc._OK


def test_vault_secrets_are_never_printed():
    tc.gateway_set_credential("stripe_key", "sk_live_TOPSECRET", targets="*",
                            operator_token=OPERATOR_TOKEN)
    assert "sk_live_TOPSECRET" not in tc._doctor_text()


@pytest.mark.skipif(sys.platform == "win32", reason="the stray path is a Windows drive letter")
def test_legacy_brain_directory_is_flagged(monkeypatch, tmp_path):
    # Before v8.3 the second brain defaulted to the literal "D:/mcp/second_brain"
    # on every platform, so notes landed in a folder named "D:" under the cwd.
    monkeypatch.chdir(tmp_path)
    (tmp_path / "D:" / "mcp" / "second_brain").mkdir(parents=True)
    rows = _rows_by_label(tc._doctor_rows())
    assert rows["Legacy path"][0] == tc._WARN
    assert str(tc.BRAIN_DIR) in rows["Legacy path"][1]


@pytest.mark.skipif(sys.platform == "win32", reason="the stray path is a Windows drive letter")
def test_no_legacy_warning_when_absent(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert "Legacy path" not in _rows_by_label(tc._doctor_rows())


# --- Writability probe ---

def test_writable_probe_accepts_a_writable_dir(tmp_path):
    assert tc._dr_writable(tmp_path / "state.json") == ""
    assert not (tmp_path / ".claude_team_write_probe").exists()  # cleans up after itself


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_writable_probe_reports_a_read_only_dir(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    try:
        assert tc._dr_writable(ro) != ""
    finally:
        ro.chmod(0o700)
