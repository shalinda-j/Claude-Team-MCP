"""Project memory (notes, facts, summaries, log) and the second brain."""

import team_coordinator as tc


# --- Project memory ---

def test_save_and_search_notes():
    tc.save_note("Auth uses JWT with 7-day expiry", tags="auth api", by_role="Backend")
    tc.save_note("DB is postgres 16", tags="db", by_role="Backend")
    out = tc.search_notes(query="jwt")
    assert "JWT" in out and "postgres" not in out
    out = tc.search_notes(tag="db")
    assert "postgres" in out


def test_search_notes_no_match():
    tc.save_note("something", by_role="PM")
    assert "No matching notes" in tc.search_notes(query="zzz-does-not-exist")


def test_set_and_get_fact():
    tc.set_fact("api_base_url", "https://api.example.com", by_role="PM")
    assert "https://api.example.com" in tc.get_facts("api_base_url")
    # Overwrite semantics
    tc.set_fact("api_base_url", "https://api2.example.com", by_role="PM")
    assert "api2" in tc.get_facts("api_base_url")


def test_get_facts_unknown_key_lists_known():
    tc.set_fact("deadline", "friday")
    out = tc.get_facts("nope")
    assert "No fact named 'nope'" in out
    assert "deadline" in out


def test_get_facts_empty():
    assert "No facts stored yet" in tc.get_facts()


def test_summary_save_and_load():
    tc.save_summary("Sprint 1 done. API deployed.", by_role="PM")
    tc.save_summary("Sprint 2 started.", by_role="PM")
    latest = tc.load_summary("latest")
    assert "Sprint 2" in latest
    everything = tc.load_summary("all")
    assert "Sprint 1" in everything and "Sprint 2" in everything
    by_id = tc.load_summary("1")
    assert "Sprint 1" in by_id


def test_load_summary_empty():
    assert "No summaries saved yet" in tc.load_summary()


def test_project_log_records_actions():
    tc.join_team("PM")
    tc.save_note("note", by_role="PM")
    out = tc.project_log()
    assert "joined the team" in out
    assert "saved note" in out


def test_context_checkpoint_saves_summary():
    out = tc.context_checkpoint("Backend", work_done="built /users endpoint",
                                decisions="use JWT", next_steps="add tests")
    assert "Checkpoint #1 saved" in out
    loaded = tc.load_summary("latest")
    assert "built /users endpoint" in loaded
    assert "DECISIONS: use JWT" in loaded


# --- Second brain ---

def test_brain_add_and_get():
    out = tc.brain_add("JWT decision", "We chose JWT for stateless auth",
                       category="decisions", tags="auth")
    assert "#1" in out
    got = tc.brain_get(1)
    assert "JWT decision" in got
    assert "stateless auth" in got


def test_brain_get_missing():
    assert "No brain note #42" in tc.brain_get(42)


def test_brain_search_by_text_category_tag():
    tc.brain_add("JWT decision", "stateless auth tokens", category="decisions", tags="auth")
    tc.brain_add("Grocery list", "milk and eggs", category="personal", tags="food")
    assert "JWT" in tc.brain_search(query="stateless")
    assert "Grocery" in tc.brain_search(category="personal")
    assert "JWT" in tc.brain_search(tag="auth")


def test_brain_link_and_backlinks():
    tc.brain_add("A", "first note")
    tc.brain_add("B", "second note")
    out = tc.brain_link(1, 2, rel="explains")
    assert "1" in out and "2" in out
    back = tc.brain_backlinks(2)
    assert "A" in back or "#1" in back


def test_brain_daily_journal():
    out = tc.brain_daily("Shipped the API")
    assert "journal" in out.lower() or "Daily" in out
    listing = tc.brain_list()
    assert isinstance(listing, str)


def test_brain_persists_on_disk():
    tc.brain_add("persisted", "this should hit the disk")
    assert tc.BRAIN_FILE.exists()
    # Re-load from disk and check the note survived.
    brain = tc._brain_load()
    assert any(n["title"] == "persisted" for n in brain["notes"])
