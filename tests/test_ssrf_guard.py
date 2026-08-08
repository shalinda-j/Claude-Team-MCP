"""Gateway SSRF guard: which destinations the hub will and won't reach.

Every case here uses literal IPs, names resolved from /etc/hosts, or a
monkeypatched resolver, so the suite makes no real DNS queries and behaves the
same offline.
"""

import urllib.error

import pytest

import team_coordinator as tc


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every hostname to a public address, deterministically."""
    def fake(host, port, *a, **kw):
        return [(2, 1, 6, "", ("93.184.216.34", port or 443))]
    monkeypatch.setattr(tc._socket, "getaddrinfo", fake)


@pytest.fixture
def no_dns(monkeypatch):
    """Every hostname fails to resolve."""
    def fake(host, port, *a, **kw):
        raise OSError("Name or service not known")
    monkeypatch.setattr(tc._socket, "getaddrinfo", fake)


# --- What gets refused ---

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud instance metadata
    "http://127.0.0.1:8000/admin",
    "http://10.0.0.5/internal",
    "http://192.168.1.1/router",
    "http://172.16.0.1/",
    "http://0.0.0.0/",
    "http://[::1]/",
    "http://[::ffff:169.254.169.254]/",           # v4-mapped v6 must not slip past
])
def test_internal_destinations_are_refused(url):
    assert tc._gw_url_blocked(url)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "not a url",
])
def test_non_http_schemes_are_refused(url):
    reason = tc._gw_url_blocked(url)
    assert "not allowed" in reason


def test_localhost_by_name_is_refused():
    assert tc._gw_url_blocked("http://localhost:8000/x")


def test_public_destination_is_allowed(public_dns):
    assert tc._gw_url_blocked("https://api.stripe.com/v1") == ""


def test_host_resolving_to_both_public_and_internal_is_refused(monkeypatch):
    # A split-horizon name must not be waved through on the strength of its
    # public record alone.
    def fake(host, port, *a, **kw):
        return [(2, 1, 6, "", ("93.184.216.34", 443)),
                (2, 1, 6, "", ("169.254.169.254", 443))]
    monkeypatch.setattr(tc._socket, "getaddrinfo", fake)
    assert "169.254.169.254" in tc._gw_url_blocked("https://sneaky.example.com/")


def test_unresolvable_host_is_not_refused(no_dns):
    # Nothing to judge, and the connection will fail on its own. Registering a
    # target for a host that is merely down should still work.
    assert tc._gw_url_blocked("https://not-up-yet.example.com/") == ""


# --- Escape hatches ---

def test_allow_private_reopens_internal(monkeypatch):
    monkeypatch.setattr(tc, "GATEWAY_ALLOW_PRIVATE", True)
    assert tc._gw_url_blocked("http://localhost:8000/x") == ""
    assert tc._gw_url_blocked("http://169.254.169.254/") == ""


def test_allowlist_permits_only_named_hosts(monkeypatch, public_dns):
    monkeypatch.setattr(tc, "GATEWAY_ALLOWED_HOSTS", ["api.stripe.com", "localhost"])
    assert tc._gw_url_blocked("https://api.stripe.com/v1") == ""
    assert tc._gw_url_blocked("http://localhost:9000/x") == ""      # internal, but named
    assert "not in GATEWAY_ALLOWED_HOSTS" in tc._gw_url_blocked("https://evil.example.com/")


def test_allowlist_matches_subdomains(monkeypatch, public_dns):
    monkeypatch.setattr(tc, "GATEWAY_ALLOWED_HOSTS", ["stripe.com"])
    assert tc._gw_url_blocked("https://files.api.stripe.com/x") == ""
    # Suffix matching must be on a label boundary, not a bare string suffix.
    assert tc._gw_url_blocked("https://notstripe.com/x")


# --- Enforcement points ---

def test_register_rest_refuses_internal_target():
    out = tc.gateway_register_rest("meta", "http://169.254.169.254/", by_role="PM")
    assert "Refused to register" in out
    assert "meta" not in tc._gw_load().get("targets", {})


def test_call_rest_rechecks_a_target_registered_before_the_guard():
    # Simulate a target stored by an older version, so the register-time check
    # never ran on it.
    tc._gw_mutate(lambda gw: gw["targets"].__setitem__("legacy", {
        "kind": "rest", "base_url": "http://169.254.169.254", "auth": {"type": "none"},
        "default_headers": {}, "enabled": True, "tags": [],
    }))
    out = tc.gateway_call_rest("legacy", "/latest/meta-data/", agent="Backend")
    assert "Refused to call" in out


def test_blocked_call_is_audited():
    tc._gw_mutate(lambda gw: gw["targets"].__setitem__("legacy", {
        "kind": "rest", "base_url": "http://127.0.0.1", "auth": {"type": "none"},
        "default_headers": {}, "enabled": True, "tags": [],
    }))
    tc.gateway_call_rest("legacy", "/x", agent="Backend")
    audit = tc._gw_load()["audit"]
    assert audit[-1]["status"] == "blocked"
    assert audit[-1]["ok"] is False


def test_redirect_to_internal_address_is_refused():
    # urlopen follows redirects itself, so a public URL answering 302 with an
    # internal Location would otherwise bypass a check on the original URL.
    handler = tc._GwSafeRedirect()
    with pytest.raises(urllib.error.URLError, match="blocked redirect"):
        handler.redirect_request(None, None, 302, "Found", {},
                                 "http://169.254.169.254/latest/meta-data/")


def test_adapter_generator_refuses_internal_spec_url():
    out = tc.gateway_generate_adapter("http://169.254.169.254/openapi.json")
    assert "Could not load spec" in out and "refused to fetch spec" in out


def test_public_target_still_registers(public_dns):
    out = tc.gateway_register_rest("stripe", "https://api.stripe.com/v1", by_role="PM")
    assert "Registered" in out
