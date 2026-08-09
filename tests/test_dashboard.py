"""Dashboard: authentication, headers, routing, and output escaping.

The dashboard served team state, security findings and vault key names over
plain HTTP with no authentication and `Access-Control-Allow-Origin: *`, so
binding to 127.0.0.1 bought nothing -- any page open in the user's browser could
read both JSON endpoints cross-origin. Three fields (`role`, `assignee`,
`judge_role`) then went into innerHTML unescaped, giving an agent a way to put a
payload where a viewer would run it.
"""

import json
import re
import threading
import urllib.error
import urllib.request

import pytest

import team_coordinator as tc

PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(tc, "DASHBOARD_TOKEN", "test-dash-token")
    monkeypatch.setattr(tc, "_dashboard_server", None)
    monkeypatch.setattr(tc, "_dashboard_thread", None)
    tc.start_dashboard(port=PORT)
    try:
        yield "test-dash-token"
    finally:
        tc.stop_dashboard()


def _get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=5) as r:
            return r.getcode(), r.read().decode(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), dict(e.headers)


# --- Authentication ---

def test_unauthenticated_read_is_refused(server):
    for path in ("/", "/gateway", "/api/state", "/api/gateway"):
        code, body, _ = _get(path)
        assert code == 401, path
        assert "token required" in body


def test_a_wrong_token_is_refused(server):
    assert _get("/api/state?t=guess")[0] == 401


def test_the_token_grants_access(server):
    code, body, _ = _get(f"/api/state?t={server}")
    assert code == 200
    assert "agents" in json.loads(body)


def test_the_token_also_works_as_a_header(server):
    req = urllib.request.Request(BASE + "/api/state",
                                 headers={"X-Dashboard-Token": server})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.getcode() == 200


def test_the_refusal_does_not_leak_the_token(server):
    assert server not in _get("/api/state?t=guess")[1]


# --- Cross-origin and headers ---

def test_no_wildcard_cors(server):
    """The wildcard is what defeated the loopback bind."""
    for path in (f"/api/state?t={server}", f"/api/gateway?t={server}"):
        _, _, headers = _get(path)
        assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("header,value", [
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Cache-Control", "no-store"),
])
def test_security_headers_are_set(server, header, value):
    _, _, headers = _get(f"/api/state?t={server}")
    assert headers.get(header) == value


def test_csp_confines_where_a_payload_could_send_data(server):
    _, _, headers = _get(f"/api/state?t={server}")
    csp = headers.get("Content-Security-Policy", "")
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_server_header_hides_the_python_version(server):
    _, _, headers = _get(f"/api/state?t={server}")
    assert "Python" not in headers.get("Server", "")


# --- Routing ---

def test_unknown_paths_are_404_not_the_dashboard(server):
    code, body, _ = _get(f"/nope?t={server}")
    assert code == 404
    assert "Team Dashboard" not in body


def test_known_routes_still_serve(server):
    for path in ("/", "/gateway"):
        code, body, _ = _get(f"{path}?t={server}")
        assert code == 200 and "<html" in body.lower()


# --- Escaping ---

HOSTILE = '<img src=x onerror=alert(1)>'


def _render(expr_pattern, data):
    """Evaluate one of the page's real innerHTML expressions over `data`.

    Rendering is client-side, so a template that says esc() is not proof --
    only the markup it produces is. This lifts the actual expression out of the
    served page and runs it, so the test tracks the shipped code.
    """
    import shutil
    import subprocess
    import tempfile
    node = shutil.which("node")
    if not node:
        pytest.skip("node not available")
    page = tc._dashboard_html()
    esc = re.search(r"function esc\(s\)\{.*?\}\n", page, re.S).group(0)
    expr = re.search(expr_pattern, page, re.S).group(1)
    script = (esc + "const d=" + json.dumps(data) + ";\n"
              "const dd=d.debate;\nconsole.log(" + expr + ");\n")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
        path = f.name
    return subprocess.run([node, path], capture_output=True, text=True, timeout=30).stdout


AGENTS_EXPR = r"E\('agents'\)\.innerHTML=(.*?);\n"
TASKS_EXPR = r"E\('tasks'\)\.innerHTML=(.*?);\n"
DEBATE_EXPR = r"if\(d\.debate\)\{const dd=d\.debate;E\('debate'\)\.innerHTML=(.*?);\}else"


def _injected_tags(html):
    """Tags in the output that the template did not author."""
    return [t for t in re.findall(r"<[^>]*>", html)
            if re.match(r"^</?(img|script|svg|iframe)\b", t, re.I)
            or re.search(r"\son\w+\s*=", t, re.I)]


def test_a_hostile_agent_role_does_not_become_markup():
    tc.join_team(HOSTILE, "Bob")
    out = _render(AGENTS_EXPR, {"agents": tc._load()["agents"]})
    assert _injected_tags(out) == []
    assert "&lt;img" in out           # present, but as text


def test_a_hostile_assignee_does_not_become_markup():
    tc.add_task("t", assignee='"><script>alert(1)</script>', created_by="PM")
    out = _render(TASKS_EXPR, {"tasks": tc._load()["tasks"]})
    assert _injected_tags(out) == []


def test_a_hostile_judge_role_does_not_become_markup():
    tc.start_debate("topic", judge_role="</span><svg onload=alert(1)>", by_role="PM")
    out = _render(DEBATE_EXPR, {"debate": tc._load()["debate"]})
    assert _injected_tags(out) == []


def test_esc_escapes_quotes_so_attribute_slots_are_safe():
    """esc() ignored quotes, so any escaped value inside an attribute was still
    injectable. Both pages share the implementation."""
    for page in (tc._dashboard_html(), tc._gateway_html()):
        fn = re.search(r"function esc\(s\)\{.*?\}\n", page, re.S).group(0)
        for ch in ('"', "'", "&", "<", ">"):
            assert ch in fn, f"esc does not handle {ch!r}"


def test_no_raw_interpolation_of_agent_controlled_fields():
    page = tc._dashboard_html()
    for field in ("a.role", "t.assignee", "dd.judge_role", "f.severity", "f.status"):
        assert f"${{{field}" not in page, f"{field} is interpolated without esc()"
