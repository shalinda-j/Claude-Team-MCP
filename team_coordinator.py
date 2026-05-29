#!/usr/bin/env python3
"""
Team Coordinator MCP Server (v6 - production-ready, multi-CLI)
=============================================================
Multiple AI coding agents -- across Claude Code, Cursor, Codex CLI, Gemini CLI,
or any MCP-compatible client -- coordinate like an SOP team, debate to reach the
right plan, share persistent project memory, spawn/close their own terminals,
and keep a self-contained "second brain" knowledge graph.

v1: shared channel + task board
v2: wait_for_message (live conversation, no manual polling)
v3: project memory -> notes, facts, summaries, activity log
v4: auto-spawn terminals (Windows) + self-contained second brain on D:\\
v5: structured debate (propose -> critique -> revise -> judge)
v6: file locking + atomic writes (safe concurrent multi-CLI access),
    stale-agent cleanup, activity-log rotation, shared-path default.

CROSS-CLI: every client that points at the SAME state file shares one world.
Default is a shared absolute path so mixing tools "just works":
    set TEAM_STATE_FILE=D:\\mcp\\shared_state.json   (recommended for multi-CLI)

Add to Claude Code:
    claude mcp add team -s user -- env TEAM_STATE_FILE=D:\\mcp\\shared_state.json python D:\\mcp\\team_coordinator.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# Optional robust file locking. Falls back gracefully if not installed.
try:
    from filelock import FileLock, Timeout as LockTimeout
    _HAS_FILELOCK = True
except Exception:
    _HAS_FILELOCK = False

mcp = FastMCP("team-coordinator")

# --- Shared team/memory state ---
# Default to a shared absolute path so multiple CLIs/IDEs converge on one world.
_DEFAULT_STATE = "D:/mcp/shared_state.json" if sys.platform == "win32" else str(Path.home() / ".claude_team_state.json")
STATE_FILE = Path(os.environ.get("TEAM_STATE_FILE", _DEFAULT_STATE))
LOCK_FILE = Path(str(STATE_FILE) + ".lock")

# --- Second brain storage (self-contained, default on D:\) ---
BRAIN_DIR = Path(os.environ.get("BRAIN_DIR", "D:/mcp/second_brain"))
BRAIN_FILE = BRAIN_DIR / "brain.json"
BRAIN_LOCK = Path(str(BRAIN_FILE) + ".lock")

# --- Auto-spawn safety limit (override with MAX_AGENTS env var) ---
MAX_AGENTS = int(os.environ.get("MAX_AGENTS", "6"))

# --- Tuning ---
AGENT_STALE_SECONDS = int(os.environ.get("AGENT_STALE_SECONDS", "300"))  # mark offline after no activity
MSG_ROTATE_LIMIT = int(os.environ.get("MSG_ROTATE_LIMIT", "2000"))       # keep channel from growing forever
LOG_ROTATE_LIMIT = int(os.environ.get("LOG_ROTATE_LIMIT", "2000"))
LOCK_TIMEOUT = 10  # seconds to wait for the lock before giving up


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _ts() -> float:
    return time.time()


# ============================================================================
# STATE HELPERS (team + memory) -- v6: locked + atomic
# ============================================================================

def _default_state() -> dict:
    return {
        "agents": {},
        "messages": [],
        "tasks": [],
        "notes": [],
        "facts": {},
        "summaries": [],
        "activity_log": [],
        "spawned": {},   # role -> {pid, terminal, started}
        "debate": None,  # active structured debate, or None
        "archived_messages": 0,  # count rotated out of the channel
    }


class _Lock:
    """Context manager: real FileLock if available, else a no-op (best effort)."""
    def __init__(self, lock_path):
        self._lock = FileLock(str(lock_path), timeout=LOCK_TIMEOUT) if _HAS_FILELOCK else None

    def __enter__(self):
        if self._lock is not None:
            try:
                self._lock.acquire()
            except LockTimeout:
                # Proceed without the lock rather than deadlock the agent.
                pass
        return self

    def __exit__(self, *exc):
        if self._lock is not None and self._lock.is_locked:
            self._lock.release()


def _atomic_write(path: Path, text: str) -> None:
    """Write to a temp file in the same dir, then atomically replace the target."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(path))  # atomic on the same filesystem
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _read_state_unlocked() -> dict:
    state = _default_state()
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            state.update(loaded)
            for k, v in _default_state().items():
                state.setdefault(k, v)
        except (json.JSONDecodeError, OSError):
            pass
    return state


def _write_state_unlocked(state: dict) -> None:
    # Rotate channel + log so the file doesn't grow without bound.
    msgs = state.get("messages", [])
    if len(msgs) > MSG_ROTATE_LIMIT:
        drop = len(msgs) - MSG_ROTATE_LIMIT
        state["messages"] = msgs[drop:]
        state["archived_messages"] = state.get("archived_messages", 0) + drop
    log = state.get("activity_log", [])
    if len(log) > LOG_ROTATE_LIMIT:
        state["activity_log"] = log[-LOG_ROTATE_LIMIT:]
    _atomic_write(STATE_FILE, json.dumps(state, indent=2, ensure_ascii=False))


def _load() -> dict:
    """Read current state (no lock needed for a pure read)."""
    return _read_state_unlocked()


def _save(state: dict) -> None:
    """Write state atomically under a lock (back-compat for simple callers)."""
    with _Lock(LOCK_FILE):
        _write_state_unlocked(state)


def _mutate(fn):
    """Run fn(state) -> result under a single lock, re-reading the freshest state
    first so concurrent agents never clobber each other (read-modify-write)."""
    with _Lock(LOCK_FILE):
        state = _read_state_unlocked()
        result = fn(state)
        _write_state_unlocked(state)
        return result


def _touch_agent(state: dict, role: str) -> None:
    """Record activity timestamp for stale detection."""
    if role and role in state.get("agents", {}):
        state["agents"][role]["last_seen"] = _ts()
        state["agents"][role]["status"] = "online"


def _cleanup_stale(state: dict) -> None:
    """Mark agents offline if they haven't been seen within AGENT_STALE_SECONDS."""
    now = _ts()
    for role, a in state.get("agents", {}).items():
        last = a.get("last_seen")
        if last and (now - last) > AGENT_STALE_SECONDS:
            a["status"] = "offline"


def _log(state: dict, who: str, action: str) -> None:
    state["activity_log"].append({"time": _now(), "who": who, "action": action})




def _format_msgs(msgs, start_index, my_role=""):
    lines = []
    for i, m in enumerate(msgs, start=start_index):
        mention = m.get("mention", "")
        flag = "  <-- FOR YOU" if my_role and mention == my_role else ""
        at = f" @{mention}" if mention else ""
        lines.append(f"[{i}] {m['time']} {m['from']}{at}: {m['text']}{flag}")
    return lines


# ============================================================================
# SECOND BRAIN HELPERS
# ============================================================================

def _brain_default() -> dict:
    return {"notes": [], "links": [], "daily": {}}


def _brain_load() -> dict:
    brain = _brain_default()
    if BRAIN_FILE.exists():
        try:
            loaded = json.loads(BRAIN_FILE.read_text(encoding="utf-8"))
            brain.update(loaded)
            for k, v in _brain_default().items():
                brain.setdefault(k, v)
        except (json.JSONDecodeError, OSError):
            pass
    return brain


def _brain_save(brain: dict) -> None:
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    with _Lock(BRAIN_LOCK):
        _atomic_write(BRAIN_FILE, json.dumps(brain, indent=2, ensure_ascii=False))


def _brain_mutate(fn):
    """Locked read-modify-write for the second brain."""
    BRAIN_DIR.mkdir(parents=True, exist_ok=True)
    with _Lock(BRAIN_LOCK):
        brain = _brain_load()
        result = fn(brain)
        _atomic_write(BRAIN_FILE, json.dumps(brain, indent=2, ensure_ascii=False))
        return result


# ============================================================================
# TEAM COORDINATION TOOLS (v1 + v2)
# ============================================================================

@mcp.tool()
def join_team(role: str, name: str = "") -> str:
    """Register yourself as a team member. Call ONCE at the start of a session.

    Args:
        role: Your job, e.g. "Backend", "Frontend", "QA", "PM".
        name: Optional display name. Defaults to the role.
    """
    member = name or role

    def op(state):
        state["agents"][role] = {"name": member, "role": role, "status": "online",
                                 "joined_at": _hms(), "last_seen": _ts()}
        state["messages"].append({"from": "system", "text": f"{member} ({role}) joined the team.", "time": _hms()})
        _log(state, role, "joined the team")
        _cleanup_stale(state)
        return [a["name"] for r, a in state["agents"].items() if r != role and a.get("status") == "online"]

    others = _mutate(op)
    roster = ", ".join(others) if others else "no one else yet"
    return (f"Joined as {member} ({role}). Online: {roster}. "
            f"Tip: load_summary to get up to speed cheaply; wait_for_message to block for replies.")


@mcp.tool()
def post_message(sender_role: str, text: str, mention: str = "") -> str:
    """Post a message to the shared team channel. Agents on wait_for_message wake instantly.

    Args:
        sender_role: Your role (must match join_team).
        text: The message body.
        mention: Optional role to direct this at, e.g. "Backend". Shows as @Backend.
    """
    def op(state):
        entry = {"from": sender_role, "text": text, "time": _hms()}
        if mention:
            entry["mention"] = mention
        state["messages"].append(entry)
        _touch_agent(state, sender_role)
        return len(state["messages"])

    total = _mutate(op)
    tag = f" -> @{mention}" if mention else ""
    return f"Posted{tag}. {total} messages in channel."


@mcp.tool()
def read_channel(since_index: int = 0, my_role: str = "") -> str:
    """Read channel messages WITHOUT waiting (instant snapshot).

    Args:
        since_index: Return messages from this index onward. 0 = from start.
        my_role: Optional -- messages that @mention you are flagged.
    """
    state = _load()
    msgs = state["messages"][since_index:]
    if not msgs:
        return f"No new messages. next_index={len(state['messages'])}"
    lines = _format_msgs(msgs, since_index, my_role)
    lines.append(f"\nnext_index={len(state['messages'])}")
    return "\n".join(lines)


@mcp.tool()
def wait_for_message(since_index: int, my_role: str = "", timeout_seconds: int = 120) -> str:
    """BLOCK until a new message appears, then return it. Enables live conversation.

    Args:
        since_index: The next_index from your last read/wait.
        my_role: Optional -- messages addressed to you (@mention) are flagged, and
                 your presence is refreshed so you don't show as stale while waiting.
        timeout_seconds: Max seconds to wait (default 120).
    """
    deadline = time.time() + max(1, timeout_seconds)
    interval = 0.25          # start responsive
    last_touch = 0.0
    while time.time() < deadline:
        state = _read_state_unlocked()
        total = len(state["messages"])
        if total > since_index:
            msgs = state["messages"][since_index:]
            lines = _format_msgs(msgs, since_index, my_role)
            lines.append(f"\nnext_index={total}")
            return "\n".join(lines)
        # Refresh presence about every 30s so a waiting agent stays "online".
        now = time.time()
        if my_role and (now - last_touch) > 30:
            _mutate(lambda s: _touch_agent(s, my_role))
            last_touch = now
        time.sleep(interval)
        interval = min(interval * 1.5, 2.0)  # back off up to 2s to cut file churn
    total = len(_read_state_unlocked()["messages"])
    return (f"No new messages after waiting {timeout_seconds}s. "
            f"next_index={total}. Call wait_for_message again to keep listening.")


@mcp.tool()
def add_task(title: str, assignee: str = "", created_by: str = "") -> str:
    """Add a task to the shared board (usually the PM).

    Args:
        title: Short description.
        assignee: Role responsible. Empty = unassigned.
        created_by: Your role.
    """
    def op(state):
        task_id = (max([t["id"] for t in state["tasks"]], default=0)) + 1
        state["tasks"].append({"id": task_id, "title": title, "assignee": assignee,
                               "status": "todo", "created_by": created_by, "updated": _hms()})
        _log(state, created_by or "system", f'added task #{task_id}: {title}')
        _touch_agent(state, created_by)
        return task_id

    task_id = _mutate(op)
    who = f" assigned to {assignee}" if assignee else " (unassigned)"
    return f'Task #{task_id} added: "{title}"{who}.'


@mcp.tool()
def update_task(task_id: int, status: str = "", assignee: str = "", note: str = "", by_role: str = "") -> str:
    """Claim a task or change status (todo/in_progress/blocked/done). Announces to channel.

    Args:
        task_id: The task number.
        status: New status. Optional.
        assignee: Reassign to this role. Optional.
        note: Optional note posted to the channel.
        by_role: Your role.
    """
    def op(state):
        task = next((t for t in state["tasks"] if t["id"] == task_id), None)
        if not task:
            return None
        if status:
            task["status"] = status
        if assignee:
            task["assignee"] = assignee
        task["updated"] = _hms()
        msg = f'Task #{task_id} "{task["title"]}" -> {task["status"]}'
        if note:
            msg += f" ({note})"
        state["messages"].append({"from": by_role or "system", "text": msg, "time": _hms()})
        _log(state, by_role or "system", f'task #{task_id} -> {task["status"]}')
        _touch_agent(state, by_role)
        # If the assignee finished, free them up for new work automatically.
        if status == "done":
            who = task.get("assignee") or by_role
            if who and who in state["agents"]:
                state["agents"][who]["status"] = "idle"
                state["messages"].append({
                    "from": who, "mention": "PM",
                    "text": f"@{who} finished task #{task_id} and is now IDLE -- ready for more.",
                    "time": _hms(),
                })
        return task

    task = _mutate(op)
    if not task:
        return f"No task #{task_id} found."
    return f"Updated task #{task_id}: status={task['status']}, assignee={task['assignee'] or 'none'}."


@mcp.tool()
def view_board(my_role: str = "") -> str:
    """View the task board and who's online (auto-marks stale agents offline).

    Args:
        my_role: Optional -- your tasks are flagged.
    """
    def op(state):
        _cleanup_stale(state)
        _touch_agent(state, my_role)
    _mutate(op)
    state = _load()
    out = ["=== TEAM ==="]
    if state["agents"]:
        for a in state["agents"].values():
            out.append(f"  {a['name']} ({a['role']}) - {a.get('status', 'online')}")
    else:
        out.append("  (no one online)")
    out.append("\n=== TASK BOARD ===")
    if not state["tasks"]:
        out.append("  (empty)")
    icons = {"todo": "[ ]", "in_progress": "[~]", "blocked": "[!]", "done": "[x]"}
    for t in state["tasks"]:
        mine = "  <-- YOURS" if my_role and t["assignee"] == my_role else ""
        ic = icons.get(t["status"], "[ ]")
        out.append(f"  {ic} #{t['id']} {t['title']} ({t['assignee'] or 'unassigned'}){mine}")
    return "\n".join(out)


# ============================================================================
# PROJECT MEMORY TOOLS (v3)
# ============================================================================

@mcp.tool()
def save_note(text: str, tags: str = "", by_role: str = "") -> str:
    """Save project knowledge to shared memory (decisions, conventions, gotchas).

    Args:
        text: The knowledge, e.g. "Auth uses JWT, 7-day expiry".
        tags: Optional space/comma-separated tags, e.g. "auth api".
        by_role: Your role.
    """
    state = _load()
    note_id = len(state["notes"]) + 1
    tag_list = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    state["notes"].append({"id": note_id, "text": text, "tags": tag_list, "by": by_role, "time": _now()})
    _log(state, by_role or "system", f"saved note #{note_id}")
    _save(state)
    return f"Note #{note_id} saved." + (f" Tags: {', '.join(tag_list)}." if tag_list else "")


@mcp.tool()
def search_notes(query: str = "", tag: str = "") -> str:
    """Search the project knowledge base (saves tokens vs re-reading files).

    Args:
        query: Text to find (case-insensitive). Empty = all.
        tag: Optional tag filter.
    """
    state = _load()
    q = query.lower().strip()
    results = []
    for n in state["notes"]:
        if tag and tag.strip() not in n.get("tags", []):
            continue
        if q and q not in n["text"].lower():
            continue
        results.append(n)
    if not results:
        return "No matching notes found."
    out = [f"Found {len(results)} note(s):"]
    for n in results:
        tg = f" [{', '.join(n['tags'])}]" if n.get("tags") else ""
        out.append(f"  #{n['id']} ({n['by'] or '?'}, {n['time']}){tg}: {n['text']}")
    return "\n".join(out)


@mcp.tool()
def set_fact(key: str, value: str, by_role: str = "") -> str:
    """Store a key-value fact (deadline, version, base URL...). Overwrites same key.

    Args:
        key: Fact name, e.g. "api_base_url".
        value: The value.
        by_role: Your role.
    """
    state = _load()
    state["facts"][key] = {"value": value, "by": by_role, "time": _now()}
    _log(state, by_role or "system", f"set fact {key}={value}")
    _save(state)
    return f"Fact saved: {key} = {value}"


@mcp.tool()
def get_facts(key: str = "") -> str:
    """Retrieve a fact, or all facts if no key given.

    Args:
        key: Optional specific fact name. Empty = list all.
    """
    state = _load()
    facts = state["facts"]
    if not facts:
        return "No facts stored yet."
    if key:
        f = facts.get(key)
        if not f:
            return f"No fact named '{key}'. Known keys: {', '.join(facts.keys())}"
        return f"{key} = {f['value']} (by {f['by'] or '?'}, {f['time']})"
    out = ["Stored facts:"]
    for k, f in facts.items():
        out.append(f"  {k} = {f['value']}")
    return "\n".join(out)


@mcp.tool()
def save_summary(text: str, by_role: str = "") -> str:
    """Save a compact progress summary. TOKEN-SAVING: write a summary when context
    gets long, then /clear and load_summary to resume cheaply.

    Args:
        text: Concise summary of done/decided/next.
        by_role: Your role.
    """
    state = _load()
    sid = len(state["summaries"]) + 1
    state["summaries"].append({"id": sid, "text": text, "by": by_role, "time": _now()})
    _log(state, by_role or "system", f"saved summary #{sid}")
    _save(state)
    return f"Summary #{sid} saved at {_now()}. Use load_summary to retrieve."


@mcp.tool()
def load_summary(which: str = "latest") -> str:
    """Load a saved summary to restore context cheaply.

    Args:
        which: "latest", "all", or a summary id number.
    """
    state = _load()
    summaries = state["summaries"]
    if not summaries:
        return "No summaries saved yet."
    if which == "all":
        out = ["All summaries:"]
        for s in summaries:
            out.append(f"\n--- #{s['id']} ({s['by'] or '?'}, {s['time']}) ---\n{s['text']}")
        return "\n".join(out)
    if which == "latest":
        s = summaries[-1]
    else:
        try:
            sid = int(which)
            s = next((x for x in summaries if x["id"] == sid), None)
        except ValueError:
            s = None
        if not s:
            return f"No summary '{which}'. Available ids: {', '.join(str(x['id']) for x in summaries)}"
    return f"Summary #{s['id']} ({s['by'] or '?'}, {s['time']}):\n{s['text']}"


@mcp.tool()
def project_log(last_n: int = 20) -> str:
    """View the timestamped project activity history.

    Args:
        last_n: How many recent entries to show (default 20).
    """
    state = _load()
    log = state["activity_log"]
    if not log:
        return "Activity log is empty."
    recent = log[-last_n:]
    out = [f"Activity log (last {len(recent)} of {len(log)}):"]
    for e in recent:
        out.append(f"  {e['time']} | {e['who']}: {e['action']}")
    return "\n".join(out)


# ============================================================================
# AUTO-SPAWN TERMINAL TOOLS (v4, Windows)
# ============================================================================

def _spawn_windows(role: str, project_dir: str, terminal: str):
    """Open a new terminal window running Claude Code. Returns (pid, window_title).

    The window gets a unique title so we can close it later by title (more reliable
    than the launcher PID, which `wt` discards after handing off the tab).
    """
    window_title = f"team-agent-{role}"
    # Note: no double-quotes inside this string -- it is wrapped in double-quotes
    # on the command line, and nested double-quotes would break it.
    kickoff = (
        f"You are the {role} agent. First call join_team with role {role}. "
        f"Then call load_summary latest for context, view_board for your tasks, "
        f"and wait_for_message to listen for the team. Do your assigned work."
    )

    if terminal == "wt":
        # Set the cmd window title (title cmd), then run claude. wt --title names the tab.
        inner = f'title {window_title} && cd /d "{project_dir}" && claude --dangerously-skip-permissions "{kickoff}"'
        args = ["wt", "new-tab", "--title", window_title, "cmd", "/k", inner]
        proc = subprocess.Popen(args)
        return proc.pid, window_title
    else:
        # Classic cmd window via `start`. The first quoted arg to `start` is the title.
        inner = f'title {window_title} && cd /d "{project_dir}" && claude --dangerously-skip-permissions "{kickoff}"'
        args = f'start "{window_title}" cmd /k {inner}'
        proc = subprocess.Popen(args, shell=True)
        return proc.pid, window_title


@mcp.tool()
def spawn_agent(role: str, project_dir: str, terminal: str = "wt", by_role: str = "") -> str:
    """Open a NEW terminal window and launch a Claude Code agent in it that
    auto-joins the team as `role`. Windows only.

    Safety: refuses once MAX_AGENTS windows are already tracked. Override with the
    MAX_AGENTS env var when adding the server.

    Args:
        role: Role for the new agent, e.g. "Backend".
        project_dir: Absolute path the new terminal should cd into.
        terminal: "wt" for Windows Terminal (default) or "cmd" for a classic cmd window.
        by_role: Your role (the one spawning).
    """
    if sys.platform != "win32":
        return "spawn_agent only works on Windows. (Detected non-Windows platform.)"
    state = _load()
    if len(state["spawned"]) >= MAX_AGENTS:
        return (f"Refused: {len(state['spawned'])} agents already spawned "
                f"(limit MAX_AGENTS={MAX_AGENTS}). Close one with close_agent, "
                f"or raise MAX_AGENTS when adding the server.")
    if role in state["spawned"]:
        return f"An agent for role '{role}' is already spawned. Close it first with close_agent."
    if terminal not in ("wt", "cmd"):
        return "terminal must be 'wt' or 'cmd'."
    try:
        pid, title = _spawn_windows(role, project_dir, terminal)
    except FileNotFoundError:
        return ("Could not launch terminal. If using 'wt', make sure Windows Terminal "
                "is installed and on PATH; otherwise try terminal='cmd'.")
    except Exception as e:
        return f"Failed to spawn: {e}"

    def op(state):
        state["spawned"][role] = {"pid": pid, "title": title, "terminal": terminal, "started": _now()}
        _log(state, by_role or "system", f"spawned {role} agent ({terminal}, title {title})")
        return len(state["spawned"])
    count = _mutate(op)
    return (f"Spawned {role} agent in a new {terminal} window (title '{title}'). "
            f"It will auto-join the team. {count}/{MAX_AGENTS} agents running.")


@mcp.tool()
def list_running_agents() -> str:
    """List terminal agents spawned via spawn_agent."""
    state = _load()
    sp = state["spawned"]
    if not sp:
        return "No spawned agents tracked."
    out = [f"Spawned agents ({len(sp)}/{MAX_AGENTS}):"]
    for role, info in sp.items():
        out.append(f"  {role}: {info['terminal']} window '{info.get('title', '?')}', since {info['started']}")
    return "\n".join(out)


@mcp.tool()
def close_agent(role: str, by_role: str = "") -> str:
    """Close the terminal WINDOW opened by spawn_agent (not just the process).

    Closes by window title, which reliably shuts the whole window -- unlike killing
    the launcher PID, which `wt` discards after creating the tab.

    Args:
        role: The role whose window to close.
        by_role: Your role.
    """
    if sys.platform != "win32":
        return "close_agent only works on Windows."
    state = _load()
    info = state["spawned"].get(role)
    if not info:
        return f"No spawned agent tracked for role '{role}'."
    title = info.get("title", f"team-agent-{role}")
    pid = info.get("pid")
    killed = False
    errors = []
    # Primary: kill by window title (catches the cmd window + its children).
    try:
        r = subprocess.run(["taskkill", "/FI", f"WINDOWTITLE eq {title}", "/T", "/F"],
                           capture_output=True, text=True)
        if "SUCCESS" in (r.stdout or "").upper():
            killed = True
        else:
            errors.append((r.stdout or r.stderr or "").strip())
    except Exception as e:
        errors.append(str(e))
    # Fallback: also try the launcher PID (helps the classic cmd path).
    if pid:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True)
        except Exception:
            pass

    def op(state):
        if role in state["spawned"]:
            del state["spawned"][role]
        if role in state["agents"]:
            state["agents"][role]["status"] = "offline"
        _log(state, by_role or "system", f"closed {role} agent window '{title}'")
        return len(state["spawned"])
    count = _mutate(op)

    if killed:
        return f"Closed {role} agent window '{title}'. {count}/{MAX_AGENTS} agents running."
    return (f"Sent close to {role} (title '{title}'). If the window is still open, it may have "
            f"had a different title. Detail: {'; '.join(e for e in errors if e) or 'no SUCCESS confirmation'}. "
            f"{count}/{MAX_AGENTS} tracked.")


# ============================================================================
# SECOND BRAIN TOOLS (v4, self-contained on D:\)
# ============================================================================

@mcp.tool()
def brain_add(title: str, content: str, category: str = "", tags: str = "") -> str:
    """Add a note to your self-contained second brain (stored on disk at D:\\mcp\\second_brain).
    Use for ideas, knowledge, references -- anything you want to recall later.

    Args:
        title: Short title (also used to link notes by name).
        content: The body of the note.
        category: Optional category/folder, e.g. "projects", "ideas".
        tags: Optional space/comma-separated tags.
    """
    brain = _brain_load()
    nid = len(brain["notes"]) + 1
    tag_list = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
    brain["notes"].append({
        "id": nid, "title": title, "content": content,
        "category": category, "tags": tag_list, "time": _now(),
    })
    _brain_save(brain)
    return f'Brain note #{nid} "{title}" saved to {BRAIN_FILE}.'


@mcp.tool()
def brain_search(query: str = "", category: str = "", tag: str = "") -> str:
    """Search your second brain by text, category, or tag.

    Args:
        query: Text to find in title or content (case-insensitive). Empty = all.
        category: Optional category filter.
        tag: Optional tag filter.
    """
    brain = _brain_load()
    q = query.lower().strip()
    results = []
    for n in brain["notes"]:
        if category and n.get("category") != category:
            continue
        if tag and tag.strip() not in n.get("tags", []):
            continue
        if q and q not in n["title"].lower() and q not in n["content"].lower():
            continue
        results.append(n)
    if not results:
        return "No matching brain notes."
    out = [f"Found {len(results)} note(s):"]
    for n in results:
        cat = f" /{n['category']}" if n.get("category") else ""
        tg = f" [{', '.join(n['tags'])}]" if n.get("tags") else ""
        preview = n["content"][:120] + ("..." if len(n["content"]) > 120 else "")
        out.append(f"  #{n['id']}{cat} \"{n['title']}\"{tg}: {preview}")
    return "\n".join(out)


@mcp.tool()
def brain_get(note_id: int) -> str:
    """Read one second-brain note in full, with its links.

    Args:
        note_id: The note id.
    """
    brain = _brain_load()
    n = next((x for x in brain["notes"] if x["id"] == note_id), None)
    if not n:
        return f"No brain note #{note_id}."
    links = [l for l in brain["links"] if l["from"] == note_id or l["to"] == note_id]
    out = [f'#{n["id"]} "{n["title"]}"']
    if n.get("category"):
        out.append(f"Category: {n['category']}")
    if n.get("tags"):
        out.append(f"Tags: {', '.join(n['tags'])}")
    out.append(f"Saved: {n['time']}")
    out.append("")
    out.append(n["content"])
    if links:
        out.append("\nLinks:")
        for l in links:
            other = l["to"] if l["from"] == note_id else l["from"]
            on = next((x["title"] for x in brain["notes"] if x["id"] == other), f"#{other}")
            out.append(f"  <-> #{other} {on}" + (f" ({l['rel']})" if l.get("rel") else ""))
    return "\n".join(out)


@mcp.tool()
def brain_link(from_id: int, to_id: int, rel: str = "") -> str:
    """Connect two second-brain notes (builds the knowledge graph).

    Args:
        from_id: Source note id.
        to_id: Target note id.
        rel: Optional relationship label, e.g. "depends on", "related".
    """
    brain = _brain_load()
    ids = {n["id"] for n in brain["notes"]}
    if from_id not in ids or to_id not in ids:
        return f"Both notes must exist. Known ids: {sorted(ids)}"
    brain["links"].append({"from": from_id, "to": to_id, "rel": rel, "time": _now()})
    _brain_save(brain)
    return f"Linked #{from_id} <-> #{to_id}" + (f" ({rel})" if rel else "") + "."


@mcp.tool()
def brain_backlinks(note_id: int) -> str:
    """Show every note connected to this one (backlinks / related notes).

    Args:
        note_id: The note id.
    """
    brain = _brain_load()
    if note_id not in {n["id"] for n in brain["notes"]}:
        return f"No brain note #{note_id}."
    connected = []
    for l in brain["links"]:
        if l["from"] == note_id:
            connected.append((l["to"], l.get("rel", "")))
        elif l["to"] == note_id:
            connected.append((l["from"], l.get("rel", "")))
    if not connected:
        return f"Note #{note_id} has no links yet."
    out = [f"Notes connected to #{note_id}:"]
    for oid, rel in connected:
        on = next((x["title"] for x in brain["notes"] if x["id"] == oid), f"#{oid}")
        out.append(f"  #{oid} {on}" + (f" ({rel})" if rel else ""))
    return "\n".join(out)


@mcp.tool()
def brain_daily(entry: str, date: str = "") -> str:
    """Append to today's daily journal in the second brain (or a given date).

    Args:
        entry: The journal line to add.
        date: Optional YYYY-MM-DD. Defaults to today.
    """
    brain = _brain_load()
    day = date or datetime.now().strftime("%Y-%m-%d")
    brain["daily"].setdefault(day, [])
    brain["daily"][day].append({"time": _hms(), "entry": entry})
    _brain_save(brain)
    return f"Added to journal {day} ({len(brain['daily'][day])} entries today)."


@mcp.tool()
def brain_list() -> str:
    """Overview of the whole second brain: note count, categories, recent titles, journal days."""
    brain = _brain_load()
    notes = brain["notes"]
    if not notes and not brain["daily"]:
        return f"Second brain is empty. (Storage: {BRAIN_FILE})"
    cats = {}
    for n in notes:
        cats[n.get("category") or "(uncategorized)"] = cats.get(n.get("category") or "(uncategorized)", 0) + 1
    out = [f"Second brain @ {BRAIN_FILE}",
           f"Total notes: {len(notes)} | Links: {len(brain['links'])} | Journal days: {len(brain['daily'])}",
           "\nBy category:"]
    for c, n in cats.items():
        out.append(f"  {c}: {n}")
    recent = notes[-5:]
    if recent:
        out.append("\nRecent notes:")
        for n in recent:
            out.append(f"  #{n['id']} {n['title']}")
    return "\n".join(out)


# ============================================================================
# AGENT AVAILABILITY / RE-ASSIGNMENT (v6.1)
# ============================================================================

@mcp.tool()
def set_status(role: str, status: str = "idle") -> str:
    """Set your working status so the PM knows if you're free for more work.
    Call set_status("idle") when you FINISH a task -- this makes you show up as
    available so the PM can assign you something new (fixing the "finished agents
    can't get new work" problem).

    Args:
        role: Your role.
        status: "idle" (free for work), "busy" (working), or "offline".
    """
    status = status.lower().strip()
    if status not in ("idle", "busy", "offline", "online"):
        return "status must be one of: idle, busy, offline."

    def op(state):
        if role not in state["agents"]:
            state["agents"][role] = {"name": role, "role": role, "joined_at": _hms()}
        state["agents"][role]["status"] = status
        state["agents"][role]["last_seen"] = _ts()
        if status == "idle":
            state["messages"].append({
                "from": role, "mention": "PM",
                "text": f"@{role} is now IDLE and ready for a new task.",
                "time": _hms(),
            })
        _log(state, role, f"status -> {status}")
        # count idle agents for the reply
        return [r for r, a in state["agents"].items() if a.get("status") == "idle"]
    idle = _mutate(op)
    if status == "idle":
        return f"@{role} marked IDLE. PM has been pinged. Idle agents now: {', '.join(idle) or 'none'}."
    return f"@{role} status set to {status}."


@mcp.tool()
def who_is_free() -> str:
    """List agents that are IDLE (finished their work and ready for a new task).
    The PM uses this to find someone to assign the next task to.
    """
    def op(state):
        _cleanup_stale(state)
    _mutate(op)
    state = _load()
    idle, busy, off = [], [], []
    for r, a in state.get("agents", {}).items():
        s = a.get("status", "online")
        if s == "idle":
            idle.append(r)
        elif s in ("busy", "online"):
            busy.append(f"{r} ({s})")
        else:
            off.append(r)
    out = ["=== AVAILABILITY ==="]
    out.append(f"IDLE (free for work): {', '.join(idle) or 'none'}")
    out.append(f"Working: {', '.join(busy) or 'none'}")
    out.append(f"Offline: {', '.join(off) or 'none'}")
    return "\n".join(out)


@mcp.tool()
def assign_work(task_title: str, to_role: str, by_role: str = "PM", detail: str = "") -> str:
    """Assign (or re-assign) a task to an agent and ping them. Works for idle agents
    too -- this is how a finished agent gets put back to work. Creates the task,
    assigns it, marks the agent busy, and posts a mention so they wake up.

    Args:
        task_title: What to do.
        to_role: The agent to assign it to.
        by_role: Your role (usually PM).
        detail: Optional extra instructions posted with the ping.
    """
    def op(state):
        task_id = (max([t["id"] for t in state["tasks"]], default=0)) + 1
        state["tasks"].append({"id": task_id, "title": task_title, "assignee": to_role,
                               "status": "todo", "created_by": by_role, "updated": _hms()})
        # mark the assignee busy so they don't get double-assigned
        if to_role in state["agents"]:
            state["agents"][to_role]["status"] = "busy"
        msg = f"@{to_role} new task #{task_id}: {task_title}"
        if detail:
            msg += f" -- {detail}"
        state["messages"].append({"from": by_role, "mention": to_role, "text": msg, "time": _hms()})
        _log(state, by_role, f"assigned task #{task_id} to {to_role}")
        _touch_agent(state, by_role)
        return task_id
    task_id = _mutate(op)
    return (f"Task #{task_id} assigned to @{to_role} and pinged. They'll see it on their next "
            f"wait_for_message/read_channel. Marked {to_role} busy.")


# ============================================================================
# STRUCTURED DEBATE / DELIBERATION TOOLS (v5)
# ============================================================================
# Research-backed pattern: PROPOSE -> CRITIQUE -> REVISE -> JUDGE.
# Agents argue, disagree with justification, and revise only for sound reasons.
# A judge/moderator decides when there's consensus or issues a final verdict,
# preventing premature convergence and capping rounds to avoid endless loops.

def _active_debate(state):
    return state.get("debate")


@mcp.tool()
def start_debate(topic: str, options: str = "", max_rounds: int = 3, judge_role: str = "PM", by_role: str = "") -> str:
    """Open a structured debate so agents can ARGUE toward the right plan instead
    of just chatting. Use this before building something where the best approach
    isn't obvious. The flow is: propose -> critique -> revise -> judge decides.

    Args:
        topic: The question to settle, e.g. "Which auth approach should we use?".
        options: Optional candidate options, comma-separated, e.g. "JWT, sessions, OAuth".
        max_rounds: Max critique rounds before the judge must decide (default 3). Caps token cost.
        judge_role: Role that will moderate and issue the final verdict (default "PM").
        by_role: Your role.
    """
    state = _load()
    opt_list = [o.strip() for o in options.split(",") if o.strip()]
    state["debate"] = {
        "topic": topic,
        "options": opt_list,
        "max_rounds": max_rounds,
        "judge_role": judge_role,
        "round": 0,
        "phase": "proposing",   # proposing -> critiquing -> revising -> decided
        "proposals": [],        # {role, stance, argument, round}
        "critiques": [],        # {role, target_role, agree, argument, round}
        "verdict": None,
        "started_by": by_role,
        "started": _now(),
    }
    state["messages"].append({
        "from": "system",
        "text": f"DEBATE STARTED: {topic}" + (f" | options: {', '.join(opt_list)}" if opt_list else "")
                + f" | judge: {judge_role} | max {max_rounds} rounds. Everyone: submit_proposal with your stance + reasoning.",
        "time": _hms(),
    })
    _log(state, by_role or "system", f"started debate: {topic}")
    _save(state)
    return (f"Debate opened on: {topic}. Judge: {judge_role}, max rounds: {max_rounds}. "
            f"Phase: PROPOSING. Each agent should call submit_proposal with their stance and reasoning.")


@mcp.tool()
def submit_proposal(role: str, stance: str, argument: str) -> str:
    """Submit your position in the current debate, with the REASONING behind it.
    Posted to the channel mentioning you so the team sees who argued what.

    Args:
        role: Your role.
        stance: Your position in a few words, e.g. "Use JWT".
        argument: Why -- the evidence/reasoning. Be specific; weak arguments get challenged.
    """
    state = _load()
    d = _active_debate(state)
    if not d:
        return "No active debate. Start one with start_debate first."
    if d["phase"] == "decided":
        return "This debate is already decided. See get_debate for the verdict."
    d["proposals"].append({"role": role, "stance": stance, "argument": argument, "round": d["round"]})
    if d["phase"] == "proposing":
        d["phase"] = "critiquing"
    state["messages"].append({
        "from": role, "mention": "all",
        "text": f"@{role} proposes: \"{stance}\" -- because: {argument}",
        "time": _hms(),
    })
    _log(state, role, f"proposed: {stance}")
    _save(state)
    return (f"Proposal recorded: \"{stance}\". Now read others' proposals (get_debate) and "
            f"challenge weak ones with submit_critique. Phase: CRITIQUING.")


@mcp.tool()
def submit_critique(role: str, target_role: str, agree: bool, argument: str) -> str:
    """Critique another agent's proposal -- AGREE or DISAGREE, always with reasons.
    This is the core of the debate: you must justify, not just vote. The message is
    displayed mentioning the target agent so the disagreement is visible in chat.

    Args:
        role: Your role.
        target_role: The role whose proposal you're responding to.
        agree: True if you agree with their proposal, False if you disagree.
        argument: Your reasoning. If disagreeing, say specifically what's wrong and why.
    """
    state = _load()
    d = _active_debate(state)
    if not d:
        return "No active debate."
    if d["phase"] == "decided":
        return "This debate is already decided."
    d["critiques"].append({
        "role": role, "target_role": target_role, "agree": agree,
        "argument": argument, "round": d["round"],
    })
    verdict_word = "AGREE" if agree else "DISAGREE"
    arrow = "✓ agrees with" if agree else "✗ disagrees with"
    state["messages"].append({
        "from": role, "mention": target_role,
        "text": f"@{role} {arrow} @{target_role}: {argument}",
        "time": _hms(),
    })
    _log(state, role, f"{verdict_word} with {target_role}")
    _save(state)
    hint = ("If someone's critique of YOUR proposal is sound, call revise_proposal to update it. "
            "Otherwise defend it with another critique.")
    return f"Critique recorded: @{role} {arrow} @{target_role}. {hint}"


@mcp.tool()
def revise_proposal(role: str, new_stance: str, reason: str) -> str:
    """Update your stance because a peer made a SOUND argument. Only revise for good
    reasons -- not just because others disagree (that's conformity, which hurts quality).

    Args:
        role: Your role.
        new_stance: Your updated position.
        reason: What argument convinced you to change.
    """
    state = _load()
    d = _active_debate(state)
    if not d:
        return "No active debate."
    if d["phase"] == "decided":
        return "This debate is already decided."
    d["proposals"].append({"role": role, "stance": new_stance, "argument": f"(revised) {reason}", "round": d["round"]})
    d["phase"] = "revising"
    state["messages"].append({
        "from": role, "mention": "all",
        "text": f"@{role} revised to: \"{new_stance}\" -- changed because: {reason}",
        "time": _hms(),
    })
    _log(state, role, f"revised to: {new_stance}")
    _save(state)
    return f"Revised stance recorded: \"{new_stance}\"."


@mcp.tool()
def next_round(by_role: str = "") -> str:
    """Advance the debate to the next critique round (the judge/PM calls this).
    Refuses past max_rounds -- then you must call judge_debate to decide.

    Args:
        by_role: Your role (should be the judge).
    """
    state = _load()
    d = _active_debate(state)
    if not d:
        return "No active debate."
    if d["phase"] == "decided":
        return "Debate already decided."
    if d["round"] + 1 >= d["max_rounds"]:
        d["round"] += 1
        _save(state)
        return (f"Reached round {d['round']} of {d['max_rounds']} (the max). "
                f"No more rounds -- the judge ({d['judge_role']}) must now call judge_debate to decide.")
    d["round"] += 1
    d["phase"] = "critiquing"
    state["messages"].append({
        "from": "system",
        "text": f"--- DEBATE ROUND {d['round'] + 1} --- Refine your arguments or concede if peers are right.",
        "time": _hms(),
    })
    _log(state, by_role or "system", f"advanced debate to round {d['round']}")
    _save(state)
    return f"Now in round {d['round'] + 1}. Agents: critique or revise. Judge decides when ready."


@mcp.tool()
def get_debate() -> str:
    """See the full state of the current debate: topic, all proposals, all critiques
    (who agreed/disagreed with whom and why), the round, and any verdict. Call this
    to catch up before arguing.
    """
    state = _load()
    d = _active_debate(state)
    if not d:
        return "No active debate. Start one with start_debate."
    out = [f"=== DEBATE: {d['topic']} ===",
           f"Phase: {d['phase'].upper()} | Round: {d['round'] + 1}/{d['max_rounds']} | Judge: {d['judge_role']}"]
    if d["options"]:
        out.append(f"Options on the table: {', '.join(d['options'])}")
    out.append("\n--- PROPOSALS (latest stance per agent) ---")
    latest = {}
    for p in d["proposals"]:
        latest[p["role"]] = p
    if latest:
        for role, p in latest.items():
            out.append(f"  @{role}: \"{p['stance']}\" -- {p['argument']}")
    else:
        out.append("  (none yet)")
    out.append("\n--- ARGUMENTS (who challenges whom) ---")
    if d["critiques"]:
        for c in d["critiques"]:
            arrow = "✓ agrees with" if c["agree"] else "✗ disagrees with"
            out.append(f"  @{c['role']} {arrow} @{c['target_role']}: {c['argument']}")
    else:
        out.append("  (none yet)")
    if d["verdict"]:
        out.append(f"\n=== VERDICT (by {d['verdict']['by']}) ===\n{d['verdict']['decision']}\nReason: {d['verdict']['reason']}")
    return "\n".join(out)


@mcp.tool()
def judge_debate(judge_role: str, decision: str, reason: str, create_tasks: bool = True) -> str:
    """End the debate with a final decision (the judge/PM does this after weighing
    the arguments). Optionally turns the decided plan into a task on the board so
    the team can execute it. Picks the side with the soundest reasoning -- not just
    the majority.

    Args:
        judge_role: Your role (the judge).
        decision: The final chosen plan/answer.
        reason: Why this won -- which arguments were strongest.
        create_tasks: If True, add a task capturing the decision so work can start.
    """
    state = _load()
    d = _active_debate(state)
    if not d:
        return "No active debate to judge."
    d["phase"] = "decided"
    d["verdict"] = {"by": judge_role, "decision": decision, "reason": reason, "time": _now()}
    state["messages"].append({
        "from": judge_role, "mention": "all",
        "text": f"[VERDICT] {decision} | Reasoning: {reason}",
        "time": _hms(),
    })
    # Archive the debate into project memory so the decision is remembered.
    state["notes"].append({
        "id": len(state["notes"]) + 1,
        "text": f"DECISION on '{d['topic']}': {decision}. Reason: {reason}",
        "tags": ["decision", "debate"],
        "by": judge_role, "time": _now(),
    })
    made = ""
    if create_tasks:
        tid = len(state["tasks"]) + 1
        state["tasks"].append({"id": tid, "title": f"Execute decision: {decision}",
                               "assignee": "", "status": "todo", "created_by": judge_role, "updated": _hms()})
        made = f" Task #{tid} created to execute it."
    _log(state, judge_role, f"judged debate: {decision}")
    state["debate"] = None  # clear active debate; archived in notes
    _save(state)
    return (f"Debate decided: {decision}.{made} Decision saved to project memory. "
            f"Now assign and execute the task as a team.")


# ============================================================================
# RESET
# ============================================================================

@mcp.tool()
def reset_team(keep_memory: bool = False) -> str:
    """Wipe the team workspace for a fresh session. Does NOT touch the second brain.

    Args:
        keep_memory: If True, keep notes/facts/summaries/log and only clear
                     agents, messages, tasks, spawned. If False, wipe team state
                     (the second brain on disk is always left untouched).
    """
    if keep_memory:
        state = _load()
        state["agents"] = {}
        state["messages"] = []
        state["tasks"] = []
        state["spawned"] = {}
        state["debate"] = None
        _log(state, "system", "reset channel+board (memory kept)")
        _save(state)
        return "Channel, board, roster, spawn list cleared. Memory kept. Second brain untouched."
    _save(_default_state())
    return "Team state fully reset. Second brain on disk is untouched."


if __name__ == "__main__":
    mcp.run()
