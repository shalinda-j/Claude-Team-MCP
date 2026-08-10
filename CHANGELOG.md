# Changelog

All notable changes to this project are documented here.

## v8.8

- **Added — container image.** `Dockerfile`, `.dockerignore` and
  `docker-compose.yml`. The server is stdio, so a client attaches to the
  container (`docker run --rm -i`); a TTY would corrupt the JSON-RPC stream, so
  compose sets `stdin_open` without `tty`. Everything stateful lives under
  `/data` as a single volume, the image runs as an unprivileged user (the hub
  spawns processes an operator registers — it should not do that as root), tini
  reaps those children and forwards signals, and the healthcheck is
  `claude-team-mcp doctor`, which already exits non-zero on a broken
  environment. Compose also has a `dashboard` profile that publishes to the
  host's loopback only.
- **Added — the image is tested, not just built.** Both pipelines build it and
  then run `doctor` inside it, assert the container is not root, prove state
  written by one container is visible to the next through the volume, and pipe
  an `initialize` request through `docker run -i` to confirm the handshake
  replies.
- **Fixed — workflows ran with more privilege and less bounding than they
  needed.** Neither had a `permissions:` block, so both inherited the
  repository default; both are now `contents: read`. Neither had
  `concurrency:`, so pushing twice ran two full matrices; CI now cancels
  superseded runs on branches but never on `main`, and the mirror queues rather
  than racing two force-pushes at the same ref. No job had a
  `timeout-minutes`, so a hung one would have burned the six-hour default.
- **Fixed — the mirror put a token in `argv`.** `git push` with credentials
  inline is visible to any other process on the runner; it now goes through
  `http.extraheader`, checks out with `persist-credentials: false`, and fails
  with a clear message when `GITLAB_MIRROR_URL` is set but the token secret is
  not.
- **Changed — the GitLab pipeline caught up with GitHub.** It gained the two
  `mcp` SDK legs and the `doctor` step it was documented as lacking, plus the
  container job, `interruptible: true`, explicit timeouts, and one YAML anchor
  for the trigger rules that were copy-pasted onto every job. `pip install -e
  .[dev]` is quoted, since `[dev]` is a glob to `sh`.

## v8.7

- **Fixed — the adapter generator let a spec write code, not just data.**
  `gateway_generate_adapter` builds a Python file by concatenating spec-derived
  strings into source, and `_gw_load_spec` fetches specs over HTTPS — so "wrap
  `https://vendor.example/openapi.json`" meant whoever served that URL chose
  part of a `.py` file on your disk. A value containing a quote closed the
  literal it landed in and opened a fresh statement. Five sinks: the spec's
  `servers[0].url`, a Swagger 2 `host`+`basePath`, the `base_url` argument, a
  `securitySchemes[*].name`, and a path-parameter name (query and header
  parameters already used `repr()`; only the path branch built its literal by
  hand). A sixth, a `paths` key, reached the docstring beside the hardened
  `summary` with none of its treatment. Everything reaching a code position now
  goes through `repr()`, everything reaching a docstring through a single
  `_gw_docsafe()`, and the generated module is parsed before it is written so a
  future slip fails loudly instead of landing a broken or hostile file.
- **Fixed — every generated adapter was dead on `mcp` 2.x.** The template
  hardcoded `from mcp.server.fastmcp import FastMCP`. v8.3 taught the server to
  survive that rename and never touched the file it writes; the suite passed
  throughout because nothing had ever executed the output. The template now
  uses the same shim, and a test loads a generated adapter and lists its tools.
- **Added — tests:** 20 new (242 total). They assert on the parsed AST rather
  than on substrings, since an escaped payload still contains its own text —
  only the tree distinguishes inert data from a live call. Verified meaningful
  by running the same vectors against the previous release, where four of them
  land a call and a fifth writes a file that will not parse. Green against both
  `mcp<2` and `mcp>=2`.

## v8.6

Dashboard hardening, continuing the audit against HashiCorp Vault.
**Contains a breaking change** — the dashboard now needs a token.

- **BREAKING — the dashboard requires a token.** It serves the full channel,
  the task board, security findings, and the gateway's vault key names, and it
  served all of that to anyone who could reach the port. Worse, both JSON
  endpoints carried `Access-Control-Allow-Origin: *`, so binding to `127.0.0.1`
  bought nothing: any page open in the user's browser could
  `fetch('http://127.0.0.1:8765/api/state')` cross-origin and read it. The
  wildcard is gone and `start_dashboard` now mints a random token per run and
  returns it in the URL (`?t=…`); `DASHBOARD_TOKEN` keeps one URL across
  restarts, and `X-Dashboard-Token` works as a header. **Open the whole URL
  `start_dashboard` prints.**
- **Fixed — stored XSS in three fields.** `role`, `assignee`, and `judge_role`
  reached `innerHTML` unescaped and are validated nowhere, so
  `join_team(role="<img src=x onerror=…>")` put a payload in front of every
  viewer, re-firing every 2 seconds and persisting until `reset_team`. All
  three are escaped now, along with six attribute slots that were one relaxed
  validator away from the same bug.
- **Fixed — `esc()` ignored quotes**, so any escaped value sitting inside an
  HTML attribute was still injectable. It now escapes `& < > " ' \``.
- **Added — security headers**: `Cache-Control: no-store`,
  `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, and a CSP whose `connect-src 'self'` leaves an
  injected payload nowhere to send what it reads.
- **Fixed — unknown paths returned 200 and the full dashboard.** They 404 now.
  The `Server` header no longer discloses the Python patch version.
- **Changed — `network_info`** still explains the `0.0.0.0` bind for LAN
  viewing, but now says the token is required and that it crosses the network
  in plain HTTP. `doctor` reports the dashboard's bind and warns beyond
  loopback.
- **Added — tests:** 18 new (222 total). The escaping tests lift the page's
  real `innerHTML` expressions out of the served HTML and run them, then check
  the produced markup for injected tags — a template that merely says `esc()`
  is not proof. Green against both `mcp<2` and `mcp>=2`.

## v8.5

A hardening release, from an audit of the whole project against HashiCorp Vault
as the reference. **Contains a breaking change** — see the operator gate below.

- **BREAKING — target registration and vault writes are operator-gated.**
  `gateway_register_mcp` takes a command and arguments and the hub spawns them,
  so any agent that could call it could run anything as the hub user; and a
  credential was usable by whatever target named it, so an agent could register
  a target pointing at a host it controlled, reference someone else's key, and
  read the secret out of the response. Both defeat the documented promise that
  agents never see the keys. `gateway_register_rest`, `gateway_register_mcp`,
  `gateway_unregister`, `gateway_toggle`, `gateway_set_credential`,
  `gateway_delete_credential`, and `gateway_generate_adapter` now require
  `operator_token`, matched against `TEAM_OPERATOR_TOKEN` in the server's
  environment. Chat, board, debate, memory, and calling existing targets are
  unchanged. **Set `TEAM_OPERATOR_TOKEN` before upgrading if you use the hub.**
- **Added — credential binding.** `gateway_set_credential` now requires
  `targets`, naming which targets may use the key; anything else is refused at
  call time and audited. `targets="*"` restores any-target use and must be said
  explicitly. Keys stored earlier keep working, and `doctor` lists the unbound
  ones.
- **Fixed — silent write loss, the whole class.** v8.3's lock fix covered one
  path; 13 tools still did `_load()` -> mutate -> `_save(state)`, an
  unserialized read-modify-write. Four processes doing 20 `save_note` calls
  each landed 60 of 80; 8 threads x 25 landed 25 of 200, and `post_message` was
  collateral. `_save` now refuses a stale snapshot (`StaleWrite`), which also
  catches any future site, and the hot paths moved to `_mutate`.
- **Fixed — credentials survived a redirect to another host.** The SSRF guard
  re-checked the destination, but stock `HTTPRedirectHandler` strips only
  `content-length` and `content-type`, so the injected `Authorization` header
  rode along — and the new host passed the check precisely because it was
  public. Auth headers are now dropped on a cross-host hop.
- **Fixed — credentials reached callers in error text.** `auth_type="query"`
  puts the secret in the URL, and `http.client` raises `InvalidURL` with the
  full selector in its message; `InvalidURL` subclasses `HTTPException`, not
  `OSError`, so urllib's wrapper never caught it. A space in a path was enough.
  Everything returned from a proxied call is now redacted.
- **Fixed — CGNAT `100.64.0.0/10` bypassed the SSRF guard** on Python 3.10/3.11,
  where it is not yet `is_private`. The check now tests `is_global` first.
- **Fixed — `webhook_notify` was an unguarded outbound sink.** An agent could
  reach cloud metadata there while the gateway refused the identical URL.
- **Fixed — `gateway_generate_adapter` wrote anywhere.** `out_path` is now
  contained to `ADAPTER_DIR`; it accepted absolute paths and `..` walks, and the
  content is spec-derived.
- **Fixed — every agent went permanently deaf once the channel rotated.**
  Message indices were positions in the retained list, and rotation renumbered
  them. A caught-up agent held `next_index == len(messages)`; once the channel
  was pinned at `MSG_ROTATE_LIMIT` that length stopped growing, so
  `total > since_index` was never true again — `read_channel` and
  `wait_for_message` returned "no new messages" forever, `@mentions` included,
  with no error. An agent that was behind had its indices reused underneath it,
  so it skipped whatever had rotated out and mislabelled the rest.
  `archived_messages` already counted the drops; nothing translated with it.
  Indices are now absolute — the nth message posted keeps index n after it
  rotates out — and a reader that fell behind is told how many it missed rather
  than being handed the wrong messages under right-looking numbers. Below
  rotation the numbers are unchanged, so indices an agent already holds stay
  valid across the upgrade.
- **Fixed — `read_receipts` under-reported anyone who passed an explicit
  index.** `acknowledge(up_to_index=-1)` stored a count while an explicit index
  stored the index itself, so the two meant different things and the comparison
  only worked for one of them. Both now store "read everything below this".
- **Added — tests:** 43 new (204 total), green against both `mcp<2` and `mcp>=2`.

## v8.4

Released as v8.4 rather than v8.2: this work was written against v8.1 but
landed after the v8.3 fix release, and a version number cannot go backwards.

- **Added — GitLab CI/CD:** `.gitlab-ci.yml` runs the full pytest suite on
  Python 3.10–3.13 in parallel plus a package job (build sdist/wheel, install
  it, smoke-test the `claude-team-mcp` entry point) with wheel artifacts kept
  for a week. Works on gitlab.com and self-hosted instances with zero config —
  just push the repo to a GitLab project.
- **Added — GitHub → GitLab mirroring:** optional
  `.github/workflows/mirror-gitlab.yml` force-pushes `main` (and tags) to a
  GitLab project on every push, so the GitLab pipeline stays in sync
  automatically. Dormant until you set the `GITLAB_MIRROR_URL` repo variable
  and `GITLAB_TOKEN` secret.
- **Added — docs:** `examples/gitlab_setup.md` walks through three ways to
  connect the project to GitLab (second remote, automatic mirroring, GitLab
  Premium pull mirroring) and documents what the pipeline runs.

## v8.3

A fix release building on v8.1. The GitLab CI/CD work landed separately, in
v8.4.

- **Fixed — the server would not start on a fresh install.** The `mcp` SDK
  released 2.0, which removed `mcp.server.fastmcp` and renamed `FastMCP` to
  `MCPServer` in `mcp.server.mcpserver`. Because the dependency was declared as
  `mcp>=1.2.0` with no upper bound, `pip install` resolved to 2.0 and the
  server raised `ModuleNotFoundError` on import. The import is now a shim that
  binds to whichever class the installed SDK provides, so **both SDK lines
  work**, and the requirement is capped at `<3` so the next rename cannot break
  installs silently. The full suite passes against `mcp<2` and `mcp>=2`.
- **Fixed — the second brain wrote to a Windows drive letter on every
  platform.** `BRAIN_DIR` defaulted to the literal `D:/mcp/second_brain`
  regardless of OS, so on Linux and macOS notes landed in a directory named
  `D:` under whatever the working directory happened to be. It now defaults to
  `~/.claude_team_brain` off Windows, matching how `TEAM_STATE_FILE` already
  behaved. `doctor` flags a leftover `D:` directory and says where to move it.
- **Added — SSRF guard on the hub.** Targets are registered from
  agent-supplied text, and the hub fetches them from its own network position
  with its stored credentials available for injection, so
  `http://169.254.169.254/` would have exposed cloud instance metadata.
  Private, loopback, and link-local destinations are now refused at
  registration, at call time (covering targets stored by earlier versions),
  when the adapter generator fetches a spec by URL, and on every redirect hop.
  Hostnames are judged against every address they resolve to. Blocked calls are
  audited. `GATEWAY_ALLOW_PRIVATE=1` and `GATEWAY_ALLOWED_HOSTS` re-open
  internal destinations deliberately.
- **Added — `doctor`.** Both an MCP tool and a `claude-team-mcp doctor` CLI
  subcommand. Reports Python and `mcp` SDK versions and which API the shim
  bound to, whether `filelock` is installed, whether each path it writes to is
  writable, vault file permissions, and the SSRF guard's mode — each with the
  fix attached. Exits non-zero when something is broken, so it can gate a setup
  script.
- **Added — CI that catches this class of break.** A weekly `schedule` run, a
  matrix leg per `mcp` SDK line pinned explicitly, and a `doctor` smoke step.
  The 1.x → 2.x rename shipped between pushes and went unnoticed precisely
  because nothing re-ran the suite in the meantime.
- **Added — tests:** 44 new, 161 total. Cover the SSRF guard (internal
  literals, IPv4-mapped IPv6, non-HTTP schemes, split-horizon DNS, redirect
  hops, both escape hatches, and each enforcement point), the `doctor` report
  across its OK/WARN/FAIL paths, and the platform-aware brain default. They
  make no network or DNS calls.

## v8.1
- **Added — test suite:** 117 pytest tests under `tests/` covering the state
  layer (atomic writes, corruption recovery, rotation, backups/restore), team
  coordination (join/chat/mentions/presence/read receipts), the task board
  (dependencies, sub-tasks, skills, auto-assign, templates, crash recovery),
  project memory + second brain, structured debate (propose → critique →
  revise → judge, votes, scoring), the security-findings lifecycle, the
  intelligence layer (similarity search, routing, conflicts, metrics,
  reports), the MCP hub/gateway (targets, vault masking, routes, rate
  limiting, audit, OpenAPI adapter generator), and concurrent multi-writer
  safety. Every test runs in an isolated temp dir — your real state files are
  never touched.
- **Added — CI:** GitHub Actions workflow (`.github/workflows/ci.yml`) runs
  the suite on every push/PR across Python 3.10–3.13 on Linux plus a Windows
  leg, and builds + smoke-tests the sdist/wheel.
- **Added — packaging:** `pyproject.toml` so the server installs as a proper
  package: `pip install git+https://github.com/shalinda-j/Claude-Team-MCP.git`
  provides a `claude-team-mcp` console command (new `main()` entry point) that
  any MCP client can register directly — no more copying files around.
- **Changed:** documented minimum Python is now 3.10 (required by the `mcp`
  SDK). README gained install-as-package instructions and a Development &
  testing section.

## v8.0
- **Added — MCP Hub / Gateway (21 tools):** the server can now act as a
  router/proxy in front of other tools. Register downstream MCP servers and REST
  APIs as targets and reach them all through one hub connection.
  - Registry & discovery: `gateway_register_rest`, `gateway_register_mcp`,
    `gateway_unregister`, `gateway_toggle`, `gateway_list_targets`,
    `gateway_describe`, `gateway_discover`, `gateway_capabilities`.
  - **Unified auth:** secrets are stored in a separate, `chmod 600` vault
    (`gateway_set_credential`, `gateway_list_credentials`,
    `gateway_delete_credential`), masked everywhere, and injected by the hub at
    call time so agents never see API keys.
  - **Routing rules:** `gateway_add_route`, `gateway_remove_route`,
    `gateway_list_routes`, `gateway_route` (priority + tag matching).
  - **Rate-limiting + audit:** per-agent/per-target limits (`gateway_set_limit`)
    and a central audit trail (`gateway_audit`, `gateway_usage`).
  - **Proxy:** `gateway_call_rest` (REST, std-lib only) and `gateway_call_tool`
    (spawns a downstream MCP server, handshakes, forwards the call).
  - **Auto-Adapter Generator:** `gateway_generate_adapter` turns an
    OpenAPI/Swagger spec (inline / file / URL) into a complete, runnable MCP
    server and auto-registers it as a hub target.
  - **Dashboard:** new `/gateway` view (targets, routes, usage, vault keys,
    audit) linked from the team dashboard.
  - Gateway state lives in its own file (`GATEWAY_FILE`) so it survives
    `reset_team`. New env vars: `GATEWAY_FILE`, `GATEWAY_VAULT_FILE`,
    `ADAPTER_DIR`, `GATEWAY_RATE_PER_MIN`, `GATEWAY_AUDIT_LIMIT`,
    `GATEWAY_CALL_TIMEOUT`, `GATEWAY_MAX_OPS`.

## v7.0
- **Added — Security audit workflow:** `report_finding`, `list_findings`,
  `get_finding`, `triage_finding`, `assign_fix`, `verify_fix`, `security_report`,
  `start_security_audit`. Security-role agents can audit the project, log
  vulnerabilities with severity, debate/triage them, assign and verify fixes, and
  produce a Markdown security report. (Defensive — finds and fixes weaknesses in
  your own project.)

## v6.5
- **Added — Reliability (Batch A):** read receipts (`acknowledge`,
  `read_receipts`), health checks (`ping`), backup/restore (`backup_now`,
  `list_backups`, `restore_backup`), crash recovery (`recover_tasks`), auto-retry
  (`safe_call`), plus automatic periodic state backups.
- **Added — Workflow (Batch B):** task dependencies, sub-tasks, priority levels,
  skill-based auto-assign (`set_skills`, `auto_assign`), reusable workflow
  templates (`save_template`, `run_template`), and debate voting (`cast_vote`,
  `vote_tally`).
- **Added — Observability (Batch C):** live web dashboard (`start_dashboard`),
  `metrics`, `timeline`, `export_report`.
- **Added — Intelligence (Batch D):** semantic `smart_search`, `suggest_route`,
  `check_conflicts`, `context_checkpoint` (token saving), `score_debate`.
- **Added — Integration (Batch E):** `network_info`, `obsidian_sync`, `git_link`,
  `suggest_worktrees`, `webhook_notify`.

## v6.1
- **Fixed:** spawned terminal windows now close reliably (title-based `taskkill`
  instead of the launcher PID that Windows Terminal discards).
- **Added:** agent availability — `set_status`, `who_is_free`, `assign_work`.
  Finished agents auto-flip to `idle` and can be re-assigned new work.
- **Improved:** debate messages now show direction explicitly
  (`@A ✗ disagrees with @B: ...`) so it's clear who is arguing with whom.

## v6.0
- **Added:** file locking (`filelock`) + atomic writes for safe concurrent access
  across multiple CLIs/IDEs (verified: 200 concurrent messages, zero lost).
- **Added:** stale-agent cleanup, adaptive polling, channel/log rotation.
- **Changed:** default state path is now a shared absolute path so multiple
  clients converge on one world.

## v5.0
- **Added:** structured debate — `start_debate`, `submit_proposal`,
  `submit_critique`, `revise_proposal`, `next_round`, `get_debate`, `judge_debate`.

## v4.0
- **Added:** auto-spawn terminals (`spawn_agent`, `list_running_agents`,
  `close_agent`) and a self-contained second brain (`brain_*`).

## v3.0
- **Added:** project memory — notes, facts, summaries, activity log.

## v2.0
- **Added:** `wait_for_message` long-polling for live conversation.

## v1.0
- Initial release: shared channel + task board.
