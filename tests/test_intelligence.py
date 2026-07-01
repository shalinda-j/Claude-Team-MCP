"""Intelligence layer: similarity, smart search, routing, conflicts, metrics."""

import team_coordinator as tc


def test_similarity_basics():
    assert tc._similarity("jwt auth tokens", "jwt auth tokens") > 0.9
    assert tc._similarity("jwt auth tokens", "grocery shopping list") == 0.0
    partial = tc._similarity("fix the login api bug", "login api")
    assert 0 < partial <= 1


def test_tokenize_drops_stopwords():
    tokens = tc._tokenize("the quick brown fox is on the run")
    assert "the" not in tokens
    assert "quick" in tokens


def test_smart_search_ranks_relevant_note_first():
    tc.save_note("JWT tokens expire after 7 days", tags="auth", by_role="Backend")
    tc.save_note("Postgres connection pool size is 20", tags="db", by_role="Backend")
    tc.brain_add("Deploy runbook", "deploy with docker compose up")
    out = tc.smart_search("jwt token expiry")
    assert "JWT" in out
    lines = out.splitlines()
    hits = [l for l in lines if l.strip().startswith("[")]
    assert "JWT" in hits[0]


def test_smart_search_no_match():
    tc.save_note("something unrelated", by_role="PM")
    assert "No relevant matches" in tc.smart_search("zzzz qqqq xxxx")


def test_suggest_route_prefers_skill_match(team):
    tc.set_skills("Backend", "api database python")
    tc.set_skills("QA", "testing selenium")
    out = tc.suggest_route("fix the database api endpoint")
    assert "Suggested: @Backend" in out


def test_suggest_route_without_agents():
    assert "No agents registered" in tc.suggest_route("anything")


def test_check_conflicts_flags_double_booking(team):
    tc.add_task("Refactor login page UI", assignee="Backend", created_by="PM")
    tc.add_task("Improve login page UI styling", assignee="QA", created_by="PM")
    tc.update_task(1, status="in_progress", by_role="Backend")
    tc.update_task(2, status="in_progress", by_role="QA")
    out = tc.check_conflicts()
    assert "overlap" in out.lower()


def test_check_conflicts_flags_multitasking_agent(team):
    tc.add_task("Task A alpha", assignee="Backend", created_by="PM")
    tc.add_task("Task B beta", assignee="Backend", created_by="PM")
    tc.update_task(1, status="in_progress", by_role="Backend")
    tc.update_task(2, status="in_progress", by_role="Backend")
    out = tc.check_conflicts()
    assert "2 tasks in progress" in out


def test_check_conflicts_clean_board(team):
    assert "No conflicts detected" in tc.check_conflicts()


def test_metrics_counts(team):
    tc.add_task("one", created_by="PM")
    tc.add_task("two", created_by="PM")
    tc.update_task(1, status="done", by_role="PM")
    out = tc.metrics()
    assert "2 total" in out
    assert "1 done" in out


def test_timeline_shows_recent_events(team):
    tc.add_task("traceable task", created_by="PM")
    out = tc.timeline()
    assert "added task #1" in out


def test_export_report_written_to_disk(team):
    tc.add_task("Build API", assignee="Backend", created_by="PM")
    out = tc.export_report(by_role="PM")
    assert "Report saved to" in out
    reports = list(tc.STATE_FILE.parent.glob("team_report_*.md"))
    assert len(reports) == 1
    body = reports[0].read_text(encoding="utf-8")
    assert "Build API" in body


def test_suggest_worktrees_lists_non_pm_agents(team):
    out = tc.suggest_worktrees("/repo/myproject")
    assert "git worktree add" in out
    assert "backend" in out
    assert "qa" in out
