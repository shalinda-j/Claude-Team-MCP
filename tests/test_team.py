"""Team coordination: join, chat, presence, availability, reliability tools."""

import threading
import time

import team_coordinator as tc


def test_join_team_registers_agent():
    out = tc.join_team("PM", "Pat")
    assert "Joined as Pat (PM)" in out
    state = tc._load()
    assert state["agents"]["PM"]["status"] == "online"
    # A system message announces the join.
    assert any("joined the team" in m["text"] for m in state["messages"])


def test_join_team_lists_other_members():
    tc.join_team("PM", "Pat")
    out = tc.join_team("Backend", "Ben")
    assert "Pat" in out


def test_post_and_read_channel(team):
    tc.post_message("PM", "hello team")
    out = tc.read_channel()
    assert "hello team" in out
    assert "next_index=" in out


def test_read_channel_since_index(team):
    tc.post_message("PM", "one")
    state = tc._load()
    idx = len(state["messages"])
    tc.post_message("PM", "two")
    out = tc.read_channel(since_index=idx)
    assert "two" in out and "one" not in out


def test_mention_flags_message_for_target(team):
    tc.post_message("PM", "please review", mention="QA")
    out = tc.read_channel(my_role="QA")
    assert "FOR YOU" in out
    assert "@QA" in out


def test_read_channel_empty():
    assert "No new messages" in tc.read_channel()


def test_wait_for_message_times_out(team):
    idx = len(tc._load()["messages"])
    out = tc.wait_for_message(since_index=idx, my_role="QA", timeout_seconds=1)
    assert "No new messages after waiting" in out


def test_wait_for_message_wakes_on_post(team):
    idx = len(tc._load()["messages"])

    def poster():
        time.sleep(0.4)
        tc.post_message("PM", "wake up!", mention="QA")

    t = threading.Thread(target=poster)
    t.start()
    out = tc.wait_for_message(since_index=idx, my_role="QA", timeout_seconds=10)
    t.join()
    assert "wake up!" in out


def test_set_status_idle_pings_pm(team):
    out = tc.set_status("Backend", "idle")
    assert "IDLE" in out
    assert tc._load()["agents"]["Backend"]["status"] == "idle"


def test_set_status_rejects_bad_value(team):
    assert "must be one of" in tc.set_status("Backend", "sleeping")


def test_who_is_free_buckets(team):
    tc.set_status("Backend", "idle")
    tc.set_status("QA", "busy")
    out = tc.who_is_free()
    idle_line = next(l for l in out.splitlines() if l.startswith("IDLE"))
    assert "Backend" in idle_line
    assert "QA" in out


def test_stale_agents_marked_offline(team, monkeypatch):
    monkeypatch.setattr(tc, "AGENT_STALE_SECONDS", 1)

    def age(state):
        state["agents"]["QA"]["last_seen"] = tc._ts() - 60
    tc._mutate(age)
    out = tc.who_is_free()
    off_line = next(l for l in out.splitlines() if l.startswith("Offline"))
    assert "QA" in off_line


def test_assign_work_creates_task_and_marks_busy(team):
    out = tc.assign_work("Ship the API", "Backend", by_role="PM")
    assert "assigned to @Backend" in out
    state = tc._load()
    assert state["tasks"][0]["title"] == "Ship the API"
    assert state["agents"]["Backend"]["status"] == "busy"


def test_acknowledge_and_read_receipts(team):
    tc.post_message("PM", "important notice")
    tc.acknowledge("Backend")
    out = tc.read_receipts()
    assert "Seen by: Backend" in out
    assert "QA" in out.split("NOT seen by:")[1]


def test_ping_alive_agent(team):
    out = tc.ping("PM", target_role="Backend")
    assert "ALIVE" in out


def test_ping_all_reports_everyone(team):
    out = tc.ping("PM")
    for role in ("PM", "Backend", "QA"):
        assert role in out


def test_safe_call_retry_then_escalate(team):
    out1 = tc.safe_call("Backend", "flaky build", attempt=1, max_attempts=3)
    assert "attempt 1/3" in out1
    out3 = tc.safe_call("Backend", "flaky build", attempt=3, max_attempts=3)
    assert "Escalated to PM" in out3
