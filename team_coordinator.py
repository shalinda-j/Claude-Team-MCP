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
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        "read_state": {},   # role -> last message index that role has acknowledged
        "skills": {},       # role -> [skill tags] for auto-assign
        "templates": {},    # name -> [ {title, assignee_skill, priority, depends_on_offset} ]
        "findings": [],     # security findings (vulnerabilities)
    }


# --- Batch A: Reliability config ---
BACKUP_DIR = Path(os.environ.get("TEAM_BACKUP_DIR", str(STATE_FILE.parent / "team_backups")))
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "10"))       # how many backups to retain
BACKUP_EVERY = int(os.environ.get("BACKUP_EVERY", "15"))     # snapshot every N writes
HEARTBEAT_STALE = int(os.environ.get("HEARTBEAT_STALE", "120"))  # ping considered stale after N s


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


# --- Batch A: backup / restore ---
def _make_backup(text: str) -> None:
    """Save a timestamped snapshot of the state and prune old ones. Best-effort."""
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        _atomic_write(BACKUP_DIR / f"state_{stamp}.json", text)
        backups = sorted(BACKUP_DIR.glob("state_*.json"))
        for old in backups[:-BACKUP_KEEP]:
            try:
                old.unlink()
            except OSError:
                pass
    except Exception:
        pass  # never let backup failure break a write


def _list_backups():
    if not BACKUP_DIR.exists():
        return []
    return sorted(BACKUP_DIR.glob("state_*.json"))


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
    # Batch A: bump write counter BEFORE serializing so it persists.
    state["_write_count"] = state.get("_write_count", 0) + 1
    do_backup = (state["_write_count"] % BACKUP_EVERY == 0)
    text = json.dumps(state, indent=2, ensure_ascii=False)
    _atomic_write(STATE_FILE, text)
    if do_backup:
        _make_backup(text)


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
def add_task(title: str, assignee: str = "", created_by: str = "",
             priority: str = "medium", depends_on: str = "", parent_id: int = 0) -> str:
    """Add a task to the shared board (usually the PM).

    Args:
        title: Short description.
        assignee: Role responsible. Empty = unassigned.
        created_by: Your role.
        priority: "high", "medium", or "low" (default medium).
        depends_on: Comma-separated task IDs that must be DONE before this can start, e.g. "1,2".
        parent_id: If this is a sub-task, the parent task's id (0 = top-level task).
    """
    priority = priority.lower().strip()
    if priority not in ("high", "medium", "low"):
        priority = "medium"
    deps = [int(x.strip()) for x in depends_on.split(",") if x.strip().isdigit()]

    def op(state):
        task_id = (max([t["id"] for t in state["tasks"]], default=0)) + 1
        state["tasks"].append({
            "id": task_id, "title": title, "assignee": assignee, "status": "todo",
            "created_by": created_by, "updated": _hms(),
            "priority": priority, "depends_on": deps, "parent_id": parent_id or 0,
        })
        _log(state, created_by or "system", f'added task #{task_id}: {title}')
        _touch_agent(state, created_by)
        return task_id

    task_id = _mutate(op)
    who = f" assigned to {assignee}" if assignee else " (unassigned)"
    extra = f" [priority: {priority}]"
    if deps:
        extra += f" [depends on: {', '.join('#'+str(d) for d in deps)}]"
    if parent_id:
        extra += f" [sub-task of #{parent_id}]"
    return f'Task #{task_id} added: "{title}"{who}.{extra}'


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
            return {"error": "notfound"}
        # Batch B: block starting a task whose dependencies aren't done yet.
        if status == "in_progress":
            deps = task.get("depends_on", [])
            unmet = []
            for d in deps:
                dep = next((t for t in state["tasks"] if t["id"] == d), None)
                if dep and dep.get("status") != "done":
                    unmet.append(f"#{d} ({dep.get('status', '?')})")
            if unmet:
                return {"error": "blocked", "unmet": unmet, "task": task}
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
            # Batch B: notify tasks that were waiting on this one.
            newly_ready = []
            for t in state["tasks"]:
                if task_id in t.get("depends_on", []) and t.get("status") == "todo":
                    remaining = [d for d in t["depends_on"]
                                 if next((x for x in state["tasks"] if x["id"] == d), {}).get("status") != "done"]
                    if not remaining:
                        newly_ready.append(t)
            for t in newly_ready:
                tgt = t.get("assignee") or "all"
                state["messages"].append({
                    "from": "system", "mention": tgt,
                    "text": f"Task #{t['id']} '{t['title']}' is now UNBLOCKED (all dependencies done) and ready to start.",
                    "time": _hms(),
                })
        return {"task": task}

    res = _mutate(op)
    if res.get("error") == "notfound":
        return f"No task #{task_id} found."
    if res.get("error") == "blocked":
        return (f"BLOCKED: task #{task_id} can't start yet — waiting on {', '.join(res['unmet'])}. "
                f"Finish those dependencies first.")
    task = res["task"]
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
    pri_mark = {"high": "!!", "medium": "  ", "low": "..", }
    pri_order = {"high": 0, "medium": 1, "low": 2}
    # Top-level tasks sorted by (priority, id); sub-tasks nested under parents.
    tops = [t for t in state["tasks"] if not t.get("parent_id")]
    tops.sort(key=lambda t: (pri_order.get(t.get("priority", "medium"), 1), t["id"]))
    def render(t, indent):
        mine = "  <-- YOURS" if my_role and t.get("assignee") == my_role else ""
        ic = icons.get(t["status"], "[ ]")
        pm = pri_mark.get(t.get("priority", "medium"), "  ")
        deps = t.get("depends_on", [])
        depstr = ""
        if deps:
            undone = [d for d in deps if next((x for x in state["tasks"] if x["id"] == d), {}).get("status") != "done"]
            depstr = f" (waiting on {', '.join('#'+str(d) for d in undone)})" if undone else " (deps met)"
        return f"  {'  '*indent}{pm} {ic} #{t['id']} {t['title']} ({t.get('assignee') or 'unassigned'}){depstr}{mine}"
    for t in tops:
        out.append(render(t, 0))
        subs = [s for s in state["tasks"] if s.get("parent_id") == t["id"]]
        subs.sort(key=lambda s: s["id"])
        for s in subs:
            out.append(render(s, 1))
    out.append("\n  legend: !! high  .. low  |  [ ]todo [~]doing [!]blocked [x]done")
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
# SECURITY — vulnerability findings workflow (audit a project with security agents)
# ============================================================================
# Lets security-role agents (SAST auditor, secret hunter, dependency scanner,
# auth reviewer, config auditor) record findings on a shared board, debate whether
# they are real, triage severity, assign fixes, verify fixes, and produce a report.
# This is DEFENSIVE: it helps the team find and fix weaknesses in their OWN project.

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_VALID_SEVERITY = set(_SEVERITY_ORDER)
_VALID_FSTATUS = {"open", "confirmed", "false_positive", "fixed", "verified", "wont_fix"}


@mcp.tool()
def report_finding(role: str, title: str, severity: str, location: str = "",
                   description: str = "", recommendation: str = "", category: str = "") -> str:
    """Record a security finding (a potential vulnerability) on the shared findings
    board. Security-role agents call this while auditing the project.

    Args:
        role: Your role, e.g. "SAST-Auditor", "Secret-Hunter".
        title: Short finding name, e.g. "SQL injection in login query".
        severity: critical | high | medium | low | info.
        location: Where it is, e.g. "src/auth/login.py:42".
        description: What the issue is and why it's a risk.
        recommendation: How to fix it.
        category: Optional class, e.g. "injection", "secrets", "auth", "config", "deps".
    """
    severity = severity.lower().strip()
    if severity not in _VALID_SEVERITY:
        return f"severity must be one of: {', '.join(_SEVERITY_ORDER)}."

    def op(state):
        fid = (max([f["id"] for f in state["findings"]], default=0)) + 1
        state["findings"].append({
            "id": fid, "title": title, "severity": severity, "location": location,
            "description": description, "recommendation": recommendation,
            "category": category.lower(), "status": "open",
            "reported_by": role, "assigned_to": "", "time": _now(),
        })
        state["messages"].append({
            "from": role, "mention": "Security-Lead",
            "text": f"[SECURITY {severity.upper()}] #{fid} {title}" + (f" @ {location}" if location else ""),
            "time": _hms()})
        _log(state, role, f"reported finding #{fid} ({severity}): {title}")
        _touch_agent(state, role)
        return fid
    fid = _mutate(op)
    return f"Finding #{fid} recorded [{severity.upper()}]: {title}. Security-Lead notified for triage."


@mcp.tool()
def list_findings(severity: str = "", status: str = "", category: str = "") -> str:
    """List security findings, sorted by severity (critical first). Filter optionally.

    Args:
        severity: Filter by severity (critical/high/medium/low/info). Empty = all.
        status: Filter by status (open/confirmed/false_positive/fixed/verified/wont_fix).
        category: Filter by category.
    """
    state = _load()
    findings = state.get("findings", [])
    sev = severity.lower().strip()
    st = status.lower().strip()
    cat = category.lower().strip()
    rows = [f for f in findings
            if (not sev or f["severity"] == sev)
            and (not st or f["status"] == st)
            and (not cat or f.get("category") == cat)]
    if not rows:
        return "No findings match." if findings else "No findings recorded yet."
    rows.sort(key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 9), f["id"]))
    icons = {"open": "🔴", "confirmed": "🟠", "false_positive": "⚪",
             "fixed": "🟡", "verified": "🟢", "wont_fix": "⚫"}
    out = [f"=== SECURITY FINDINGS ({len(rows)}) ==="]
    for f in rows:
        ic = icons.get(f["status"], "•")
        loc = f" @ {f['location']}" if f.get("location") else ""
        asg = f" -> @{f['assigned_to']}" if f.get("assigned_to") else ""
        out.append(f"  {ic} #{f['id']} [{f['severity'].upper()}] {f['title']}{loc} "
                   f"({f['status']}{asg})")
    return "\n".join(out)


@mcp.tool()
def get_finding(finding_id: int) -> str:
    """Read one finding in full (description, recommendation, status history).

    Args:
        finding_id: The finding id.
    """
    state = _load()
    f = next((x for x in state.get("findings", []) if x["id"] == finding_id), None)
    if not f:
        return f"No finding #{finding_id}."
    out = [f"#{f['id']} [{f['severity'].upper()}] {f['title']}",
           f"Status: {f['status']} | Category: {f.get('category') or '-'} | Reported by: {f['reported_by']}"]
    if f.get("location"):
        out.append(f"Location: {f['location']}")
    if f.get("assigned_to"):
        out.append(f"Assigned to: @{f['assigned_to']}")
    out.append("")
    out.append(f"Description: {f.get('description') or '(none)'}")
    out.append(f"Recommendation: {f.get('recommendation') or '(none)'}")
    out.append(f"Reported: {f['time']}")
    return "\n".join(out)


@mcp.tool()
def triage_finding(finding_id: int, status: str, by_role: str = "Security-Lead", note: str = "") -> str:
    """Triage a finding: confirm it's real, mark it a false positive, or set its
    lifecycle status. The Security-Lead usually does this after the team debates it.

    Args:
        finding_id: The finding id.
        status: open | confirmed | false_positive | fixed | verified | wont_fix.
        by_role: Your role.
        note: Optional note posted to the channel.
    """
    status = status.lower().strip()
    if status not in _VALID_FSTATUS:
        return f"status must be one of: {', '.join(sorted(_VALID_FSTATUS))}."

    def op(state):
        f = next((x for x in state["findings"] if x["id"] == finding_id), None)
        if not f:
            return {"error": True}
        old = f["status"]
        f["status"] = status
        msg = f"Finding #{finding_id} '{f['title']}': {old} -> {status}"
        if note:
            msg += f" ({note})"
        state["messages"].append({"from": by_role, "mention": "all", "text": f"[SECURITY] {msg}", "time": _hms()})
        _log(state, by_role, f"triaged finding #{finding_id} -> {status}")
        return {"f": f}
    res = _mutate(op)
    if res.get("error"):
        return f"No finding #{finding_id}."
    return f"Finding #{finding_id} status set to {status}."


@mcp.tool()
def assign_fix(finding_id: int, to_role: str, by_role: str = "Security-Lead") -> str:
    """Assign a confirmed finding to a developer agent to fix, creating a linked
    task and pinging them.

    Args:
        finding_id: The finding to fix.
        to_role: The agent who will fix it.
        by_role: Your role.
    """
    def op(state):
        f = next((x for x in state["findings"] if x["id"] == finding_id), None)
        if not f:
            return {"error": "notfound"}
        f["assigned_to"] = to_role
        tid = (max([t["id"] for t in state["tasks"]], default=0)) + 1
        pri = "high" if f["severity"] in ("critical", "high") else "medium"
        state["tasks"].append({
            "id": tid, "title": f"Fix [{f['severity']}] {f['title']}", "assignee": to_role,
            "status": "todo", "created_by": by_role, "updated": _hms(),
            "priority": pri, "depends_on": [], "parent_id": 0, "finding_id": finding_id})
        if to_role in state["agents"]:
            state["agents"][to_role]["status"] = "busy"
        state["messages"].append({
            "from": by_role, "mention": to_role,
            "text": f"@{to_role} please fix security finding #{finding_id} [{f['severity'].upper()}] "
                    f"'{f['title']}' (task #{tid}). Fix: {f.get('recommendation') or 'see finding'}",
            "time": _hms()})
        _log(state, by_role, f"assigned fix of finding #{finding_id} to {to_role}")
        return {"tid": tid, "f": f}
    res = _mutate(op)
    if res.get("error") == "notfound":
        return f"No finding #{finding_id}."
    return (f"Finding #{finding_id} assigned to @{to_role} (task #{res['tid']} created, "
            f"priority {'high' if res['f']['severity'] in ('critical','high') else 'medium'}).")


@mcp.tool()
def verify_fix(finding_id: int, verified: bool, by_role: str = "", note: str = "") -> str:
    """After a fix, a security agent re-checks and confirms whether the finding is
    actually resolved. Sets status to 'verified' or back to 'open'.

    Args:
        finding_id: The finding id.
        verified: True if the fix genuinely resolves it; False to reopen.
        by_role: Your role.
        note: Optional note (e.g. what was re-tested).
    """
    def op(state):
        f = next((x for x in state["findings"] if x["id"] == finding_id), None)
        if not f:
            return {"error": True}
        f["status"] = "verified" if verified else "open"
        verdict = "VERIFIED FIXED" if verified else "STILL VULNERABLE (reopened)"
        msg = f"Finding #{finding_id} '{f['title']}' re-checked: {verdict}"
        if note:
            msg += f" — {note}"
        state["messages"].append({"from": by_role or "system", "mention": "all", "text": f"[SECURITY] {msg}", "time": _hms()})
        _log(state, by_role or "system", f"verify finding #{finding_id}: {verdict}")
        return {"ok": True}
    res = _mutate(op)
    if res.get("error"):
        return f"No finding #{finding_id}."
    return f"Finding #{finding_id} {'verified as fixed' if verified else 'reopened — fix did not hold'}."


@mcp.tool()
def security_report(by_role: str = "") -> str:
    """Generate a full security report (findings by severity, status summary,
    open criticals) and save it as Markdown next to the state file.

    Args:
        by_role: Your role.
    """
    state = _load()
    findings = state.get("findings", [])
    if not findings:
        return "No findings to report yet."
    by_sev = {}
    by_status = {}
    for f in findings:
        by_sev[f["severity"]] = by_sev.get(f["severity"], 0) + 1
        by_status[f["status"]] = by_status.get(f["status"], 0) + 1
    lines = ["# Security Audit Report", f"_Generated {_now()}_\n"]
    lines.append("## Summary by severity")
    for sev in ["critical", "high", "medium", "low", "info"]:
        if by_sev.get(sev):
            lines.append(f"- {sev.upper()}: {by_sev[sev]}")
    lines.append("\n## Summary by status")
    for st, n in by_status.items():
        lines.append(f"- {st}: {n}")
    # Highlight unresolved criticals/highs
    urgent = [f for f in findings if f["severity"] in ("critical", "high")
              and f["status"] not in ("fixed", "verified", "false_positive", "wont_fix")]
    if urgent:
        lines.append("\n## ⚠️ UNRESOLVED CRITICAL / HIGH")
        for f in urgent:
            lines.append(f"- #{f['id']} [{f['severity'].upper()}] {f['title']} "
                         f"@ {f.get('location') or '?'} ({f['status']})")
    lines.append("\n## All findings")
    fs = sorted(findings, key=lambda f: (_SEVERITY_ORDER.get(f["severity"], 9), f["id"]))
    for f in fs:
        lines.append(f"\n### #{f['id']} [{f['severity'].upper()}] {f['title']}")
        lines.append(f"- Status: {f['status']} | Category: {f.get('category') or '-'} "
                     f"| Reported by: {f['reported_by']}"
                     + (f" | Assigned: @{f['assigned_to']}" if f.get('assigned_to') else ""))
        if f.get("location"):
            lines.append(f"- Location: `{f['location']}`")
        if f.get("description"):
            lines.append(f"- Issue: {f['description']}")
        if f.get("recommendation"):
            lines.append(f"- Fix: {f['recommendation']}")
    report = "\n".join(lines)
    path = STATE_FILE.parent / f"security_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    try:
        _atomic_write(path, report)
    except Exception as e:
        return f"Generated report but couldn't save: {e}\n\n{report}"
    _mutate(lambda s: _log(s, by_role or "system", "generated security report"))
    open_crit = sum(1 for f in urgent)
    return (f"Security report saved to {path}. "
            f"{len(findings)} findings, {open_crit} unresolved critical/high.\n\n{report}")


@mcp.tool()
def start_security_audit(project_dir: str, by_role: str = "Security-Lead", terminal: str = "wt") -> str:
    """Kick off a security audit: spawn a standard set of security agents (SAST
    auditor, secret hunter, dependency scanner, config auditor) for the project,
    each auto-joining and ready to report findings. Windows only for spawning.

    Args:
        project_dir: Absolute path of the project to audit.
        by_role: Your role (the security lead).
        terminal: "wt" or "cmd".
    """
    roles = ["SAST-Auditor", "Secret-Hunter", "Dependency-Scanner", "Config-Auditor"]
    if sys.platform != "win32":
        return ("start_security_audit spawns terminals (Windows only). On other systems, "
                "manually open terminals for these roles: " + ", ".join(roles) +
                ". Each should join_team then audit and report_finding.")
    spawned, skipped = [], []
    state = _load()
    for r in roles:
        if r in state.get("spawned", {}):
            skipped.append(r)
            continue
        if len(state.get("spawned", {})) + len(spawned) >= MAX_AGENTS:
            skipped.append(r + " (limit)")
            continue
        try:
            pid, title = _spawn_windows(r, project_dir, terminal)
            _mutate(lambda s, rr=r, pp=pid, tt=title: s["spawned"].__setitem__(
                rr, {"pid": pp, "title": tt, "terminal": terminal, "started": _now()}))
            spawned.append(r)
        except Exception as e:
            skipped.append(f"{r} (error: {e})")
    _mutate(lambda s: _log(s, by_role, f"started security audit: spawned {spawned}"))
    msg = f"Security audit started. Spawned: {', '.join(spawned) or 'none'}."
    if skipped:
        msg += f" Skipped: {', '.join(skipped)}."
    msg += " Each agent will audit the project and call report_finding."
    return msg


# ============================================================================
# BATCH E — INTEGRATION (network mode, Obsidian sync, git, webhooks)
# ============================================================================

import urllib.request as _urlreq
import urllib.error as _urlerr

OBSIDIAN_VAULT = os.environ.get("OBSIDIAN_VAULT", "")  # path to a vault folder, optional
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")        # optional default webhook (Slack/Discord/etc)


@mcp.tool()
def network_info() -> str:
    """Explain how to run this team across MULTIPLE MACHINES, and show whether the
    live dashboard (which already serves over HTTP) is reachable on the network.

    The coordination state is a shared file. For multiple machines, point every
    client's TEAM_STATE_FILE at the SAME file on a shared drive / synced folder
    (e.g. a network share, Dropbox, or Syncthing). The file lock keeps it safe.
    """
    out = ["=== NETWORK / MULTI-MACHINE ==="]
    out.append(f"State file: {STATE_FILE}")
    out.append("To share across machines, put TEAM_STATE_FILE on a shared/synced path")
    out.append("(network share, Dropbox, Syncthing). File locking keeps writes safe.")
    out.append("")
    if _dashboard_server is not None:
        port = _dashboard_server.server_address[1]
        out.append(f"Dashboard is LIVE on port {port}.")
        out.append(f"To view from another device on your LAN, restart it bound to 0.0.0.0")
        out.append(f"(set DASHBOARD_HOST=0.0.0.0) and browse http://<this-machine-ip>:{port}/")
    else:
        out.append("Dashboard not running. Start it with start_dashboard.")
    return "\n".join(out)


@mcp.tool()
def obsidian_sync(target: str = "notes", by_role: str = "") -> str:
    """Export project knowledge to an Obsidian vault as Markdown files (so it shows
    up in Obsidian with backlinks/graph). Requires OBSIDIAN_VAULT env var set to a
    vault folder path.

    Args:
        target: What to export — "notes" (project notes), "brain" (second brain),
                or "all".
        by_role: Your role.
    """
    if not OBSIDIAN_VAULT:
        return ("OBSIDIAN_VAULT is not set. Add it when registering the server, e.g. "
                "env OBSIDIAN_VAULT=D:\\MyVault\\TeamMCP ... then call obsidian_sync again.")
    vault = Path(OBSIDIAN_VAULT)
    try:
        vault.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return f"Cannot create/access vault folder {vault}: {e}"
    written = 0
    state = _load()
    if target in ("notes", "all"):
        for n in state.get("notes", []):
            tags = " ".join(f"#{t}" for t in n.get("tags", []))
            md = f"---\nid: {n['id']}\nby: {n.get('by','')}\ntime: {n['time']}\n---\n\n{n['text']}\n\n{tags}\n"
            try:
                _atomic_write(vault / f"note-{n['id']}.md", md)
                written += 1
            except Exception:
                pass
    if target in ("brain", "all"):
        try:
            brain = _brain_load()
            for n in brain.get("notes", []):
                links = [l for l in brain.get("links", []) if l["from"] == n["id"] or l["to"] == n["id"]]
                backlinks = ""
                for l in links:
                    other = l["to"] if l["from"] == n["id"] else l["from"]
                    on = next((x["title"] for x in brain["notes"] if x["id"] == other), str(other))
                    backlinks += f"- [[brain-{other}|{on}]]\n"
                tags = " ".join(f"#{t}" for t in n.get("tags", []))
                cat = f"category: {n.get('category','')}" if n.get("category") else ""
                md = (f"---\nid: {n['id']}\n{cat}\ntime: {n['time']}\n---\n\n"
                      f"# {n['title']}\n\n{n['content']}\n\n{tags}\n\n"
                      f"## Links\n{backlinks or '(none)'}\n")
                _atomic_write(vault / f"brain-{n['title'][:40].replace('/', '-')}-{n['id']}.md", md)
                written += 1
        except Exception:
            pass
    _mutate(lambda s: _log(s, by_role or "system", f"obsidian sync ({target}): {written} files"))
    return f"Synced {written} note(s) to Obsidian vault: {vault}. Open it in Obsidian to see them with the graph view."


@mcp.tool()
def git_link(task_id: int, branch: str = "", commit: str = "", by_role: str = "") -> str:
    """Link a git branch or commit to a task, so the board records which code
    corresponds to which work item.

    Args:
        task_id: The task to link.
        branch: Branch name (optional).
        commit: Commit hash or message (optional).
        by_role: Your role.
    """
    def op(state):
        task = next((t for t in state["tasks"] if t["id"] == task_id), None)
        if not task:
            return {"error": True}
        task.setdefault("git", {})
        if branch:
            task["git"]["branch"] = branch
        if commit:
            task["git"].setdefault("commits", []).append({"ref": commit, "time": _hms()})
        state["messages"].append({
            "from": by_role or "system", "mention": "all",
            "text": f"Task #{task_id} linked to git" + (f" branch '{branch}'" if branch else "") +
                    (f" commit {commit[:40]}" if commit else ""),
            "time": _hms()})
        _log(state, by_role or "system", f"git-linked task #{task_id}")
        return {"git": task["git"]}
    res = _mutate(op)
    if res.get("error"):
        return f"No task #{task_id}."
    return f"Task #{task_id} git link updated: {json.dumps(res['git'])}"


@mcp.tool()
def suggest_worktrees(project_dir: str) -> str:
    """Generate ready-to-run git worktree commands so each active agent gets its own
    isolated working directory (preventing file conflicts). Pairs with the MCP board
    for coordination.

    Args:
        project_dir: The main repo path, e.g. D:\\AU-HRM\\lankabook\\acc.lankabook.lk
    """
    state = _load()
    roles = [r for r, a in state.get("agents", {}).items()
             if r != "PM" and a.get("status") in ("online", "idle", "busy")]
    if not roles:
        return "No non-PM agents online to create worktrees for."
    out = [f"# Run these from {project_dir} to give each agent an isolated worktree:"]
    for r in roles:
        b = r.lower()
        out.append(f"git worktree add ../{Path(project_dir).name}-{b} -b {b}")
    out.append("\n# Then start each agent's client in its own worktree folder.")
    out.append("# MCP board = coordination; worktrees = file isolation.")
    return "\n".join(out)


@mcp.tool()
def webhook_notify(event: str, url: str = "", by_role: str = "") -> str:
    """Send a notification to an external webhook (Slack/Discord/Teams/custom). Use
    for milestones like 'all tasks done' or 'debate decided'. Posts a simple JSON
    payload {text: event}. Uses WEBHOOK_URL env var if no url is given.

    Args:
        event: The message text to send.
        url: Webhook URL (optional if WEBHOOK_URL env var is set).
        by_role: Your role.
    """
    target = url or WEBHOOK_URL
    if not target:
        return ("No webhook URL. Pass url=... or set WEBHOOK_URL env var when registering "
                "the server. (Slack/Discord both accept a JSON {text/content} POST.)")
    # Support both Slack ("text") and Discord ("content") shapes.
    payload = json.dumps({"text": event, "content": event}).encode("utf-8")
    req = _urlreq.Request(target, data=payload, headers={"Content-Type": "application/json"})
    try:
        with _urlreq.urlopen(req, timeout=10) as resp:
            code = resp.getcode()
    except _urlerr.HTTPError as e:
        code = e.code
    except Exception as e:
        return f"Webhook failed: {e}"
    _mutate(lambda s: _log(s, by_role or "system", f"webhook sent (HTTP {code})"))
    return f"Webhook sent (HTTP {code}): {event}"


# ============================================================================
# BATCH D — INTELLIGENCE (semantic search, auto-summarize, conflict detection,
#                         smart routing, debate scoring)
# ============================================================================

import re as _re
import math as _math

_STOP = set("a an the of to in on for and or but is are was were be been being with "
            "this that these those it its as at by from into we you they i he she "
            "do does did has have had will would can could should our your their".split())


def _stem(w: str) -> str:
    """Very light stemmer so 'login/logs', 'security/securely', 'database/databases' match."""
    for suf in ("ation", "izing", "ising", "ingly", "edly", "ing", "ies", "ied", "ly", "es", "ed", "s"):
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _tokenize(text: str):
    words = [w for w in _re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 1]
    return [_stem(w) for w in words]


def _similarity(a: str, b: str) -> float:
    """Lightweight cosine-style similarity over token frequency. No dependencies."""
    ta, tb = _tokenize(a), _tokenize(b)
    if not ta or not tb:
        return 0.0
    fa, fb = {}, {}
    for w in ta:
        fa[w] = fa.get(w, 0) + 1
    for w in tb:
        fb[w] = fb.get(w, 0) + 1
    common = set(fa) & set(fb)
    dot = sum(fa[w] * fb[w] for w in common)
    na = _math.sqrt(sum(v * v for v in fa.values()))
    nb = _math.sqrt(sum(v * v for v in fb.values()))
    return dot / (na * nb) if na and nb else 0.0


@mcp.tool()
def smart_search(query: str, top_k: int = 5) -> str:
    """Meaning-based search across BOTH project notes and the second brain, ranked
    by relevance (not just exact keyword match). Use this to recall knowledge
    cheaply instead of re-reading files or scrolling history.

    Args:
        query: What you're looking for, in natural words.
        top_k: How many top results to return (default 5).
    """
    state = _load()
    candidates = []
    for n in state.get("notes", []):
        candidates.append(("note", n["id"], n["text"], _similarity(query, n["text"])))
    try:
        brain = _brain_load()
        for n in brain.get("notes", []):
            blob = f"{n.get('title','')} {n.get('content','')}"
            candidates.append(("brain", n["id"], f"{n.get('title','')}: {n.get('content','')[:100]}", _similarity(query, blob)))
    except Exception:
        pass
    candidates = [c for c in candidates if c[3] > 0]
    candidates.sort(key=lambda c: c[3], reverse=True)
    if not candidates:
        return f"No relevant matches for '{query}'."
    out = [f"Top matches for '{query}':"]
    for src, cid, text, score in candidates[:top_k]:
        out.append(f"  [{src} #{cid}] (relevance {score:.2f}) {text[:120]}")
    return "\n".join(out)


@mcp.tool()
def suggest_route(text: str) -> str:
    """Smart routing: given a message or task description, suggest which agent is
    the best fit based on their declared skills and current availability. The PM
    can use this to decide who to assign or @mention.

    Args:
        text: The message/task content to route.
    """
    state = _load()
    ranked = []
    for role, a in state.get("agents", {}).items():
        skills = state.get("skills", {}).get(role, [])
        skill_blob = " ".join(skills) + " " + role
        score = _similarity(text, skill_blob)
        # tiny boost for idle availability
        if a.get("status") == "idle":
            score += 0.05
        ranked.append((score, role, a.get("status", "online")))
    if not ranked:
        return "No agents registered yet."
    ranked.sort(reverse=True)
    out = ["Routing suggestion (best fit first):"]
    for score, role, status in ranked[:3]:
        out.append(f"  @{role} ({status}) — fit {score:.2f}")
    best = ranked[0][1]
    out.append(f"\nSuggested: @{best}")
    return "\n".join(out)


@mcp.tool()
def check_conflicts() -> str:
    """Conflict detection: flag risks where agents may collide — multiple agents
    assigned overlapping work, or several in_progress tasks touching similar areas
    (by title similarity). Helps avoid two agents editing the same thing.

    """
    state = _load()
    tasks = state.get("tasks", [])
    active = [t for t in tasks if t.get("status") == "in_progress"]
    warnings = []
    # Same assignee with multiple in-progress tasks
    by_assignee = {}
    for t in active:
        by_assignee.setdefault(t.get("assignee"), []).append(t)
    for who, ts in by_assignee.items():
        if who and len(ts) > 1:
            warnings.append(f"@{who} has {len(ts)} tasks in progress at once: " +
                            ", ".join(f"#{t['id']}" for t in ts))
    # Similar-titled active tasks by different agents (possible overlap)
    for i in range(len(active)):
        for j in range(i + 1, len(active)):
            t1, t2 = active[i], active[j]
            if t1.get("assignee") != t2.get("assignee"):
                sim = _similarity(t1["title"], t2["title"])
                if sim > 0.35:
                    warnings.append(f"Possible overlap (similarity {sim:.2f}): "
                                    f"#{t1['id']} '{t1['title']}' (@{t1.get('assignee')}) vs "
                                    f"#{t2['id']} '{t2['title']}' (@{t2.get('assignee')})")
    if not warnings:
        return "No conflicts detected. Work is well-separated."
    return "⚠️ CONFLICTS / RISKS:\n" + "\n".join("  " + w for w in warnings) + \
           "\n\nTip: give each agent its own git worktree, or serialize overlapping tasks with dependencies."


@mcp.tool()
def context_checkpoint(role: str, work_done: str, decisions: str = "", next_steps: str = "") -> str:
    """Auto-summarize helper for TOKEN SAVING. Call this when your context window is
    getting long: it saves a structured summary you can reload after /clear, so you
    resume cheaply instead of carrying the whole history. Returns the saved summary.

    Args:
        role: Your role.
        work_done: What you've completed so far.
        decisions: Key decisions made (optional).
        next_steps: What remains to do (optional).
    """
    parts = [f"[{role} checkpoint @ {_now()}]", f"DONE: {work_done}"]
    if decisions:
        parts.append(f"DECISIONS: {decisions}")
    if next_steps:
        parts.append(f"NEXT: {next_steps}")
    summary = "\n".join(parts)

    def op(state):
        sid = len(state["summaries"]) + 1
        state["summaries"].append({"id": sid, "text": summary, "by": role, "time": _now()})
        _log(state, role, f"context checkpoint #{sid}")
        return sid
    sid = _mutate(op)
    return (f"Checkpoint #{sid} saved. You can now /clear and call load_summary latest to "
            f"resume with minimal tokens.\n\n{summary}")


@mcp.tool()
def score_debate() -> str:
    """Debate quality scoring: rate each agent's contribution in the current debate
    by argument depth (length/specificity of reasoning) and engagement (critiques
    given). Helps the judge weigh who argued substantively vs who just agreed.

    """
    state = _load()
    d = state.get("debate")
    if not d:
        return "No active debate."
    scores = {}
    # proposals: reward substantive reasoning
    for p in d.get("proposals", []):
        s = scores.setdefault(p["role"], {"proposal": 0, "critiques": 0, "depth": 0})
        s["proposal"] += 1
        s["depth"] += len(_tokenize(p.get("argument", "")))
    # critiques: reward engagement, especially reasoned disagreement
    for c in d.get("critiques", []):
        s = scores.setdefault(c["role"], {"proposal": 0, "critiques": 0, "depth": 0})
        s["critiques"] += 1
        depth = len(_tokenize(c.get("argument", "")))
        s["depth"] += depth + (3 if not c.get("agree") else 0)  # reasoned disagreement weighted
    if not scores:
        return "No contributions to score yet."
    ranked = sorted(scores.items(), key=lambda x: x[1]["depth"], reverse=True)
    out = ["=== DEBATE CONTRIBUTION SCORES ==="]
    for role, s in ranked:
        out.append(f"  @{role}: depth {s['depth']} | {s['proposal']} proposal(s), {s['critiques']} critique(s)")
    out.append("\n(Higher depth = more substantive reasoning. Judge: weigh substance over volume.)")
    return "\n".join(out)


# ============================================================================
# BATCH C — OBSERVABILITY (dashboard, metrics, export, timeline)
# ============================================================================

DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", "8765"))
DASHBOARD_HOST = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
_dashboard_thread = None
_dashboard_server = None


def _compute_metrics(state: dict) -> dict:
    tasks = state.get("tasks", [])
    by_status = {}
    for t in tasks:
        by_status[t.get("status", "todo")] = by_status.get(t.get("status", "todo"), 0) + 1
    agents = state.get("agents", {})
    online = sum(1 for a in agents.values() if a.get("status") in ("online", "idle", "busy"))
    debates_decided = sum(1 for n in state.get("notes", []) if "decision" in n.get("tags", []))
    return {
        "agents_total": len(agents),
        "agents_online": online,
        "messages": len(state.get("messages", [])) + state.get("archived_messages", 0),
        "tasks_total": len(tasks),
        "tasks_done": by_status.get("done", 0),
        "tasks_in_progress": by_status.get("in_progress", 0),
        "tasks_todo": by_status.get("todo", 0),
        "tasks_blocked": by_status.get("blocked", 0),
        "notes": len(state.get("notes", [])),
        "decisions": debates_decided,
        "active_debate": bool(state.get("debate")),
    }


@mcp.tool()
def metrics() -> str:
    """Show team metrics: agent count, message volume, task breakdown, decisions made.
    A quick health/progress snapshot of the whole project.
    """
    m = _compute_metrics(_load())
    out = ["=== TEAM METRICS ==="]
    out.append(f"  Agents: {m['agents_online']}/{m['agents_total']} online")
    out.append(f"  Messages: {m['messages']}")
    out.append(f"  Tasks: {m['tasks_total']} total — "
               f"{m['tasks_done']} done, {m['tasks_in_progress']} in progress, "
               f"{m['tasks_todo']} todo, {m['tasks_blocked']} blocked")
    if m['tasks_total']:
        pct = round(100 * m['tasks_done'] / m['tasks_total'])
        out.append(f"  Progress: {pct}% of tasks done")
    out.append(f"  Notes: {m['notes']} | Decisions recorded: {m['decisions']}")
    out.append(f"  Active debate: {'yes' if m['active_debate'] else 'no'}")
    return "\n".join(out)


@mcp.tool()
def timeline(last_n: int = 30) -> str:
    """Show a chronological timeline of the whole project (joins, tasks, decisions,
    recoveries) from the activity log.

    Args:
        last_n: How many recent events to show (default 30).
    """
    state = _load()
    log = state.get("activity_log", [])
    if not log:
        return "Timeline is empty."
    recent = log[-last_n:]
    out = [f"=== PROJECT TIMELINE (last {len(recent)} of {len(log)}) ==="]
    for e in recent:
        out.append(f"  {e['time']}  {e['who']}: {e['action']}")
    return "\n".join(out)


@mcp.tool()
def export_report(by_role: str = "") -> str:
    """Generate a Markdown project report (metrics, task board, decisions, recent
    timeline) and save it next to the state file. Good for sharing or archiving.

    Args:
        by_role: Your role.
    """
    state = _load()
    m = _compute_metrics(state)
    lines = []
    lines.append(f"# Team Project Report")
    lines.append(f"_Generated {_now()}_\n")
    lines.append("## Metrics")
    lines.append(f"- Agents online: {m['agents_online']}/{m['agents_total']}")
    lines.append(f"- Messages: {m['messages']}")
    lines.append(f"- Tasks: {m['tasks_total']} ({m['tasks_done']} done, "
                 f"{m['tasks_in_progress']} in progress, {m['tasks_todo']} todo, {m['tasks_blocked']} blocked)")
    if m['tasks_total']:
        lines.append(f"- Progress: {round(100*m['tasks_done']/m['tasks_total'])}%")
    lines.append(f"- Decisions recorded: {m['decisions']}\n")
    lines.append("## Task Board")
    icons = {"todo": "[ ]", "in_progress": "[~]", "blocked": "[!]", "done": "[x]"}
    for t in state.get("tasks", []):
        sub = "  " if t.get("parent_id") else ""
        lines.append(f"- {sub}{icons.get(t.get('status'),'[ ]')} #{t['id']} {t['title']} "
                     f"({t.get('assignee') or 'unassigned'}, {t.get('priority','medium')})")
    lines.append("\n## Decisions")
    decs = [n for n in state.get("notes", []) if "decision" in n.get("tags", [])]
    if decs:
        for n in decs:
            lines.append(f"- {n['text']}")
    else:
        lines.append("- (none yet)")
    lines.append("\n## Recent Activity")
    for e in state.get("activity_log", [])[-20:]:
        lines.append(f"- {e['time']} — {e['who']}: {e['action']}")
    report = "\n".join(lines)
    path = STATE_FILE.parent / f"team_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    try:
        _atomic_write(path, report)
    except Exception as e:
        return f"Generated report but couldn't save: {e}\n\n{report}"
    _mutate(lambda s: _log(s, by_role or "system", "exported report"))
    return f"Report saved to {path}\n\n{report}"


def _dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Team Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff;--green:#3fb950;--amber:#d29922;--red:#f85149;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:16px;font-size:14px}
h1{font-size:18px;margin-bottom:4px}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);margin-bottom:10px}
.metrics{display:flex;flex-wrap:wrap;gap:14px}
.metric{min-width:70px}
.metric .n{font-size:22px;font-weight:600}
.metric .l{font-size:11px;color:var(--dim)}
.agent,.task,.msg,.ev{padding:6px 0;border-bottom:1px solid var(--border);font-size:13px}
.agent:last-child,.task:last-child,.msg:last-child,.ev:last-child{border:none}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.online{background:var(--green)}.idle{background:var(--accent)}.busy{background:var(--amber)}.offline{background:var(--dim)}
.pill{font-size:10px;padding:1px 6px;border-radius:10px;background:#21262d;color:var(--dim);margin-left:4px}
.s-done{color:var(--green)}.s-progress{color:var(--amber)}.s-blocked{color:var(--red)}.s-todo{color:var(--dim)}
.row{padding:6px 0;border-bottom:1px solid var(--border);font-size:13px}.row:last-child{border:none}
.sev-critical{color:#f85149}.sev-high{color:#ff7b72}.sev-medium{color:var(--amber)}.sev-low{color:var(--green)}.sev-info{color:var(--dim)}
.msg .who{color:var(--accent);font-weight:600}
.mention{color:var(--amber)}
.scroll{max-height:340px;overflow-y:auto}
.bar{height:6px;background:#21262d;border-radius:3px;overflow:hidden;margin-top:8px}
.bar>div{height:100%;background:var(--green)}
.full{grid-column:1/-1}
</style></head><body>
<h1>🤖 Team Dashboard <a href="/gateway" style="font-size:13px;color:#58a6ff;text-decoration:none">Gateway →</a></h1>
<div class="sub" id="updated">connecting…</div>
<div class="grid">
  <div class="card full"><h2>Metrics</h2><div class="metrics" id="metrics"></div><div class="bar"><div id="progbar" style="width:0%"></div></div></div>
  <div class="card"><h2>Agents</h2><div id="agents"></div></div>
  <div class="card"><h2>Tasks</h2><div class="scroll" id="tasks"></div></div>
  <div class="card"><h2>Channel</h2><div class="scroll" id="messages"></div></div>
  <div class="card"><h2>Debate</h2><div id="debate"></div></div>
  <div class="card"><h2>Timeline</h2><div class="scroll" id="timeline"></div></div>
  <div class="card"><h2>Security findings</h2><div class="scroll" id="findings"></div></div>
  <div class="card"><h2>Memory</h2><div id="memory"></div></div>
  <div class="card"><h2>Reliability &amp; health</h2><div class="scroll" id="reliability"></div></div>
  <div class="card"><h2>Workflow</h2><div class="scroll" id="workflow"></div></div>
</div>
<script>
const E=id=>document.getElementById(id);
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function men(s){return esc(s).replace(/@(\\w+)/g,'<span class="mention">@$1</span>');}
async function tick(){
 try{
  const r=await fetch('/api/state'); const d=await r.json();
  const m=d.metrics;
  E('metrics').innerHTML=[['agents_online','Online'],['messages','Messages'],['tasks_total','Tasks'],['tasks_done','Done'],['tasks_in_progress','Doing'],['decisions','Decisions']]
    .map(([k,l])=>`<div class="metric"><div class="n">${m[k]}</div><div class="l">${l}</div></div>`).join('');
  E('progbar').style.width=(m.tasks_total?Math.round(100*m.tasks_done/m.tasks_total):0)+'%';
  E('agents').innerHTML=Object.values(d.agents).map(a=>`<div class="agent"><span class="dot ${a.status||'online'}"></span>${esc(a.name)} <span class="pill">${a.role}</span> <span class="pill">${a.status||'online'}</span></div>`).join('')||'<div class="agent">none</div>';
  E('tasks').innerHTML=d.tasks.map(t=>{const c={done:'s-done',in_progress:'s-progress',blocked:'s-blocked',todo:'s-todo'}[t.status]||'s-todo';const sub=t.parent_id?'&nbsp;&nbsp;↳ ':'';return `<div class="task">${sub}<span class="${c}">●</span> #${t.id} ${esc(t.title)} <span class="pill">${t.assignee||'—'}</span> <span class="pill">${t.priority||'med'}</span></div>`;}).join('')||'<div class="task">none</div>';
  E('messages').innerHTML=d.messages.slice(-40).map(x=>`<div class="msg"><span class="who">${esc(x.from)}</span>: ${men(x.text)}</div>`).reverse().join('')||'<div class="msg">none</div>';
  if(d.debate){const dd=d.debate;E('debate').innerHTML=`<b>${esc(dd.topic)}</b><br><span class="pill">${dd.phase}</span> <span class="pill">round ${dd.round+1}/${dd.max_rounds}</span> <span class="pill">judge ${dd.judge_role}</span>`+ (dd.verdict?`<br><br>✅ <b>${esc(dd.verdict.decision)}</b>`:'');}else{E('debate').innerHTML='<span class="s-todo">No active debate</span>';}
  E('timeline').innerHTML=d.timeline.slice(-25).map(e=>`<div class="ev"><span class="s-todo">${e.time.split(' ')[1]||e.time}</span> ${esc(e.who)}: ${esc(e.action)}</div>`).reverse().join('')||'<div class="ev">none</div>';
  const F=d.findings||[],closed=['fixed','verified','false_positive','wont_fix'];
  const sev={critical:0,high:0,medium:0,low:0,info:0};F.forEach(f=>{if(sev[f.severity]!==undefined)sev[f.severity]++;});
  const openF=F.filter(f=>!closed.includes(f.status)).length;
  E('findings').innerHTML=F.length?`<div class="row sub">${F.length} total · ${openF} open · <span class="sev-critical">${sev.critical}C</span> <span class="sev-high">${sev.high}H</span> <span class="sev-medium">${sev.medium}M</span> <span class="sev-low">${sev.low}L</span></div>`+F.slice(-12).reverse().map(f=>`<div class="row"><span class="sev-${f.severity}">●</span> #${f.id} ${esc(f.title)} <span class="pill">${f.status}</span> ${f.location?'<span class="sub">'+esc(f.location)+'</span>':''}</div>`).join(''):'<div class="row sub">no findings — clean ✓</div>';
  const M=d.memory||{};
  E('memory').innerHTML=`<div class="metrics">`+[['notes','Notes'],['facts','Facts'],['summaries','Summaries'],['brain','Brain']].map(([k,l])=>`<div class="metric"><div class="n">${M[k]||0}</div><div class="l">${l}</div></div>`).join('')+`</div>`;
  const R=d.reliability||{},ag=Object.values(d.agents),up=ag.filter(a=>['online','idle','busy'].includes(a.status||'online')).length,rc=Object.entries(R.receipts||{});
  E('reliability').innerHTML=`<div class="row">Backups: <b>${R.backups||0}</b> ${R.last_backup?'<span class="sub">latest '+esc(R.last_backup)+'</span>':''}</div><div class="row">Agents healthy: <b>${up}/${ag.length}</b></div><div class="row sub">Read receipts (last msg seen):</div>`+(rc.length?rc.map(([r,i])=>`<div class="row">${esc(r)} <span class="pill">@${i}</span></div>`).join(''):'<div class="row sub">none yet</div>');
  const W=d.workflow||{},sk=Object.entries(W.skills||{}),vt=Object.entries(W.votes||{});
  E('workflow').innerHTML=`<div class="row sub">Skills:</div>`+(sk.length?sk.map(([r,s])=>`<div class="row">${esc(r)}: ${(s||[]).map(x=>'<span class="pill">'+esc(x)+'</span>').join(' ')}</div>`).join(''):'<div class="row sub">none set</div>')+`<div class="row sub">Templates: ${(W.templates||[]).map(esc).join(', ')||'none'}</div>`+(vt.length?`<div class="row sub">Votes:</div>`+vt.map(([r,v])=>`<div class="row">${esc(r)} → ${esc(v.choice)}</div>`).join(''):'');
  E('updated').textContent='live · updated '+new Date().toLocaleTimeString();
 }catch(e){E('updated').textContent='disconnected — retrying…';}
}
tick();setInterval(tick,2000);
</script></body></html>"""


class _DashHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # silence

    def do_GET(self):
        if self.path.startswith("/api/state"):
            state = _read_state_unlocked()
            try:
                brain_notes = len(_brain_load().get("notes", []))
            except Exception:
                brain_notes = 0
            backups = _list_backups()
            debate = state.get("debate") or {}
            payload = {
                "metrics": _compute_metrics(state),
                "agents": state.get("agents", {}),
                "tasks": state.get("tasks", []),
                "messages": state.get("messages", []),
                "debate": state.get("debate"),
                "timeline": state.get("activity_log", []),
                "findings": state.get("findings", []),
                "memory": {
                    "notes": len(state.get("notes", [])),
                    "facts": len(state.get("facts", {})),
                    "summaries": len(state.get("summaries", [])),
                    "brain": brain_notes,
                },
                "reliability": {
                    "backups": len(backups),
                    "last_backup": (backups[-1].name if backups else ""),
                    "receipts": state.get("read_state", {}),
                },
                "workflow": {
                    "skills": state.get("skills", {}),
                    "templates": list(state.get("templates", {}).keys()),
                    "votes": debate.get("votes", {}),
                },
            }
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/gateway"):
            body = json.dumps(_gw_dashboard_payload()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/gateway"):
            body = _gateway_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = _dashboard_html().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


@mcp.tool()
def start_dashboard(port: int = 0) -> str:
    """Launch a live web dashboard showing the board, channel, agents, debate, and
    timeline, auto-refreshing every 2s. Open the printed URL in your browser.

    Args:
        port: Port to serve on (0 = use DASHBOARD_PORT, default 8765).
    """
    global _dashboard_thread, _dashboard_server
    if _dashboard_server is not None:
        return f"Dashboard already running at http://localhost:{_dashboard_server.server_address[1]}/"
    use_port = port or DASHBOARD_PORT
    try:
        _dashboard_server = ThreadingHTTPServer((DASHBOARD_HOST, use_port), _DashHandler)
    except OSError as e:
        return f"Could not start dashboard on port {use_port}: {e}. Try a different port."
    _dashboard_thread = threading.Thread(target=_dashboard_server.serve_forever, daemon=True)
    _dashboard_thread.start()
    return (f"Dashboard live at http://localhost:{use_port}/ — open it in your browser. "
            f"It auto-refreshes every 2 seconds.")


@mcp.tool()
def stop_dashboard() -> str:
    """Stop the live web dashboard if it's running."""
    global _dashboard_server, _dashboard_thread
    if _dashboard_server is None:
        return "Dashboard is not running."
    _dashboard_server.shutdown()
    _dashboard_server.server_close()
    _dashboard_server = None
    _dashboard_thread = None
    return "Dashboard stopped."


# ============================================================================
# BATCH B — WORKFLOW (skills/auto-assign, templates, voting, sub-task helpers)
# ============================================================================

@mcp.tool()
def set_skills(role: str, skills: str) -> str:
    """Declare what an agent is good at, so the PM can auto-assign matching tasks.

    Args:
        role: The agent role.
        skills: Space/comma-separated skill tags, e.g. "backend api database".
    """
    skill_list = [s.strip().lower() for s in skills.replace(",", " ").split() if s.strip()]

    def op(state):
        state["skills"][role] = skill_list
        _log(state, role, f"set skills: {', '.join(skill_list)}")
    _mutate(op)
    return f"@{role} skills set: {', '.join(skill_list) or '(none)'}."


@mcp.tool()
def auto_assign(task_id: int, by_role: str = "PM") -> str:
    """Auto-assign a task to the best-matching IDLE agent based on declared skills
    and the words in the task title. Falls back to any idle agent.

    Args:
        task_id: The task to assign.
        by_role: Your role (usually PM).
    """
    def op(state):
        task = next((t for t in state["tasks"] if t["id"] == task_id), None)
        if not task:
            return {"error": "notfound"}
        title_words = set(task["title"].lower().replace(",", " ").split())
        candidates = []
        for role, a in state.get("agents", {}).items():
            if a.get("status") not in ("idle", "online"):
                continue
            skills = set(state.get("skills", {}).get(role, []))
            score = len(skills & title_words)
            # idle agents preferred over merely-online
            score += 1 if a.get("status") == "idle" else 0
            candidates.append((score, role))
        if not candidates:
            return {"error": "noagents"}
        candidates.sort(reverse=True)
        best_score, best = candidates[0]
        task["assignee"] = best
        task["status"] = "todo"
        if best in state["agents"]:
            state["agents"][best]["status"] = "busy"
        state["messages"].append({
            "from": by_role, "mention": best,
            "text": f"@{best} auto-assigned task #{task_id}: {task['title']} (skill match: {best_score}).",
            "time": _hms()})
        _log(state, by_role, f"auto-assigned #{task_id} to {best}")
        return {"role": best, "score": best_score}
    res = _mutate(op)
    if res.get("error") == "notfound":
        return f"No task #{task_id}."
    if res.get("error") == "noagents":
        return "No available agents to assign. Everyone is busy or offline."
    return f"Task #{task_id} auto-assigned to @{res['role']} (match score {res['score']})."


@mcp.tool()
def save_template(name: str, steps: str, by_role: str = "") -> str:
    """Save a reusable workflow template (a sequence of task steps you can spin up
    again later, e.g. a 'code-review' or 'SSCL-return' workflow).

    Args:
        name: Template name, e.g. "sscl-return".
        steps: Steps as 'title|skill|priority' separated by ';'. depends_on is the
               previous step automatically. Example:
               "Parse data|backend|high; Calculate|backend|high; QA verify|qa|medium; Report|frontend|medium".
        by_role: Your role.
    """
    parsed = []
    for raw in steps.split(";"):
        raw = raw.strip()
        if not raw:
            continue
        parts = [p.strip() for p in raw.split("|")]
        title = parts[0]
        skill = parts[1].lower() if len(parts) > 1 and parts[1] else ""
        pri = parts[2].lower() if len(parts) > 2 and parts[2] in ("high", "medium", "low") else "medium"
        parsed.append({"title": title, "skill": skill, "priority": pri})
    if not parsed:
        return "No valid steps parsed. Use 'title|skill|priority; ...'."

    def op(state):
        state["templates"][name] = parsed
        _log(state, by_role or "system", f"saved template '{name}' ({len(parsed)} steps)")
    _mutate(op)
    return f"Template '{name}' saved with {len(parsed)} steps. Run it with run_template."


@mcp.tool()
def list_templates() -> str:
    """List saved workflow templates."""
    state = _load()
    tpls = state.get("templates", {})
    if not tpls:
        return "No templates saved. Create one with save_template."
    out = ["Saved templates:"]
    for name, steps in tpls.items():
        out.append(f"  '{name}' ({len(steps)} steps): " + " -> ".join(s["title"] for s in steps))
    return "\n".join(out)


@mcp.tool()
def run_template(name: str, auto_assign_steps: bool = True, by_role: str = "PM") -> str:
    """Instantiate a workflow template: creates all its tasks as a dependency chain
    (each step depends on the previous), with priorities, optionally auto-assigning
    each to a skill-matched agent.

    Args:
        name: Template name to run.
        auto_assign_steps: If True, auto-assign each created task by skill.
        by_role: Your role.
    """
    def op(state):
        tpl = state.get("templates", {}).get(name)
        if not tpl:
            return {"error": "notfound"}
        created = []
        prev_id = 0
        for step in tpl:
            tid = (max([t["id"] for t in state["tasks"]], default=0)) + 1
            deps = [prev_id] if prev_id else []
            task = {"id": tid, "title": step["title"], "assignee": "", "status": "todo",
                    "created_by": by_role, "updated": _hms(),
                    "priority": step.get("priority", "medium"),
                    "depends_on": deps, "parent_id": 0, "_skill": step.get("skill", "")}
            # try auto-assign by skill among idle/online agents
            if auto_assign_steps and step.get("skill"):
                want = step["skill"]
                match = None
                for role, a in state.get("agents", {}).items():
                    if want in state.get("skills", {}).get(role, []):
                        match = role
                        break
                if match:
                    task["assignee"] = match
            state["tasks"].append(task)
            created.append((tid, step["title"], task["assignee"]))
            prev_id = tid
        _log(state, by_role, f"ran template '{name}' -> {len(created)} tasks")
        state["messages"].append({
            "from": by_role, "mention": "all",
            "text": f"Workflow '{name}' started: {len(created)} tasks created as a dependency chain.",
            "time": _hms()})
        return {"created": created}
    res = _mutate(op)
    if res.get("error") == "notfound":
        return f"No template named '{name}'. See list_templates."
    lines = [f"Workflow '{name}' instantiated ({len(res['created'])} tasks, chained):"]
    for tid, title, who in res["created"]:
        lines.append(f"  #{tid} {title} -> {who or 'unassigned'}")
    return "\n".join(lines)


@mcp.tool()
def cast_vote(role: str, choice: str, reason: str = "") -> str:
    """Cast a vote in the current debate. Complements the judge: votes are tallied
    so the judge can see where the team leans (but the judge still decides on merit).

    Args:
        role: Your role.
        choice: The option you vote for.
        reason: Optional short reason.
    """
    def op(state):
        d = state.get("debate")
        if not d:
            return {"error": "nodebate"}
        d.setdefault("votes", {})
        d["votes"][role] = {"choice": choice, "reason": reason, "time": _hms()}
        state["messages"].append({
            "from": role, "mention": "all",
            "text": f"@{role} votes: {choice}" + (f" — {reason}" if reason else ""),
            "time": _hms()})
        _log(state, role, f"voted: {choice}")
        return {"ok": True}
    res = _mutate(op)
    if res.get("error") == "nodebate":
        return "No active debate to vote in. Start one with start_debate."
    return f"@{role} vote recorded: {choice}."


@mcp.tool()
def vote_tally() -> str:
    """Show the current vote tally in the active debate (the judge uses this as
    input, not as the final word)."""
    state = _load()
    d = state.get("debate")
    if not d:
        return "No active debate."
    votes = d.get("votes", {})
    if not votes:
        return "No votes cast yet."
    counts = {}
    for v in votes.values():
        counts[v["choice"]] = counts.get(v["choice"], 0) + 1
    out = ["=== VOTE TALLY ==="]
    for choice, n in sorted(counts.items(), key=lambda x: -x[1]):
        voters = [r for r, v in votes.items() if v["choice"] == choice]
        out.append(f"  {choice}: {n} vote(s) — {', '.join(voters)}")
    out.append("(Judge decides on merit, not just the count.)")
    return "\n".join(out)


# ============================================================================
# BATCH A — RELIABILITY (read receipts, retry, health, backup/restore, recovery)
# ============================================================================

@mcp.tool()
def acknowledge(role: str, up_to_index: int = -1) -> str:
    """Mark messages as READ by you (a read receipt). Lets the team see who has
    actually seen what. Call after reading the channel.

    Args:
        role: Your role.
        up_to_index: Highest message index you've read. -1 = mark everything read.
    """
    def op(state):
        total = len(state["messages"])
        idx = total if up_to_index < 0 else min(up_to_index, total)
        state["read_state"][role] = idx
        _touch_agent(state, role)
        return idx, total
    idx, total = _mutate(op)
    return f"@{role} acknowledged up to message {idx}/{total}."


@mcp.tool()
def read_receipts(message_index: int = -1) -> str:
    """See who has read up to a given message (or the latest). Useful to check if
    an agent has seen an instruction before assuming they're ignoring it.

    Args:
        message_index: The message index to check. -1 = latest message.
    """
    state = _load()
    total = len(state["messages"])
    if total == 0:
        return "No messages yet."
    target = total - 1 if message_index < 0 else message_index
    rs = state.get("read_state", {})
    seen, not_seen = [], []
    for role in state.get("agents", {}):
        if rs.get(role, -1) > target:
            seen.append(role)
        else:
            not_seen.append(role)
    out = [f"Read status for message #{target} (of {total}):"]
    out.append(f"  Seen by: {', '.join(seen) or 'no one'}")
    out.append(f"  NOT seen by: {', '.join(not_seen) or 'everyone has seen it'}")
    return "\n".join(out)


@mcp.tool()
def ping(from_role: str, target_role: str = "", timeout_seconds: int = 15) -> str:
    """Health check: ping a teammate (or everyone) and report who is alive based on
    recent activity. Does not require the other agent to do anything if their
    last_seen is fresh; otherwise it posts a ping they can answer.

    Args:
        from_role: Your role.
        target_role: Role to ping. Empty = report health of all agents.
        timeout_seconds: How long to wait for a stale agent to respond.
    """
    state = _load()
    now = _ts()
    agents = state.get("agents", {})
    if not target_role:
        out = ["=== HEALTH CHECK ==="]
        for role, a in agents.items():
            last = a.get("last_seen")
            if last and (now - last) <= HEARTBEAT_STALE:
                out.append(f"  {role}: ALIVE (seen {int(now - last)}s ago)")
            else:
                ago = f"{int(now - last)}s ago" if last else "never"
                out.append(f"  {role}: NO RECENT ACTIVITY (last {ago})")
        return "\n".join(out)
    # Targeted ping: if fresh, report alive immediately.
    a = agents.get(target_role)
    if a and a.get("last_seen") and (now - a["last_seen"]) <= HEARTBEAT_STALE:
        return f"@{target_role} is ALIVE (active {int(now - a['last_seen'])}s ago)."
    # Otherwise post a ping and wait for them to touch activity.
    _mutate(lambda s: s["messages"].append(
        {"from": from_role, "mention": target_role,
         "text": f"@{target_role} PING from @{from_role} — reply or run any tool to confirm you're alive.",
         "time": _hms()}))
    deadline = now + max(1, timeout_seconds)
    while _ts() < deadline:
        st = _read_state_unlocked()
        a = st.get("agents", {}).get(target_role, {})
        if a.get("last_seen") and (_ts() - a["last_seen"]) <= timeout_seconds:
            return f"@{target_role} responded — ALIVE."
        time.sleep(0.5)
    return f"@{target_role} did NOT respond within {timeout_seconds}s — may be down. Consider recover_tasks."


@mcp.tool()
def backup_now(by_role: str = "") -> str:
    """Force an immediate backup snapshot of the whole team state.

    Args:
        by_role: Your role.
    """
    state = _load()
    text = json.dumps(state, indent=2, ensure_ascii=False)
    _make_backup(text)
    backups = _list_backups()
    return f"Backup saved. {len(backups)} backups kept in {BACKUP_DIR} (newest first auto-pruned beyond {BACKUP_KEEP})."


@mcp.tool()
def list_backups() -> str:
    """List available state backups you can restore from."""
    backups = _list_backups()
    if not backups:
        return f"No backups yet in {BACKUP_DIR}. Backups are made automatically every {BACKUP_EVERY} writes, or via backup_now."
    out = [f"Backups in {BACKUP_DIR} (oldest first):"]
    for i, b in enumerate(backups):
        out.append(f"  [{i}] {b.name}")
    out.append("Restore with restore_backup(index=...) -- newest is the highest index.")
    return "\n".join(out)


@mcp.tool()
def restore_backup(index: int = -1, by_role: str = "") -> str:
    """Restore team state from a backup (e.g. after corruption or a bad reset).
    A safety backup of the current state is taken first.

    Args:
        index: Which backup (from list_backups). -1 = most recent.
        by_role: Your role.
    """
    backups = _list_backups()
    if not backups:
        return "No backups available to restore."
    pick = backups[index] if -len(backups) <= index < len(backups) else backups[-1]
    try:
        data = json.loads(pick.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Could not read backup {pick.name}: {e}"
    with _Lock(LOCK_FILE):
        # safety snapshot of current before overwriting
        try:
            _make_backup(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        _write_state_unlocked(data)
    return f"Restored team state from {pick.name}. (A safety backup of the previous state was saved first.)"


@mcp.tool()
def recover_tasks(stale_seconds: int = 0, by_role: str = "PM") -> str:
    """Crash recovery: find in_progress tasks whose assignee is offline/stale and
    release them back to 'todo' so another agent can pick them up. Call this when
    an agent crashes or a ping fails.

    Args:
        stale_seconds: An agent idle longer than this is considered down
                       (0 = use AGENT_STALE_SECONDS default).
        by_role: Your role.
    """
    limit = stale_seconds or AGENT_STALE_SECONDS

    def op(state):
        now = _ts()
        recovered = []
        for t in state["tasks"]:
            if t.get("status") != "in_progress":
                continue
            who = t.get("assignee")
            a = state.get("agents", {}).get(who, {})
            last = a.get("last_seen")
            down = (a.get("status") == "offline") or (last and (now - last) > limit) or (not a)
            if who and down:
                t["status"] = "todo"
                t["updated"] = _hms()
                recovered.append((t["id"], who))
                state["messages"].append({
                    "from": by_role, "mention": "all",
                    "text": f"RECOVERY: task #{t['id']} '{t['title']}' released from @{who} (down) back to TODO.",
                    "time": _hms()})
        _log(state, by_role, f"recovered {len(recovered)} task(s)")
        return recovered
    recovered = _mutate(op)
    if not recovered:
        return "No stale in-progress tasks found. Nothing to recover."
    lines = [f"Recovered {len(recovered)} task(s) back to TODO:"]
    for tid, who in recovered:
        lines.append(f"  #{tid} (was @{who})")
    lines.append("Re-assign them with assign_work.")
    return "\n".join(lines)


@mcp.tool()
def safe_call(role: str, action_note: str, attempt: int = 1, max_attempts: int = 3) -> str:
    """Auto-retry helper. Record that you are attempting an action that might fail
    (e.g. a flaky build or network step). If it keeps failing, call again with the
    next attempt number; after max_attempts the team is notified to intervene.

    Args:
        role: Your role.
        action_note: What you're trying to do.
        attempt: Current attempt number (start at 1).
        max_attempts: Give up and escalate after this many.
    """
    def op(state):
        _touch_agent(state, role)
        if attempt >= max_attempts:
            state["messages"].append({
                "from": role, "mention": "PM",
                "text": f"@{role} FAILED '{action_note}' after {attempt} attempts — escalating to @PM.",
                "time": _hms()})
            _log(state, role, f"escalated after {attempt} attempts: {action_note}")
            return "escalate"
        state["messages"].append({
            "from": role, "mention": "all",
            "text": f"@{role} retrying '{action_note}' (attempt {attempt}/{max_attempts}).",
            "time": _hms()})
        return "retry"
    outcome = _mutate(op)
    if outcome == "escalate":
        return (f"Reached max_attempts ({max_attempts}) for '{action_note}'. Escalated to PM. "
                f"Stop retrying and wait for guidance.")
    return (f"Recorded attempt {attempt}/{max_attempts} for '{action_note}'. "
            f"If it fails again, call safe_call with attempt={attempt + 1}.")


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


# ============================================================================
# BATCH F — MCP HUB / GATEWAY
# A single MCP server that acts as a router/proxy. Other MCP servers and REST
# APIs register as "targets"; any agent connects once to the hub and reaches all
# of them. The hub holds the credentials (agents never see keys), enforces rate
# limits, keeps a central audit trail, routes requests, and can AUTO-GENERATE a
# full MCP adapter from an OpenAPI/Swagger spec.
# ============================================================================

import asyncio as _asyncio
import base64 as _b64
import collections as _collections
import keyword as _keyword
import urllib.parse as _urlparse

# The hub acts as an MCP *client* to downstream MCP servers. Import lazily so the
# server still runs (REST + generator + registry) even if the client extras are
# unavailable in this Python environment.
try:
    from mcp import ClientSession as _ClientSession, StdioServerParameters as _StdioParams
    from mcp.client.stdio import stdio_client as _stdio_client
    _HAS_MCP_CLIENT = True
except Exception:
    _HAS_MCP_CLIENT = False

# --- Gateway storage (kept separate from team state so it survives reset_team
#     and so secrets live in their own chmod-600 vault, never in team state) ---
GATEWAY_FILE = Path(os.environ.get("GATEWAY_FILE", str(STATE_FILE.parent / "gateway.json")))
GATEWAY_LOCK = Path(str(GATEWAY_FILE) + ".lock")
GATEWAY_VAULT = Path(os.environ.get("GATEWAY_VAULT_FILE", str(STATE_FILE.parent / "gateway_vault.json")))
GATEWAY_VAULT_LOCK = Path(str(GATEWAY_VAULT) + ".lock")
ADAPTER_DIR = Path(os.environ.get("ADAPTER_DIR", str(STATE_FILE.parent / "adapters")))
GATEWAY_RATE_DEFAULT = int(os.environ.get("GATEWAY_RATE_PER_MIN", "60"))
GATEWAY_AUDIT_LIMIT = int(os.environ.get("GATEWAY_AUDIT_LIMIT", "2000"))
GATEWAY_CALL_TIMEOUT = int(os.environ.get("GATEWAY_CALL_TIMEOUT", "30"))
GATEWAY_MAX_OPS = int(os.environ.get("GATEWAY_MAX_OPS", "150"))

# In-memory sliding-window rate counters (the hub is a single long-lived process,
# so this avoids write-amplifying the audit file on every call).
_GW_RATE = _collections.defaultdict(_collections.deque)
_GW_RATE_LOCK = threading.Lock()


def _gw_default() -> dict:
    return {
        "targets": {},   # name -> target definition (rest | mcp)
        "routes": [],    # [{pattern, target, priority}]
        "limits": {"default": {"per_minute": GATEWAY_RATE_DEFAULT}, "targets": {}},
        "audit": [],     # [{time, agent, target, op, status, ms, ok}]
        "stats": {},     # target -> {calls, errors, last}
    }


def _gw_load() -> dict:
    gw = _gw_default()
    if GATEWAY_FILE.exists():
        try:
            loaded = json.loads(GATEWAY_FILE.read_text(encoding="utf-8"))
            gw.update(loaded)
            for k, v in _gw_default().items():
                gw.setdefault(k, v)
        except (json.JSONDecodeError, OSError):
            pass
    return gw


def _gw_mutate(fn):
    """Locked read-modify-write for the gateway registry/audit."""
    GATEWAY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _Lock(GATEWAY_LOCK):
        gw = _gw_load()
        result = fn(gw)
        _atomic_write(GATEWAY_FILE, json.dumps(gw, indent=2, ensure_ascii=False))
        return result


def _vault_load() -> dict:
    if GATEWAY_VAULT.exists():
        try:
            return json.loads(GATEWAY_VAULT.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _vault_mutate(fn):
    GATEWAY_VAULT.parent.mkdir(parents=True, exist_ok=True)
    with _Lock(GATEWAY_VAULT_LOCK):
        vault = _vault_load()
        result = fn(vault)
        _atomic_write(GATEWAY_VAULT, json.dumps(vault, indent=2, ensure_ascii=False))
        try:
            os.chmod(GATEWAY_VAULT, 0o600)  # owner-only; best effort
        except OSError:
            pass
        return result


def _gw_mask(v) -> str:
    v = str(v or "")
    if not v:
        return ""
    return "****" if len(v) <= 4 else "****" + v[-4:]


def _gw_mask_val(v) -> str:
    v = str(v)
    return v if v.startswith("vault:") else _gw_mask(v)  # vault: references aren't secret


def _gw_audit(gw: dict, agent: str, target: str, op: str, status, ms: float = 0, ok: bool = True) -> None:
    gw.setdefault("audit", []).append({
        "time": _now(), "agent": agent or "anon", "target": target,
        "op": op, "status": str(status)[:120], "ms": int(ms), "ok": bool(ok),
    })
    if len(gw["audit"]) > GATEWAY_AUDIT_LIMIT:
        gw["audit"] = gw["audit"][-GATEWAY_AUDIT_LIMIT:]
    st = gw.setdefault("stats", {}).setdefault(target, {"calls": 0, "errors": 0, "last": ""})
    st["calls"] += 1
    if not ok:
        st["errors"] += 1
    st["last"] = _now()


def _effective_limit(gw: dict, target: str) -> int:
    lims = gw.get("limits", {})
    tl = lims.get("targets", {}).get(target)
    if isinstance(tl, dict) and "per_minute" in tl:
        return int(tl["per_minute"])
    d = lims.get("default", {})
    if isinstance(d, dict) and "per_minute" in d:
        return int(d["per_minute"])
    return GATEWAY_RATE_DEFAULT


def _gw_rate(agent: str, target: str, limit_per_min: int):
    """Sliding-window check. Returns (allowed, retry_after_seconds)."""
    if not limit_per_min or limit_per_min <= 0:
        return True, 0
    key = f"{agent or 'anon'}|{target}"
    now = _ts()
    with _GW_RATE_LOCK:
        dq = _GW_RATE[key]
        while dq and (now - dq[0]) > 60:
            dq.popleft()
        if len(dq) >= limit_per_min:
            return False, max(1, int(60 - (now - dq[0])))
        dq.append(now)
        return True, 0


# --- small arg parsers (accept JSON or friendly shorthand) ---
def _gw_list(s):
    if isinstance(s, list):
        return [str(x) for x in s]
    s = (s or "").strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            v = json.loads(s)
            if isinstance(v, list):
                return [str(x) for x in v]
        except Exception:
            pass
    return [x.strip() for x in s.split(",") if x.strip()] if "," in s else s.split()


def _gw_obj(s):
    if isinstance(s, dict):
        return s
    s = (s or "").strip()
    if not s:
        return {}
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _gw_qs(s):
    s = (s or "").strip()
    if not s:
        return {}
    if s.startswith("{"):
        return {str(k): str(v) for k, v in _gw_obj(s).items()}
    return dict(_urlparse.parse_qsl(s, keep_blank_values=True))


def _gw_resolve_env(env_map) -> dict:
    """Resolve a target's env map, expanding "vault:KEY" values from the secret vault."""
    vault = _vault_load()
    out = {}
    for k, v in (env_map or {}).items():
        if isinstance(v, str) and v.startswith("vault:"):
            out[str(k)] = vault.get(v[6:], "")
        else:
            out[str(k)] = str(v)
    return out


# ============================================================================
# Downstream MCP client plumbing (sync wrappers around the async stdio client)
# ============================================================================

async def _gw_mcp_do(command, args, env, action):
    # No configured env -> inherit the SDK's safe default environment (keeps PATH etc).
    # Configured env -> layer it ON TOP of that default so we add a token without
    # wiping the variables the downstream process needs to start.
    full_env = None
    if env:
        try:
            from mcp.client.stdio import get_default_environment as _gde
            full_env = {**_gde(), **env}
        except Exception:
            full_env = {**os.environ, **env}
    params = _StdioParams(command=command, args=list(args or []), env=full_env)
    async with _stdio_client(params) as (read, write):
        async with _ClientSession(read, write) as session:
            await session.initialize()
            return await action(session)


def _gw_run(coro):
    """Run an async coroutine to completion from a synchronous MCP tool."""
    try:
        _asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return _asyncio.run(coro)
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: _asyncio.run(coro)).result()


def _gw_ser_tools(res):
    out = []
    for t in getattr(res, "tools", []) or []:
        out.append({"name": getattr(t, "name", "?"),
                    "description": (getattr(t, "description", "") or "")[:200]})
    return out


def _gw_ser_content(res):
    parts = []
    for c in getattr(res, "content", []) or []:
        txt = getattr(c, "text", None)
        parts.append(txt if txt is not None else str(c))
    return "\n".join(parts) if parts else "(no content)"


# ============================================================================
# Registration & discovery
# ============================================================================

@mcp.tool()
def gateway_register_rest(name: str, base_url: str, description: str = "",
                          auth_type: str = "none", auth_name: str = "",
                          credential_key: str = "", tags: str = "",
                          default_headers: str = "", by_role: str = "") -> str:
    """Register a REST API as a hub target. Agents then call it via gateway_call_rest
    WITHOUT ever seeing the credential -- the hub injects it at call time.

    Args:
        name: Unique target name, e.g. "stripe".
        base_url: Base URL, e.g. "https://api.stripe.com/v1".
        description: What this API does.
        auth_type: none | header | bearer | query | basic.
        auth_name: Header/query name for auth_type header|query (e.g. "X-API-Key").
        credential_key: Vault key holding the secret (set via gateway_set_credential).
        tags: Comma-separated routing tags, e.g. "payments,billing".
        default_headers: JSON object of headers always sent.
        by_role: Your role.
    """
    if not name or not base_url:
        return "name and base_url are required."

    def op(gw):
        existed = name in gw["targets"]
        gw["targets"][name] = {
            "kind": "rest", "description": description, "tags": _gw_list(tags),
            "base_url": base_url,
            "auth": {"type": auth_type, "name": auth_name, "credential": credential_key},
            "default_headers": _gw_obj(default_headers), "enabled": True,
            "registered_by": by_role or "gateway", "registered_at": _now(),
        }
        _gw_audit(gw, by_role, name, "register_rest", "ok")
        return existed

    existed = _gw_mutate(op)
    note = ""
    if auth_type != "none" and not credential_key:
        note = " (set credential_key + gateway_set_credential so the hub can authenticate)"
    return f"{'Updated' if existed else 'Registered'} REST target '{name}' -> {base_url} [auth: {auth_type}]{note}."


@mcp.tool()
def gateway_register_mcp(name: str, command: str, args: str = "", description: str = "",
                         env: str = "", tags: str = "", by_role: str = "") -> str:
    """Register a downstream MCP server as a hub target. Agents reach all of its tools
    through gateway_call_tool -- one hub connection fans out to many servers.

    Args:
        name: Unique target name, e.g. "github".
        command: Executable, e.g. "python" or "npx".
        args: Args as JSON list or space/comma-separated, e.g. "-y @some/mcp-server".
        description: What this server provides.
        env: JSON object of env vars. Use a "vault:KEY" value to inject a stored secret
             without writing it into config, e.g. {"TOKEN":"vault:gh_token"}.
        tags: Comma-separated routing tags.
        by_role: Your role.
    """
    if not name or not command:
        return "name and command are required."

    def op(gw):
        existed = name in gw["targets"]
        gw["targets"][name] = {
            "kind": "mcp", "description": description, "tags": _gw_list(tags),
            "command": command, "args": _gw_list(args), "env": _gw_obj(env),
            "capabilities": [], "enabled": True,
            "registered_by": by_role or "gateway", "registered_at": _now(),
        }
        _gw_audit(gw, by_role, name, "register_mcp", "ok")
        return existed

    existed = _gw_mutate(op)
    tip = " Run gateway_discover to fetch its tool list." if _HAS_MCP_CLIENT else ""
    return f"{'Updated' if existed else 'Registered'} MCP target '{name}' (command: {command} {' '.join(_gw_list(args))}).{tip}"


@mcp.tool()
def gateway_unregister(name: str, by_role: str = "") -> str:
    """Remove a registered target (and any routes pointing at it) from the hub."""
    def op(gw):
        if name not in gw["targets"]:
            return False
        del gw["targets"][name]
        gw["routes"] = [r for r in gw.get("routes", []) if r.get("target") != name]
        _gw_audit(gw, by_role, name, "unregister", "ok")
        return True
    return f"Removed target '{name}'." if _gw_mutate(op) else f"No target '{name}'."


@mcp.tool()
def gateway_toggle(name: str, enabled: bool = True, by_role: str = "") -> str:
    """Enable or disable a target without deleting its config.

    Args:
        name: Target name.
        enabled: True to enable, False to disable.
        by_role: Your role.
    """
    def op(gw):
        if name not in gw["targets"]:
            return None
        gw["targets"][name]["enabled"] = bool(enabled)
        _gw_audit(gw, by_role, name, "enable" if enabled else "disable", "ok")
        return True
    r = _gw_mutate(op)
    if r is None:
        return f"No target '{name}'."
    return f"Target '{name}' is now {'enabled' if enabled else 'disabled'}."


@mcp.tool()
def gateway_list_targets(tag: str = "", kind: str = "") -> str:
    """List registered targets (credentials are never shown).

    Args:
        tag: Filter by routing tag.
        kind: Filter by kind: rest | mcp.
    """
    gw = _gw_load()
    targets = gw.get("targets", {})
    if not targets:
        return ("No targets registered. Use gateway_register_rest / gateway_register_mcp, "
                "or gateway_generate_adapter to build one from an OpenAPI spec.")
    out = [f"=== HUB TARGETS ({len(targets)}) ==="]
    for n, t in sorted(targets.items()):
        if tag and tag not in t.get("tags", []):
            continue
        if kind and t.get("kind") != kind:
            continue
        caps = len(t.get("capabilities", [])) or len(t.get("operations", []))
        flag = "" if t.get("enabled", True) else " [disabled]"
        where = t.get("base_url") or (t.get("command", "") + " " + " ".join(t.get("args", []))).strip()
        tg = (" {" + ",".join(t.get("tags", [])) + "}") if t.get("tags") else ""
        out.append(f"  - {n} [{t.get('kind')}]{flag} -> {where}  ({caps} ops){tg}")
        if t.get("description"):
            out.append(f"      {t['description']}")
    return "\n".join(out)


@mcp.tool()
def gateway_describe(name: str) -> str:
    """Show full config + cached capabilities for one target (credential masked)."""
    gw = _gw_load()
    t = gw.get("targets", {}).get(name)
    if not t:
        return f"No target '{name}'."
    out = [f"=== {name} [{t.get('kind')}] ===",
           f"  description: {t.get('description', '')}",
           f"  tags: {', '.join(t.get('tags', [])) or '-'}",
           f"  enabled: {t.get('enabled', True)}",
           f"  registered: {t.get('registered_at', '?')} by {t.get('registered_by', '?')}"]
    if t.get("kind") == "rest":
        a = t.get("auth", {})
        out.append(f"  base_url: {t.get('base_url', '')}")
        out.append(f"  auth: type={a.get('type', 'none')} name={a.get('name', '') or '-'} "
                   f"credential_key={a.get('credential', '') or '-'}")
        if t.get("default_headers"):
            out.append(f"  default_headers: {json.dumps(t['default_headers'])}")
        ops = t.get("operations", [])
        if ops:
            out.append(f"  operations ({len(ops)}):")
            for o in ops[:40]:
                out.append(f"    - {o['op_id']}: {o['method']} {o['path']}")
    else:
        out.append(f"  command: {t.get('command', '')} {' '.join(t.get('args', []))}")
        if t.get("env"):
            out.append("  env: " + ", ".join(f"{k}={_gw_mask_val(v)}" for k, v in t["env"].items()))
        caps = t.get("capabilities", [])
        synced = f" (synced {t.get('capabilities_synced')})" if caps else ""
        out.append(f"  capabilities ({len(caps)}){synced}:")
        for c in caps[:40]:
            out.append(f"    - {c['name']}: {c.get('description', '')[:80]}")
    return "\n".join(out)


@mcp.tool()
def gateway_discover(name: str = "", by_role: str = "") -> str:
    """Connect to registered MCP server target(s), list their tools, and cache the
    capabilities so agents can browse them without re-spawning. REST targets are
    skipped (their operations come from registration / generated specs).

    Args:
        name: A single MCP target to discover. Empty = all MCP targets.
        by_role: Your role (for the audit log).
    """
    if not _HAS_MCP_CLIENT:
        return "MCP client SDK not available in this Python env (pip install mcp). REST targets still work."
    gw = _gw_load()
    targets = gw.get("targets", {})
    if name and (name not in targets or targets[name].get("kind") != "mcp"):
        return f"'{name}' is not a registered MCP target."
    todo = [name] if name else [n for n, t in targets.items() if t.get("kind") == "mcp"]
    if not todo:
        return "No MCP targets registered. Use gateway_register_mcp first."
    results = []
    for n in todo:
        t = targets.get(n)
        if not t or t.get("kind") != "mcp":
            continue
        env = _gw_resolve_env(t.get("env", {}))
        t0 = time.time()
        try:
            res = _gw_run(_gw_mcp_do(t["command"], t.get("args", []), env, lambda s: s.list_tools()))
            caps = _gw_ser_tools(res)
            ms = (time.time() - t0) * 1000

            def _upd(g, n=n, caps=caps, ms=ms):
                if n in g["targets"]:
                    g["targets"][n]["capabilities"] = caps
                    g["targets"][n]["capabilities_synced"] = _now()
                _gw_audit(g, by_role, n, "discover", f"{len(caps)} tools", ms=ms)

            _gw_mutate(_upd)
            preview = ", ".join(c["name"] for c in caps[:12]) + ("..." if len(caps) > 12 else "")
            results.append(f"{n}: {len(caps)} tools -- {preview}")
        except Exception as e:
            _gw_mutate(lambda g, n=n: _gw_audit(g, by_role, n, "discover", "error", ok=False))
            results.append(f"{n}: discovery failed -- {e}")
    return "\n".join(results)


@mcp.tool()
def gateway_capabilities(query: str = "") -> str:
    """Tool discovery -- ask the hub "what can I do?". Lists every capability across all
    enabled targets: MCP server tools (cached via gateway_discover) and REST
    operations (from generated specs). Optionally filter by a keyword.

    Args:
        query: Optional keyword to filter capabilities by name/description/path.
    """
    gw = _gw_load()
    q = (query or "").lower()
    blocks, total = [], 0
    for n, t in sorted(gw.get("targets", {}).items()):
        if not t.get("enabled", True):
            continue
        lines = []
        if t.get("kind") == "mcp":
            for c in t.get("capabilities", []):
                if q and q not in (c.get("name", "") + " " + c.get("description", "")).lower():
                    continue
                lines.append(f"    - {c['name']}  -  {c.get('description', '')[:70]}")
        else:
            for o in t.get("operations", []):
                if q and q not in (o.get("op_id", "") + " " + o.get("path", "") + " " + o.get("summary", "")).lower():
                    continue
                lines.append(f"    - {o['op_id']}  -  {o['method']} {o['path']}")
        if lines:
            hint = "gateway_call_tool" if t.get("kind") == "mcp" else "gateway_call_rest"
            blocks.append(f"  {n} [{t.get('kind')}] -> use {hint}:")
            blocks.extend(lines)
            total += len(lines)
    if not blocks:
        if any(t.get("kind") == "mcp" and not t.get("capabilities") for t in gw.get("targets", {}).values()):
            return "No capabilities cached yet -- run gateway_discover to fetch MCP tool lists."
        return "No capabilities found" + (f" matching '{query}'." if query else ". Register targets first.")
    head = f"=== HUB CAPABILITIES ({total}{' matching ' + repr(query) if query else ''}) ==="
    return head + "\n" + "\n".join(blocks)


# ============================================================================
# Unified auth -- credential vault (agents never see raw secrets)
# ============================================================================

@mcp.tool()
def gateway_set_credential(key: str, value: str, by_role: str = "") -> str:
    """Store a secret in the hub vault (a separate, chmod-600 file). Targets reference
    it by KEY; the raw value is never returned by any tool or shown on the dashboard.

    Args:
        key: Vault key, e.g. "stripe_key".
        value: The secret value.
        by_role: Your role.
    """
    if not key or not value:
        return "key and value are required."
    _vault_mutate(lambda v: v.__setitem__(key, value))
    _gw_mutate(lambda g: _gw_audit(g, by_role, "vault", "set_credential", key))
    return (f"Stored credential '{key}' ({_gw_mask(value)}). Reference it from a target's "
            f"credential_key, or as a 'vault:{key}' env value on an MCP target.")


@mcp.tool()
def gateway_list_credentials() -> str:
    """List vault credential KEYS only (never the values)."""
    keys = list(_vault_load().keys())
    if not keys:
        return "Vault is empty. Add one with gateway_set_credential."
    return "Vault keys (values hidden):\n" + "\n".join(f"  - {k}" for k in sorted(keys))


@mcp.tool()
def gateway_delete_credential(key: str, by_role: str = "") -> str:
    """Delete a credential from the vault."""
    existed = _vault_mutate(lambda v: v.pop(key, None) is not None)
    if existed:
        _gw_mutate(lambda g: _gw_audit(g, by_role, "vault", "delete_credential", key))
        return f"Deleted credential '{key}'."
    return f"No credential '{key}'."


# ============================================================================
# Routing rules
# ============================================================================

@mcp.tool()
def gateway_add_route(pattern: str, target: str, priority: int = 0, by_role: str = "") -> str:
    """Add a routing rule: if a request contains `pattern` (case-insensitive), send it
    to `target`. Higher priority wins. Used by gateway_route to pick a target.

    Args:
        pattern: Substring/keyword to match in a request.
        target: Target name to route to (must be registered).
        priority: Higher = checked first (default 0).
        by_role: Your role.
    """
    def op(gw):
        if target not in gw["targets"]:
            return False
        gw["routes"] = [r for r in gw.get("routes", [])
                        if not (r.get("pattern") == pattern and r.get("target") == target)]
        gw["routes"].append({"pattern": pattern, "target": target,
                             "priority": int(priority), "added_by": by_role or "gateway"})
        _gw_audit(gw, by_role, target, "add_route", pattern)
        return True
    return (f"Route added: '{pattern}' -> {target} (priority {priority})."
            if _gw_mutate(op) else f"Target '{target}' is not registered.")


@mcp.tool()
def gateway_remove_route(pattern: str, target: str = "", by_role: str = "") -> str:
    """Remove routing rule(s) matching pattern (and target if given)."""
    def op(gw):
        before = len(gw.get("routes", []))
        gw["routes"] = [r for r in gw.get("routes", [])
                        if not (r.get("pattern") == pattern and (not target or r.get("target") == target))]
        removed = before - len(gw["routes"])
        if removed:
            _gw_audit(gw, by_role, target or "*", "remove_route", pattern)
        return removed
    n = _gw_mutate(op)
    return f"Removed {n} route(s)." if n else "No matching route."


@mcp.tool()
def gateway_list_routes() -> str:
    """List routing rules, highest priority first."""
    gw = _gw_load()
    routes = sorted(gw.get("routes", []), key=lambda r: -r.get("priority", 0))
    if not routes:
        return "No routes. Add one with gateway_add_route, or rely on tag matching in gateway_route."
    out = ["=== ROUTES ==="]
    for r in routes:
        out.append(f"  [{r.get('priority', 0)}] '{r.get('pattern')}' -> {r.get('target')}")
    return "\n".join(out)


@mcp.tool()
def gateway_route(request: str) -> str:
    """Decide which target should handle a request. Checks explicit routes (by
    priority), then falls back to matching target tags found in the request text.

    Args:
        request: A natural-language or keyword request, e.g. "charge a credit card".
    """
    gw = _gw_load()
    req = (request or "").lower()
    for r in sorted(gw.get("routes", []), key=lambda r: -r.get("priority", 0)):
        pat = (r.get("pattern") or "").lower()
        if pat and pat in req and r.get("target") in gw.get("targets", {}):
            return f"-> {r['target']}  (route: '{r['pattern']}')"
    hits = []
    for n, t in gw.get("targets", {}).items():
        if not t.get("enabled", True):
            continue
        if any(tag.lower() in req for tag in t.get("tags", [])):
            hits.append(n)
    if hits:
        return "-> " + ", ".join(hits) + "  (tag match)"
    return "No route matched. Targets: " + (", ".join(gw.get("targets", {})) or "none")


# ============================================================================
# Rate limiting, audit & usage
# ============================================================================

@mcp.tool()
def gateway_set_limit(target: str = "", per_minute: int = 60, by_role: str = "") -> str:
    """Set a per-minute call rate limit (per agent). Empty target sets the global
    default. per_minute <= 0 means unlimited.

    Args:
        target: Target name, or empty for the global default.
        per_minute: Max calls per minute per agent.
        by_role: Your role.
    """
    def op(gw):
        gw.setdefault("limits", {"default": {}, "targets": {}})
        if target:
            gw["limits"].setdefault("targets", {})[target] = {"per_minute": int(per_minute)}
        else:
            gw["limits"]["default"] = {"per_minute": int(per_minute)}
        _gw_audit(gw, by_role, target or "default", "set_limit", str(per_minute))
    _gw_mutate(op)
    scope = f"target '{target}'" if target else "default (all targets)"
    return f"Rate limit for {scope} set to {per_minute}/min" + (" (unlimited)" if per_minute <= 0 else "") + "."


@mcp.tool()
def gateway_audit(last_n: int = 30, agent: str = "", target: str = "") -> str:
    """Show the central audit trail: who called what, when, status, latency.

    Args:
        last_n: How many recent entries to show.
        agent: Filter by agent/role.
        target: Filter by target.
    """
    gw = _gw_load()
    rows = gw.get("audit", [])
    if agent:
        rows = [r for r in rows if r.get("agent") == agent]
    if target:
        rows = [r for r in rows if r.get("target") == target]
    rows = rows[-last_n:]
    if not rows:
        return "No audit entries match."
    out = [f"=== GATEWAY AUDIT (last {len(rows)}) ==="]
    for r in rows:
        mark = "" if r.get("ok", True) else " x"
        ms = f" {r['ms']}ms" if r.get("ms") else ""
        out.append(f"  {r.get('time')}  {r.get('agent')} -> {r.get('target')} "
                   f"{r.get('op')} [{r.get('status')}]{ms}{mark}")
    return "\n".join(out)


@mcp.tool()
def gateway_usage() -> str:
    """Show per-target usage stats (calls, errors, last used) and configured limits."""
    gw = _gw_load()
    stats = gw.get("stats", {})
    out = ["=== GATEWAY USAGE ===", f"  default limit: {_effective_limit(gw, '__none__')}/min"]
    if not stats:
        out.append("  No calls yet.")
    for n, s in sorted(stats.items()):
        out.append(f"  - {n}: {s.get('calls', 0)} calls, {s.get('errors', 0)} errors, "
                   f"limit {_effective_limit(gw, n)}/min, last {s.get('last', '-')}")
    return "\n".join(out)


# ============================================================================
# Proxy / invocation
# ============================================================================

@mcp.tool()
def gateway_call_rest(target: str, path: str = "", method: str = "GET",
                      query: str = "", body: str = "", agent: str = "",
                      extra_headers: str = "") -> str:
    """Proxy a REST call through the hub to a registered REST target. The hub injects
    the stored credential, enforces the rate limit, and logs the call. Agents never
    handle the secret.

    Args:
        target: A registered REST target name.
        path: Path appended to the target's base_url, e.g. "/charges".
        method: GET | POST | PUT | PATCH | DELETE.
        query: Query string ("a=1&b=2") or JSON object.
        body: Request body (sent as JSON if non-empty).
        agent: Your role (for rate-limiting + audit).
        extra_headers: JSON object of extra headers.
    """
    gw = _gw_load()
    t = gw.get("targets", {}).get(target)
    if not t:
        return f"Unknown target '{target}'. See gateway_list_targets."
    if t.get("kind") != "rest":
        return f"'{target}' is an MCP target -- use gateway_call_tool instead."
    if not t.get("enabled", True):
        return f"Target '{target}' is disabled."
    lim = _effective_limit(gw, target)
    ok, retry = _gw_rate(agent, target, lim)
    if not ok:
        _gw_mutate(lambda g: _gw_audit(g, agent, target, f"{method.upper()} {path}", "rate-limited", ok=False))
        return f"Rate limit {lim}/min hit for {target}. Retry in ~{retry}s."
    url = t.get("base_url", "").rstrip("/")
    if path:
        url += "/" + path.lstrip("/")
    headers = dict(t.get("default_headers", {}))
    headers.update(_gw_obj(extra_headers))
    q = _gw_qs(query)
    auth = t.get("auth", {})
    cred = _vault_load().get(auth.get("credential", ""), "") if auth.get("credential") else ""
    atype, aname = auth.get("type", "none"), auth.get("name", "")
    if atype == "header" and aname:
        headers[aname] = cred
    elif atype == "bearer":
        headers["Authorization"] = "Bearer " + cred
    elif atype == "basic":
        headers["Authorization"] = "Basic " + _b64.b64encode(cred.encode("utf-8")).decode("ascii")
    elif atype == "query" and aname:
        q[aname] = cred
    if q:
        url += ("&" if "?" in url else "?") + _urlparse.urlencode(q)
    data = body.encode("utf-8") if body else None
    if data:
        headers.setdefault("Content-Type", "application/json")
    req = _urlreq.Request(url, data=data, headers=headers, method=method.upper())
    t0 = time.time()
    try:
        with _urlreq.urlopen(req, timeout=GATEWAY_CALL_TIMEOUT) as resp:
            code, text = resp.getcode(), resp.read().decode("utf-8", "replace")
    except _urlerr.HTTPError as e:
        code = e.code
        try:
            text = e.read().decode("utf-8", "replace")
        except Exception:
            text = ""
    except Exception as e:
        ms = (time.time() - t0) * 1000
        _gw_mutate(lambda g: _gw_audit(g, agent, target, f"{method.upper()} {path}", "error", ms=ms, ok=False))
        return f"Request to {target} failed: {e}"
    ms = (time.time() - t0) * 1000
    ok2 = 200 <= code < 400
    _gw_mutate(lambda g: _gw_audit(g, agent, target, f"{method.upper()} {path}", str(code), ms=ms, ok=ok2))
    snippet = text if len(text) <= 4000 else text[:4000] + f"\n... [{len(text)} bytes total, truncated]"
    return f"HTTP {code} - {int(ms)}ms - {target} {method.upper()} {path or '/'}\n{snippet}"


@mcp.tool()
def gateway_call_tool(target: str, tool: str, arguments: str = "", agent: str = "") -> str:
    """Proxy a tool call to a registered MCP server through the hub. The hub spawns the
    server with its stored credentials injected, forwards the call, logs it to the
    audit trail, and returns the result. Agents never see the credentials.

    Args:
        target: A registered MCP target name.
        tool: The tool to invoke on that server.
        arguments: JSON object of arguments, e.g. '{"city":"London"}'.
        agent: Your role (for rate-limiting + audit).
    """
    if not _HAS_MCP_CLIENT:
        return "MCP client SDK not available in this Python env (pip install mcp)."
    gw = _gw_load()
    t = gw.get("targets", {}).get(target)
    if not t:
        return f"Unknown target '{target}'. See gateway_list_targets."
    if t.get("kind") != "mcp":
        return f"'{target}' is a {t.get('kind')} target -- use gateway_call_rest instead."
    if not t.get("enabled", True):
        return f"Target '{target}' is disabled."
    lim = _effective_limit(gw, target)
    ok, retry = _gw_rate(agent, target, lim)
    if not ok:
        _gw_mutate(lambda g: _gw_audit(g, agent, target, f"call:{tool}", "rate-limited", ok=False))
        return f"Rate limit {lim}/min hit for {target}. Retry in ~{retry}s."
    env = _gw_resolve_env(t.get("env", {}))
    args_obj = _gw_obj(arguments)
    t0 = time.time()
    try:
        res = _gw_run(_gw_mcp_do(t["command"], t.get("args", []), env, lambda s: s.call_tool(tool, args_obj)))
        text = _gw_ser_content(res)
        ms = (time.time() - t0) * 1000
        _gw_mutate(lambda g: _gw_audit(g, agent, target, f"call:{tool}", "ok", ms=ms))
        return f"[{target}.{tool} {int(ms)}ms]\n{text}"
    except Exception as e:
        ms = (time.time() - t0) * 1000
        _gw_mutate(lambda g: _gw_audit(g, agent, target, f"call:{tool}", "error", ms=ms, ok=False))
        return f"Call to {target}.{tool} failed: {e}"


# ============================================================================
# KILLER FEATURE -- Auto-Adapter Generator (OpenAPI/Swagger -> MCP server)
# ============================================================================

def _gw_py_ident(s: str) -> str:
    s = _re.sub(r"[^0-9a-zA-Z_]", "_", str(s or "")).strip("_")
    if not s:
        s = "op"
    if s[0].isdigit():
        s = "n_" + s
    if _keyword.iskeyword(s):
        s = s + "_"
    return s


def _gw_synth_id(method: str, path: str) -> str:
    return _gw_py_ident(method + "_" + path)


def _gw_load_spec(spec: str):
    s = (spec or "").strip()
    if not s:
        raise ValueError("empty spec")
    if s[0] in "{[":
        return json.loads(s)
    if s.startswith(("http://", "https://")):
        with _urlreq.urlopen(s, timeout=20) as r:
            s = r.read().decode("utf-8", "replace")
    elif Path(s).exists():
        s = Path(s).read_text(encoding="utf-8")
    else:
        raise ValueError("spec must be inline JSON, an existing file path, or a URL")
    try:
        return json.loads(s)
    except Exception:
        try:
            import yaml  # optional
            return yaml.safe_load(s)
        except ImportError:
            raise ValueError("spec looks like YAML -- install PyYAML or provide JSON")


def _gw_spec_base_url(spec: dict) -> str:
    servers = spec.get("servers")
    if isinstance(servers, list) and servers and isinstance(servers[0], dict) and servers[0].get("url"):
        return servers[0]["url"]
    host = spec.get("host")  # Swagger v2
    if host:
        scheme = (spec.get("schemes") or ["https"])[0]
        return f"{scheme}://{host}{spec.get('basePath', '')}"
    return ""


def _gw_detect_auth(spec: dict):
    schemes = (spec.get("components", {}) or {}).get("securitySchemes") or spec.get("securityDefinitions") or {}
    for s in schemes.values():
        if not isinstance(s, dict):
            continue
        typ = (s.get("type") or "").lower()
        if typ == "apikey":
            loc = (s.get("in") or "header").lower()
            return ("query" if loc == "query" else "header"), s.get("name", "X-API-Key")
        if typ == "http" and (s.get("scheme", "").lower() == "bearer"):
            return "bearer", "Authorization"
        if typ == "oauth2":
            return "bearer", "Authorization"
    return "none", ""


def _gw_extract_ops(spec: dict):
    ops = []
    paths = spec.get("paths", {})
    if not isinstance(paths, dict):
        return ops
    for path, item in paths.items():
        if not isinstance(item, dict):
            continue
        shared = item.get("parameters") if isinstance(item.get("parameters"), list) else []
        for method in ("get", "post", "put", "patch", "delete"):
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            params = list(shared)
            if isinstance(op.get("parameters"), list):
                params += op["parameters"]
            params = [p for p in params if isinstance(p, dict) and p.get("name")]
            ops.append({
                "method": method, "path": path,
                "op_id": op.get("operationId") or _gw_synth_id(method, path),
                "summary": op.get("summary") or op.get("description") or "",
                "params": params,
                "has_body": bool(op.get("requestBody")) or method in ("post", "put", "patch"),
            })
    return ops


def _gw_gen_tool(op: dict, used: set) -> str:
    name = _gw_py_ident(op["op_id"])
    base, i = name, 2
    while name in used:
        name, i = f"{base}_{i}", i + 1
    used.add(name)
    seen = {"body"} if op["has_body"] else set()
    pmap = []  # (location, original_name, python_name)
    for p in op["params"]:
        loc = p.get("in")
        if loc not in ("path", "query", "header"):
            continue
        py = _gw_py_ident(p["name"])
        b, j = py, 2
        while py in seen:
            py, j = f"{b}_{j}", j + 1
        seen.add(py)
        pmap.append((loc, p["name"], py))
    sig = [f'{py}: str = ""' for _, _, py in pmap]
    if op["has_body"]:
        sig.append('body: str = ""')
    doc = " ".join((op["summary"] or "").split())[:280].replace('"""', "'''").rstrip("\\")
    L = ["@mcp.tool()", f"def {name}({', '.join(sig)}) -> str:", f'    """{doc}', "",
         f'    {op["method"].upper()} {op["path"]}', '    """', f'    _p = {op["path"]!r}']
    for loc, orig, py in pmap:
        if loc == "path":
            L.append(f'    _p = _p.replace("{{{orig}}}", _url.quote(str({py})))')
    L.append("    _q = {}")
    for loc, orig, py in pmap:
        if loc == "query":
            L.append(f'    if {py} != "": _q[{orig!r}] = {py}')
    L.append("    _h = {}")
    for loc, orig, py in pmap:
        if loc == "header":
            L.append(f'    if {py} != "": _h[{orig!r}] = {py}')
    body_expr = "body" if op["has_body"] else '""'
    L.append(f'    return _request({op["method"].upper()!r}, _p, _q, {body_expr}, _h)')
    L.append("")
    return "\n".join(L)


def _gw_gen_adapter_code(name: str, base_url: str, ops: list, auth_type: str, auth_name: str) -> str:
    prefix = _re.sub(r"[^A-Z0-9]", "_", name.upper())
    used = set()
    tools_src = "\n".join(_gw_gen_tool(o, used) for o in ops)
    tmpl = '''#!/usr/bin/env python3
"""
__NAME__ MCP adapter -- auto-generated by Claude Team MCP Gateway from an OpenAPI spec.
Exposes __COUNT__ operations as MCP tools. Editable; re-generating overwrites this file.

Run standalone:
    env __PREFIX___API_KEY=... python __FILE__
Register with Claude Code:
    claude mcp add __NAME__ -s user -- env __PREFIX___API_KEY=... python /abs/path/__FILE__
"""
import os
import urllib.request as _req
import urllib.error as _err
import urllib.parse as _url

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("__NAME__")

BASE_URL = os.environ.get("__PREFIX___BASE_URL", "__BASE_URL__").rstrip("/")
API_KEY = os.environ.get("__PREFIX___API_KEY", "")
AUTH_TYPE = "__AUTH_TYPE__"
AUTH_NAME = "__AUTH_NAME__"


def _request(method, path, query=None, body="", headers=None):
    url = BASE_URL + ("/" + path.lstrip("/") if path else "")
    h = dict(headers or {})
    q = dict(query or {})
    if API_KEY:
        if AUTH_TYPE == "header" and AUTH_NAME:
            h[AUTH_NAME] = API_KEY
        elif AUTH_TYPE == "bearer":
            h["Authorization"] = "Bearer " + API_KEY
        elif AUTH_TYPE == "query" and AUTH_NAME:
            q[AUTH_NAME] = API_KEY
    if q:
        url += ("&" if "?" in url else "?") + _url.urlencode(q)
    data = body.encode("utf-8") if body else None
    if data:
        h.setdefault("Content-Type", "application/json")
    req = _req.Request(url, data=data, headers=h, method=method)
    try:
        with _req.urlopen(req, timeout=30) as resp:
            return "HTTP %s\\n%s" % (resp.getcode(), resp.read().decode("utf-8", "replace"))
    except _err.HTTPError as e:
        return "HTTP %s\\n%s" % (e.code, e.read().decode("utf-8", "replace"))
    except Exception as e:
        return "Request failed: %s" % e


__TOOLS__

if __name__ == "__main__":
    mcp.run()
'''
    return (tmpl.replace("__NAME__", name).replace("__COUNT__", str(len(ops)))
                .replace("__PREFIX__", prefix).replace("__BASE_URL__", base_url or "")
                .replace("__AUTH_TYPE__", auth_type or "none").replace("__AUTH_NAME__", auth_name or "")
                .replace("__FILE__", name + ".py").replace("__TOOLS__", tools_src))


@mcp.tool()
def gateway_generate_adapter(spec: str, name: str = "", out_path: str = "",
                             base_url: str = "", credential_key: str = "",
                             register: bool = True, by_role: str = "") -> str:
    """KILLER FEATURE -- turn an OpenAPI/Swagger spec into a ready-to-run MCP server.
    Parses every path+method into an MCP tool and writes a standalone Python MCP
    server file. Optionally auto-registers it as a hub REST target so it is callable
    and discoverable immediately. Removes ~90% of the boilerplate of wrapping an API.

    Args:
        spec: Inline JSON, a file path, or a URL to an OpenAPI/Swagger document.
        name: Adapter/target name (default: derived from the spec title).
        out_path: Where to write the .py file (default: <ADAPTER_DIR>/<name>.py).
        base_url: Override the base URL (else taken from the spec's servers/host).
        credential_key: Vault key for the API key when auto-registering the REST target.
        register: If True, also register the generated API as a hub REST target.
        by_role: Your role.
    """
    try:
        doc = _gw_load_spec(spec)
    except Exception as e:
        return f"Could not load spec: {e}"
    if not isinstance(doc, dict) or "paths" not in doc:
        return "Spec has no 'paths' -- is this a valid OpenAPI/Swagger document?"
    info = doc.get("info", {}) if isinstance(doc.get("info"), dict) else {}
    adapter_name = _gw_py_ident(name or info.get("title") or "api").lower()
    burl = base_url or _gw_spec_base_url(doc)
    auth_type, auth_name = _gw_detect_auth(doc)
    ops = _gw_extract_ops(doc)
    if not ops:
        return "No operations found in spec."
    truncated = ""
    if len(ops) > GATEWAY_MAX_OPS:
        truncated = f" (capped at {GATEWAY_MAX_OPS} of {len(ops)} ops; raise GATEWAY_MAX_OPS)"
        ops = ops[:GATEWAY_MAX_OPS]
    code = _gw_gen_adapter_code(adapter_name, burl, ops, auth_type, auth_name)
    out = Path(out_path) if out_path else (ADAPTER_DIR / f"{adapter_name}.py")
    out.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(out, code)
    summary = [f"Generated MCP adapter '{adapter_name}' with {len(ops)} tools -> {out}{truncated}",
               f"Base URL: {burl or '(none -- pass base_url=)'}",
               f"Auth: {auth_type}" + (f" ({auth_name})" if auth_name else "")]
    if register and burl:
        ops_brief = [{"op_id": _gw_py_ident(o["op_id"]), "method": o["method"].upper(),
                      "path": o["path"], "summary": " ".join((o["summary"] or "").split())[:120]} for o in ops]

        def _reg(gw):
            gw["targets"][adapter_name] = {
                "kind": "rest", "description": info.get("title") or adapter_name,
                "tags": ["generated"], "base_url": burl,
                "auth": {"type": auth_type, "name": auth_name, "credential": credential_key},
                "default_headers": {}, "operations": ops_brief, "enabled": True,
                "registered_by": by_role or "gateway", "registered_at": _now(),
            }
            _gw_audit(gw, by_role, adapter_name, "generate_adapter", f"{len(ops)} ops")

        _gw_mutate(_reg)
        ck = credential_key or f"{adapter_name}_key"
        summary.append(f"Registered REST target '{adapter_name}'. Set its key with "
                       f"gateway_set_credential('{ck}', '<secret>'), then call via gateway_call_rest "
                       f"or browse with gateway_capabilities.")
    elif register and not burl:
        summary.append("Not registered (no base URL). Re-run with base_url=... to register a callable target.")
    prefix = _re.sub(r"[^A-Z0-9]", "_", adapter_name.upper())
    summary.append(f"Run standalone: env {prefix}_API_KEY=... python {out}")
    return "\n".join(summary)


# ============================================================================
# Gateway dashboard view (served by the existing dashboard server at /gateway)
# ============================================================================

def _gw_dashboard_payload() -> dict:
    gw = _gw_load()
    targets = {}
    for n, t in gw.get("targets", {}).items():
        targets[n] = {
            "kind": t.get("kind"), "description": t.get("description", ""),
            "tags": t.get("tags", []), "enabled": t.get("enabled", True),
            "base_url": t.get("base_url", ""),
            "command": (t.get("command", "") + " " + " ".join(t.get("args", []))).strip(),
            "capabilities": len(t.get("capabilities", [])),
            "operations": len(t.get("operations", [])),
        }
    return {
        "targets": targets,
        "routes": gw.get("routes", []),
        "audit": gw.get("audit", [])[-80:],
        "stats": gw.get("stats", {}),
        "default_limit": _effective_limit(gw, "__none__"),
        "credentials": list(_vault_load().keys()),
    }


def _gateway_html() -> str:
    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>MCP Hub / Gateway</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#e6edf3;--dim:#8b949e;--accent:#58a6ff;--green:#3fb950;--amber:#d29922;--red:#f85149;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding:16px;font-size:14px}
h1{font-size:18px;margin-bottom:4px}
a{color:var(--accent);text-decoration:none}
.sub{color:var(--dim);font-size:12px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:var(--dim);margin-bottom:10px}
.row{padding:6px 0;border-bottom:1px solid var(--border);font-size:13px}
.row:last-child{border:none}
.pill{font-size:10px;padding:1px 6px;border-radius:10px;background:#21262d;color:var(--dim);margin-left:4px}
.rest{color:var(--green)}.mcp{color:var(--accent)}
.scroll{max-height:360px;overflow-y:auto}
.full{grid-column:1/-1}.ok{color:var(--green)}.err{color:var(--red)}
code{color:var(--amber)}
</style></head><body>
<h1>🛰️ MCP Hub / Gateway <a href="/" style="font-size:13px">&larr; Team Dashboard</a></h1>
<div class="sub" id="updated">connecting...</div>
<div class="grid">
  <div class="card full"><h2>Targets</h2><div id="targets"></div></div>
  <div class="card"><h2>Routes</h2><div id="routes"></div></div>
  <div class="card"><h2>Usage</h2><div id="usage"></div></div>
  <div class="card"><h2>Vault keys</h2><div id="creds"></div></div>
  <div class="card full"><h2>Audit trail</h2><div class="scroll" id="audit"></div></div>
</div>
<script>
const E=id=>document.getElementById(id);
function esc(s){return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
async function tick(){
 try{
  const r=await fetch('/api/gateway'); const d=await r.json();
  E('targets').innerHTML=Object.entries(d.targets).map(([n,t])=>`<div class="row"><span class="${t.kind}">●</span> <b>${esc(n)}</b> <span class="pill">${t.kind}</span> ${t.enabled?'':'<span class="pill">disabled</span>'} <span class="pill">${t.capabilities||t.operations||0} ops</span><br><span class="sub">${esc(t.base_url||t.command||'')} ${(t.tags||[]).map(x=>'#'+esc(x)).join(' ')}</span></div>`).join('')||'<div class="row">none -- register a target</div>';
  E('routes').innerHTML=(d.routes||[]).slice().sort((a,b)=>(b.priority||0)-(a.priority||0)).map(r=>`<div class="row"><code>${esc(r.pattern)}</code> → ${esc(r.target)} <span class="pill">p${r.priority||0}</span></div>`).join('')||'<div class="row">none</div>';
  E('usage').innerHTML=`<div class="row sub">default ${d.default_limit}/min</div>`+Object.entries(d.stats||{}).map(([n,s])=>`<div class="row">${esc(n)}: ${s.calls||0} calls, <span class="${s.errors?'err':'ok'}">${s.errors||0} err</span></div>`).join('');
  E('creds').innerHTML=(d.credentials||[]).map(k=>`<div class="row">🔑 ${esc(k)} <span class="sub">(hidden)</span></div>`).join('')||'<div class="row">empty</div>';
  E('audit').innerHTML=(d.audit||[]).slice(-80).map(a=>`<div class="row"><span class="sub">${esc((a.time||'').split(' ')[1]||a.time)}</span> ${esc(a.agent)} → ${esc(a.target)} ${esc(a.op)} <span class="${a.ok===false?'err':'ok'}">[${esc(a.status)}]</span> ${a.ms?a.ms+'ms':''}</div>`).reverse().join('')||'<div class="row">no activity</div>';
  E('updated').textContent='live - '+new Date().toLocaleTimeString();
 }catch(e){E('updated').textContent='disconnected -- retrying...';}
}
tick();setInterval(tick,2000);
</script></body></html>"""


if __name__ == "__main__":
    mcp.run()
