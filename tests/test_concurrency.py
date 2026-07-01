"""Concurrency: parallel writers must not lose or corrupt data."""

import json
import threading

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


def test_state_file_valid_json_after_burst():
    for i in range(30):
        tc.set_fact(f"key{i}", f"value{i}")
    # File on disk parses cleanly and holds every fact.
    data = json.loads(tc.STATE_FILE.read_text(encoding="utf-8"))
    assert len(data["facts"]) == 30
