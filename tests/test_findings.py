"""Security findings lifecycle: report -> triage -> assign fix -> verify -> report."""

import team_coordinator as tc


def _report():
    return tc.report_finding(
        "SAST-Auditor", "SQL injection in login", "critical",
        location="src/auth/login.py:42",
        description="user input concatenated into query",
        recommendation="use parameterized queries",
        category="injection")


def test_report_finding_and_notify(team):
    out = _report()
    assert "Finding #1" in out and "CRITICAL" in out
    f = tc._load()["findings"][0]
    assert f["status"] == "open"
    assert f["severity"] == "critical"
    # Security-Lead is pinged in the channel.
    assert any(m.get("mention") == "Security-Lead" for m in tc._load()["messages"])


def test_report_finding_invalid_severity(team):
    out = tc.report_finding("SAST-Auditor", "bad severity", "catastrophic")
    assert "severity must be one of" in out
    assert tc._load()["findings"] == []


def test_list_findings_sorted_and_filtered(team):
    _report()
    tc.report_finding("Config-Auditor", "Verbose error pages", "low", category="config")
    out = tc.list_findings()
    # Critical sorts before low.
    assert out.index("SQL injection") < out.index("Verbose error")
    only_low = tc.list_findings(severity="low")
    assert "Verbose error" in only_low and "SQL injection" not in only_low
    by_cat = tc.list_findings(category="injection")
    assert "SQL injection" in by_cat


def test_get_finding_detail(team):
    _report()
    out = tc.get_finding(1)
    assert "parameterized queries" in out
    assert "src/auth/login.py:42" in out
    assert "No finding #9" in tc.get_finding(9)


def test_triage_finding(team):
    _report()
    out = tc.triage_finding(1, "confirmed", by_role="Security-Lead", note="verified by hand")
    assert "confirmed" in out
    assert tc._load()["findings"][0]["status"] == "confirmed"
    assert "status must be one of" in tc.triage_finding(1, "maybe")


def test_assign_fix_creates_high_priority_task(team):
    _report()
    out = tc.assign_fix(1, "Backend", by_role="Security-Lead")
    assert "task #1 created" in out
    state = tc._load()
    task = state["tasks"][0]
    assert task["priority"] == "high"          # critical finding => high priority
    assert task["finding_id"] == 1
    assert task["assignee"] == "Backend"
    assert state["findings"][0]["assigned_to"] == "Backend"
    assert state["agents"]["Backend"]["status"] == "busy"


def test_verify_fix_closes_or_reopens(team):
    _report()
    tc.verify_fix(1, True, by_role="QA")
    assert tc._load()["findings"][0]["status"] == "verified"
    tc.verify_fix(1, False, by_role="QA")
    assert tc._load()["findings"][0]["status"] == "open"


def test_security_report_written_to_disk(team):
    _report()
    tc.report_finding("Config-Auditor", "Verbose error pages", "low", category="config")
    out = tc.security_report(by_role="Security-Lead")
    assert "Security report saved" in out
    assert "UNRESOLVED CRITICAL / HIGH" in out
    reports = list(tc.STATE_FILE.parent.glob("security_report_*.md"))
    assert len(reports) == 1
    assert "SQL injection" in reports[0].read_text(encoding="utf-8")


def test_security_report_empty(team):
    assert "No findings" in tc.security_report()
