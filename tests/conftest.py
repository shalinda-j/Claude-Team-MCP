"""Shared pytest fixtures.

Every test runs against a fresh temp directory: all of the module's path
globals (team state, lock, backups, brain, gateway, vault, adapters) are
monkeypatched per-test so the suite never touches a developer's real state
files and tests are fully isolated from each other.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Make the repo root importable no matter where pytest is invoked from.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Point the import-time defaults at a throwaway dir as a belt-and-braces
# guard (the per-test fixture below overrides them anyway).
_IMPORT_TMP = tempfile.mkdtemp(prefix="claude-team-mcp-tests-")
os.environ.setdefault("TEAM_STATE_FILE", str(Path(_IMPORT_TMP) / "state.json"))
os.environ.setdefault("BRAIN_DIR", str(Path(_IMPORT_TMP) / "brain"))
os.environ.setdefault("TEAM_BACKUP_DIR", str(Path(_IMPORT_TMP) / "backups"))
os.environ.setdefault("GATEWAY_FILE", str(Path(_IMPORT_TMP) / "gateway.json"))
os.environ.setdefault("GATEWAY_VAULT_FILE", str(Path(_IMPORT_TMP) / "vault.json"))
os.environ.setdefault("ADAPTER_DIR", str(Path(_IMPORT_TMP) / "adapters"))

import team_coordinator as tc  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Redirect every persistent path to a per-test temp dir."""
    state_file = tmp_path / "state.json"
    brain_dir = tmp_path / "brain"
    monkeypatch.setattr(tc, "STATE_FILE", state_file)
    monkeypatch.setattr(tc, "LOCK_FILE", tmp_path / "state.json.lock")
    monkeypatch.setattr(tc, "BACKUP_DIR", tmp_path / "backups")
    monkeypatch.setattr(tc, "BRAIN_DIR", brain_dir)
    monkeypatch.setattr(tc, "BRAIN_FILE", brain_dir / "brain.json")
    monkeypatch.setattr(tc, "BRAIN_LOCK", brain_dir / "brain.json.lock")
    monkeypatch.setattr(tc, "GATEWAY_FILE", tmp_path / "gateway.json")
    monkeypatch.setattr(tc, "GATEWAY_LOCK", tmp_path / "gateway.json.lock")
    monkeypatch.setattr(tc, "GATEWAY_VAULT", tmp_path / "gateway_vault.json")
    monkeypatch.setattr(tc, "GATEWAY_VAULT_LOCK", tmp_path / "gateway_vault.json.lock")
    monkeypatch.setattr(tc, "ADAPTER_DIR", tmp_path / "adapters")
    tc._GW_RATE.clear()
    yield tc


@pytest.fixture
def team(isolated_state):
    """A small team already joined: PM, Backend, QA."""
    tc.join_team("PM", "Pat")
    tc.join_team("Backend", "Ben")
    tc.join_team("QA", "Quinn")
    return isolated_state
