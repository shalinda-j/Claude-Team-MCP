"""Channel indices survive rotation.

Indices used to be positions in the retained list, and rotation renumbered them
underneath everyone. A caught-up agent held next_index == len(messages); once
the channel was pinned at MSG_ROTATE_LIMIT that length stopped growing, so
`total > since_index` was never true again and every agent went permanently deaf
at 2000 messages, @mentions included, with no error anywhere. An agent that was
behind had its indices reused, so it skipped what rotated out and mislabelled
the rest.

MSG_ROTATE_LIMIT is lowered here rather than posting 2000 messages -- it is the
same code path.
"""

import pytest

import team_coordinator as tc


@pytest.fixture(autouse=True)
def small_channel(monkeypatch):
    monkeypatch.setattr(tc, "MSG_ROTATE_LIMIT", 5)


def _next(state=None):
    return tc._msg_next_index(state or tc._load())


def test_a_caught_up_agent_still_receives_messages_after_rotation():
    tc.join_team("Backend", "Ben")
    for i in range(4):
        tc.post_message("PM", f"m{i}")
    nxt = _next()

    for i in range(3):
        tc.post_message("PM", f"URGENT-{i}", mention="Backend")

    out = tc.read_channel(nxt, "Backend")
    for i in range(3):
        assert f"URGENT-{i}" in out
    assert "FOR YOU" in out


def test_wait_for_message_wakes_after_rotation():
    tc.join_team("Backend", "Ben")
    for i in range(6):
        tc.post_message("PM", f"m{i}")
    nxt = _next()
    tc.post_message("PM", "after rotation", mention="Backend")

    out = tc.wait_for_message(nxt, "Backend", timeout_seconds=2)
    assert "after rotation" in out
    assert "No new messages" not in out


def test_next_index_keeps_climbing_past_the_limit():
    for i in range(30):
        tc.post_message("PM", f"m{i}")
    state = tc._load()
    assert len(state["messages"]) == 5          # the file stays bounded
    assert _next(state) == 30                   # but indices do not reset
    assert state["archived_messages"] == 25


def test_a_reader_polling_every_message_misses_none():
    """The real usage: read, take next_index, read again -- across many rotations."""
    seen, nxt = [], _next()
    for i in range(40):
        tc.post_message("PM", f"bulk-{i}")
        out = tc.read_channel(nxt, "Backend")
        seen += [line for line in out.splitlines() if "bulk-" in line]
        nxt = int(out.rsplit("next_index=", 1)[1])
    assert len(seen) == 40


def test_an_index_always_names_the_same_message():
    for i in range(3):
        tc.post_message("PM", f"m{i}")
    at_one = tc.read_channel(1).splitlines()[0]
    assert "m1" in at_one
    for i in range(20):                          # force several rotations
        tc.post_message("PM", f"filler-{i}")
    # Index 1 is gone now, but nothing else has taken its number.
    later = tc.read_channel(18)
    assert "m1" not in later
    assert "[18]" in later


def test_a_reader_that_fell_behind_is_told():
    for i in range(20):
        tc.post_message("PM", f"m{i}")
    out = tc.read_channel(0, "Backend")
    assert "rotated out" in out
    # and what it does return is labelled with true indices
    assert "[15]" in out


def test_negative_index_is_clamped():
    tc.post_message("PM", "only")
    out = tc.read_channel(-5, "Backend")
    assert "[-5]" not in out and "[-1]" not in out
    assert "only" in out


def test_wait_for_message_with_a_negative_index_does_not_return_bogus_labels():
    tc.post_message("PM", "only")
    out = tc.wait_for_message(-3, "PM", timeout_seconds=1)
    assert "[-3]" not in out


def test_no_new_messages_reports_the_absolute_next_index():
    for i in range(20):
        tc.post_message("PM", f"m{i}")
    nxt = _next()
    assert f"next_index={nxt}" in tc.read_channel(nxt, "Backend")


# --- receipts, which index the same channel ---

def test_acknowledging_an_explicit_index_is_seen_as_read():
    tc.join_team("PM", "Pat")
    for i in range(4):
        tc.post_message("PM", f"m{i}")
    last = _next() - 1
    tc.acknowledge("PM", up_to_index=last)
    assert "Seen by: PM" in tc.read_receipts(last)


def test_acknowledge_everything_still_works():
    tc.join_team("PM", "Pat")
    for i in range(4):
        tc.post_message("PM", f"m{i}")
    tc.acknowledge("PM", up_to_index=-1)
    assert "Seen by: PM" in tc.read_receipts(-1)


def test_receipts_survive_rotation():
    tc.join_team("PM", "Pat")
    for i in range(20):
        tc.post_message("PM", f"m{i}")
    tc.acknowledge("PM", up_to_index=-1)
    tc.post_message("Backend", "brand new")
    out = tc.read_receipts(-1)
    assert "NOT seen by: PM" in out          # PM has not read the newest one
