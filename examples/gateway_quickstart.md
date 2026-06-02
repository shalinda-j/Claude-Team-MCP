# MCP Hub / Gateway — quick start

The hub turns this server into a **router/proxy** in front of other tools.
Register it once in your client (exactly like the team server — same file, same
state), then register downstream MCP servers and REST APIs as **targets**. Every
agent reaches all of them through the one hub connection.

## Why

- **One connection, many tools.** Stop adding a new MCP server entry per
  technology — register them with the hub instead.
- **Unified auth.** Secrets live in a `chmod 600` vault and are injected by the
  hub at call time. Agents never see API keys.
- **Governance.** Per-agent rate limits + a central audit trail of every call.
- **No more hand-written adapters.** Generate a full MCP server from an OpenAPI
  spec in one call.

## 1. Put a secret in the vault

```
gateway_set_credential("openweather_key", "<your-api-key>")
```

`gateway_list_credentials` shows only the **keys** — values are never returned.

## 2. Register targets

### A REST API

```
gateway_register_rest(
  name="weather",
  base_url="https://api.openweathermap.org/data/2.5",
  auth_type="query", auth_name="appid", credential_key="openweather_key",
  tags="weather,forecast")
```

`auth_type` is one of `none | header | bearer | query | basic`.

### Another MCP server

```
gateway_register_mcp(
  name="github",
  command="npx",
  args="-y @modelcontextprotocol/server-github",
  env='{"GITHUB_PERSONAL_ACCESS_TOKEN":"vault:gh_token"}',
  tags="git,issues")

gateway_discover("github")     # spawn it once and cache its tool list
```

Use a `vault:KEY` value in `env` to inject a stored secret without writing it
into the registry.

## 3. Discover and call — as any agent

```
gateway_capabilities()                 # "what can I do?" across every target
gateway_capabilities("issue")          # filter by keyword

gateway_call_rest("weather", path="/weather", query="q=London,uk", agent="Backend")
gateway_call_tool("github", "create_issue", arguments='{"title":"Bug","body":"..."}', agent="Backend")
```

The hub injects the credential, enforces the rate limit, and logs the call.

## 4. Routing, limits, audit

```
gateway_add_route("forecast", "weather", priority=10)   # keyword -> target
gateway_route("show me the forecast for tomorrow")      # -> weather

gateway_set_limit("weather", 30)        # 30 calls/min per agent (0 = unlimited)

gateway_audit(last_n=20)                # who called what, when, status, latency
gateway_usage()                         # per-target calls/errors/limits
```

Open the team dashboard (`start_dashboard`) and click **Gateway →**, or browse
`/gateway` directly, for a live view of targets, routes, usage, vault keys, and
the audit trail.

## 5. Auto-Adapter Generator (the killer feature)

Turn any OpenAPI/Swagger document into a complete, runnable MCP server:

```
# from a URL
gateway_generate_adapter("https://petstore3.swagger.io/api/v3/openapi.json",
    name="petstore", credential_key="petstore_key")

# from the bundled sample file
gateway_generate_adapter("examples/petstore_openapi.json", name="petstore")
```

This writes `<ADAPTER_DIR>/petstore.py` — one MCP tool per operation — and
auto-registers it as a hub REST target, so it is callable and discoverable
immediately:

```
gateway_set_credential("petstore_key", "<api-key>")
gateway_capabilities("pet")
gateway_call_rest("petstore", path="/pet/findByStatus", query="status=available", agent="QA")
```

You can also run the generated file as a standalone MCP server:

```
env PETSTORE_API_KEY=... python <ADAPTER_DIR>/petstore.py
```

JSON specs work out of the box; YAML specs require `PyYAML` (`pip install pyyaml`).
