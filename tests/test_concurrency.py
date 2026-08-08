"""Concurrency: parallel writers must not lose or corrupt data."""

import json
import threading

import pytest
from filelock import FileLock

import team_coordinator as tc


def test_parallel_posts_lose_nothing():
    """8 threads x 25 messages each: all 200 must survive (READMEs headline claim)."""
    threads = []

    def worker(n):
        for i in range(25):
            tc.post_message(f"agent{n}", f"msg {n}-{i}")

    for n in range(8):
        t = threading.Thread(target=worker, args=(n,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    msgs = tc._load()["messages"]
    assert len(msgs) == 200
    # Every individual message made it, none clobbered.
    seen = {m["text"] for m in msgs}
    assert all(f"msg {n}-{i}" in seen for n in range(8) for i in range(25))


def test_parallel_task_ids_are_unique():
    threads = []

    def worker(n):
        for i in range(10):
            tc.add_task(f"task {n}-{i}", created_by=f"agent{n}")

    for n in range(6):
        t = threading.Thread(target=worker, args=(n,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    tasks = tc._load()["tasks"]
    assert len(tasks) == 60
    ids = [t["id"] for t in tasks]
    assert len(set(ids)) == 60, "duplicate task ids under concurrency"


def test_unacquirable_lock_refuses_instead_of_losing_writes(monkeypatch):
    """A writer that cannot take the lock must fail, not write unserialized.

    Every write is a read-modify-write of the whole state file, so continuing
    without the lock does not corrupt a field -- it reloads a stale copy and
    silently drops whatever anyone else wrote in the meantime. Before this was
    fixed, 8 writers blocked on a held lock produced 1 surviving message and 7
    that vanished with no error anywhere.
    """
    monkeypatch.setattr(tc, "LOCK_TIMEOUT", 1)
    tc.join_team("PM")
    baseline = len(tc._load()["messages"])

    holder = FileLock(str(tc.LOCK_FILE))
    holder.acquire()
    try:
        results = []

        def writer(n):
            try:
                tc.post_message("PM", f"blocked-{n}")
                results.append(("wrote", n))
            except tc.LockBusy:
                results.append(("refused", n))

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        holder.release()

    # Whatever the split, nothing may disappear: a write either lands or raises.
    wrote = [n for kind, n in results if kind == "wrote"]
    assert len(results) == 8
    persisted = [m for m in tc._load()["messages"] if m["text"].startswith("blocked-")]
    assert len(persisted) == len(wrote)
    assert len(tc._load()["messages"]) == baseline + len(wrote)


def test_lock_busy_is_raised_not_swallowed(monkeypatch):
    monkeypatch.setattr(tc, "LOCK_TIMEOUT", 1)
    holder = FileLock(str(tc.LOCK_FILE))
    holder.acquire()
    try:
        with pytest.raises(tc.LockBusy, match="retry the call"):
            tc._mutate(lambda s: s["facts"].__setitem__("k", "v"))
    finally:
        holder.release()
    # Nothing was written by the refused call.
    assert "k" not in tc._load()["facts"]


def test_writes_still_work_once_the_lock_is_free():
    for i in range(8):
        tc.post_message("PM", f"free-{i}")
    texts = {m["text"] for m in tc._load()["messages"]}
    assert all(f"free-{i}" in texts for i in range(8))


def test_state_file_valid_json_after_burst():
    for i in range(30):
        tc.set_fact(f"key{i}", f"value{i}")
    # File on disk parses cleanly and holds every fact.
    data = json.loads(tc.STATE_FILE.read_text(encoding="utf-8"))
    assert len(data["facts"]) == 30
