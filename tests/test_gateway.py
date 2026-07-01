"""MCP Hub / Gateway: targets, vault, routes, limits, audit, adapter generator."""

import json

import team_coordinator as tc


PETSTORE_SPEC = json.dumps({
    "openapi": "3.0.0",
    "info": {"title": "Petstore", "version": "1.0"},
    "servers": [{"url": "https://petstore.example.com/v1"}],
    "components": {"securitySchemes": {
        "ApiKey": {"type": "apiKey", "in": "header", "name": "X-API-Key"}}},
    "paths": {
        "/pets": {
            "get": {"operationId": "listPets", "summary": "List pets",
                    "parameters": [{"name": "limit", "in": "query"}]},
            "post": {"operationId": "createPet", "summary": "Create a pet"},
        },
        "/pets/{petId}": {
            "get": {"operationId": "getPet", "summary": "Get one pet",
                    "parameters": [{"name": "petId", "in": "path"}]},
        },
    },
})


def _register():
    tc.gateway_register_rest(
        "petstore", "https://petstore.example.com/v1",
        description="pets api", auth_type="header", auth_name="X-API-Key",
        credential_key="petstore_key", tags="pets,animals", by_role="PM")


# --- Target registry ---

def test_register_rest_target():
    out = _register()
    assert tc.GATEWAY_FILE.exists()
    gw = tc._gw_load()
    assert gw["targets"]["petstore"]["kind"] == "rest"
    assert gw["targets"]["petstore"]["tags"] == ["pets", "animals"]


def test_register_requires_name_and_url():
    assert "required" in tc.gateway_register_rest("", "")


def test_reregister_updates():
    _register()
    out = tc.gateway_register_rest("petstore", "https://petstore2.example.com", by_role="PM")
    assert "Updated" in out
    assert tc._gw_load()["targets"]["petstore"]["base_url"] == "https://petstore2.example.com"


def test_list_targets_and_filter():
    _register()
    tc.gateway_register_rest("billing", "https://billing.example.com", tags="payments")
    out = tc.gateway_list_targets()
    assert "petstore" in out and "billing" in out
    only_pets = tc.gateway_list_targets(tag="pets")
    assert "petstore" in only_pets and "billing" not in only_pets


def test_toggle_and_unregister():
    _register()
    tc.gateway_toggle("petstore", enabled=False)
    assert tc._gw_load()["targets"]["petstore"]["enabled"] is False
    out = tc.gateway_unregister("petstore")
    assert "petstore" not in tc._gw_load()["targets"]


# --- Vault ---

def test_vault_set_list_delete_never_leaks_value():
    out = tc.gateway_set_credential("stripe_key", "sk_live_supersecret123", by_role="PM")
    assert "sk_live_supersecret123" not in out  # masked
    listing = tc.gateway_list_credentials()
    assert "stripe_key" in listing
    assert "supersecret" not in listing
    assert "Deleted" in tc.gateway_delete_credential("stripe_key")
    assert "No credential" in tc.gateway_delete_credential("stripe_key")


def test_vault_requires_key_and_value():
    assert "required" in tc.gateway_set_credential("", "")


def test_vault_stored_separately_from_state():
    tc.gateway_set_credential("k", "v")
    assert tc.GATEWAY_VAULT.exists()
    raw_state = tc.STATE_FILE.read_text(encoding="utf-8") if tc.STATE_FILE.exists() else ""
    assert "v" == json.loads(tc.GATEWAY_VAULT.read_text(encoding="utf-8"))["k"]
    assert "secret" not in raw_state


# --- Routing ---

def test_routes_add_match_remove():
    _register()
    out = tc.gateway_add_route("pet", "petstore", priority=5, by_role="PM")
    assert "Route added" in out
    routed = tc.gateway_route("please fetch the pet named waffles")
    assert "petstore" in routed
    assert "petstore" in tc.gateway_list_routes()
    assert "Removed 1" in tc.gateway_remove_route("pet", "petstore")


def test_route_requires_registered_target():
    assert "not registered" in tc.gateway_add_route("x", "ghost")


def test_route_falls_back_to_tags():
    _register()
    out = tc.gateway_route("do something with animals today")
    assert "petstore" in out and "tag match" in out


def test_route_no_match():
    _register()
    assert "No route matched" in tc.gateway_route("launch the rocket")


# --- Limits, usage, audit ---

def test_set_limit_and_usage():
    _register()
    tc.gateway_set_limit("petstore", per_minute=5, by_role="PM")
    gw = tc._gw_load()
    assert tc._effective_limit(gw, "petstore") == 5
    assert isinstance(tc.gateway_usage(), str)


def test_rate_limiter_blocks_after_limit():
    results = [tc._gw_rate("agentX", "t", 3) for _ in range(5)]
    allowed = [ok for ok, _retry in results if ok]
    denied = [retry for ok, retry in results if not ok]
    assert len(allowed) == 3
    assert len(denied) == 2
    assert all(retry >= 1 for retry in denied)


def test_rate_limiter_unlimited_when_zero():
    assert tc._gw_rate("agentY", "t", 0) == (True, 0)


def test_audit_records_registrations():
    _register()
    out = tc.gateway_audit()
    assert "register_rest" in out
    filtered = tc.gateway_audit(target="petstore")
    assert "petstore" in filtered


# --- Adapter generator ---

def test_generate_adapter_from_inline_spec():
    out = tc.gateway_generate_adapter(PETSTORE_SPEC, by_role="PM")
    assert "Generated MCP adapter 'petstore'" in out
    assert "3 tools" in out
    adapter = tc.ADAPTER_DIR / "petstore.py"
    assert adapter.exists()
    code = adapter.read_text(encoding="utf-8")
    assert "listPets" in code
    assert "FastMCP" in code
    # Auto-registered as a REST target with operations catalogued.
    gw = tc._gw_load()
    assert gw["targets"]["petstore"]["base_url"] == "https://petstore.example.com/v1"
    assert len(gw["targets"]["petstore"]["operations"]) == 3
    # Discoverable through capabilities.
    caps = tc.gateway_capabilities("pets")
    assert "listPets" in caps


def test_generate_adapter_detects_auth():
    tc.gateway_generate_adapter(PETSTORE_SPEC)
    auth = tc._gw_load()["targets"]["petstore"]["auth"]
    assert auth["type"] == "header"
    assert auth["name"] == "X-API-Key"


def test_generate_adapter_rejects_bad_spec():
    assert "Could not load spec" in tc.gateway_generate_adapter("not json at all")
    assert "no 'paths'" in tc.gateway_generate_adapter('{"openapi": "3.0.0"}')


def test_spec_helpers():
    doc = json.loads(PETSTORE_SPEC)
    assert tc._gw_spec_base_url(doc) == "https://petstore.example.com/v1"
    assert tc._gw_detect_auth(doc) == ("header", "X-API-Key")
    ops = tc._gw_extract_ops(doc)
    assert {o["op_id"] for o in ops} == {"listPets", "createPet", "getPet"}
