# Team Coordinator MCP — Complete Feature Guide (94 tools)

සියලුම features කොහොමද වැඩ කරන්නේ කියලා, batch අනුව.

═══════════════════════════════════════════════════════════
CORE — Team Coordination & Debate (පෙර හදපු)
═══════════════════════════════════════════════════════════
join_team, post_message, read_channel, wait_for_message,
add_task, update_task, view_board, set_status, who_is_free, assign_work
start_debate, submit_proposal, submit_critique, revise_proposal,
next_round, get_debate, judge_debate
save_note, search_notes, set_fact, get_facts, save_summary, load_summary, project_log
spawn_agent, list_running_agents, close_agent
brain_add, brain_search, brain_get, brain_link, brain_backlinks, brain_daily, brain_list
reset_team

═══════════════════════════════════════════════════════════
BATCH A — RELIABILITY (8 tools)
═══════════════════════════════════════════════════════════
acknowledge      — messages කියෙව්වා කියලා mark (read receipt)
read_receipts    — කවුද message එක කියෙව්වද බලනවා
ping             — agents alive ද කියලා health check
backup_now       — දැන්ම state backup එකක්
list_backups     — restore කරන්න පුළුවන් backups
restore_backup   — corruption/bad reset එකකින් පස්සේ restore
recover_tasks    — crashed agent ගේ tasks ආපහු todo කරනවා
safe_call        — operation එකක් retry + escalate

Auto-backup: හැම 15 writes එකකටම automatic (BACKUP_EVERY).

═══════════════════════════════════════════════════════════
BATCH B — WORKFLOW (7 tools + enhanced add_task/update_task/board)
═══════════════════════════════════════════════════════════
add_task         — දැන් priority, depends_on, parent_id support
                   (dependency done වෙනකම් task block වෙනවා)
set_skills       — agent ගේ skills declare
auto_assign      — task එක skill-match agent ට auto-assign
save_template    — reusable workflow එකක් save
list_templates   — templates බලනවා
run_template     — workflow එකක් dependency chain විදිහට spin up
cast_vote        — debate එකේ vote කරනවා
vote_tally       — votes ගණන් කරනවා

═══════════════════════════════════════════════════════════
BATCH C — OBSERVABILITY (5 tools)
═══════════════════════════════════════════════════════════
start_dashboard  — browser එකේ live dashboard (http://localhost:8765/)
                   board+chat+agents+debate+timeline, 2s auto-refresh
stop_dashboard   — dashboard නවත්වනවා
metrics          — agents, messages, task %, decisions snapshot
timeline         — project history chronologically
export_report    — Markdown report file එකක් save

═══════════════════════════════════════════════════════════
BATCH D — INTELLIGENCE (5 tools)
═══════════════════════════════════════════════════════════
smart_search     — meaning-based search (notes + brain), keyword නෙවෙයි
suggest_route    — message/task එකට best agent කවුද කියලා suggest
check_conflicts  — agents collide වෙන්න පුළුවන් තැන් flag
context_checkpoint — token saving: summary save කරලා /clear කරන්න
score_debate     — agents ගේ argument quality score කරනවා

═══════════════════════════════════════════════════════════
BATCH E — INTEGRATION (5 tools)
═══════════════════════════════════════════════════════════
network_info     — multiple machines වල run කරන හැටි
obsidian_sync    — notes/brain → Obsidian vault (graph view එක්ක)
git_link         — task එකකට git branch/commit link
suggest_worktrees — per-agent git worktree commands generate
webhook_notify   — Slack/Discord/Teams notification

═══════════════════════════════════════════════════════════
BATCH F — MCP HUB / GATEWAY (21 tools)
═══════════════════════════════════════════════════════════
තනි MCP server එකක් router/proxy එකක් විදිහට. අනිත් MCP servers + REST APIs
මේකට "targets" විදිහට register වෙනවා; agent එක එක පාරක් hub එකට connect වෙලා
ඔක්කොටම access ගන්නවා. Keys agent ට පේන්නේ නෑ.

REGISTRY & DISCOVERY
gateway_register_rest  — REST API එකක් target එකක් විදිහට register
gateway_register_mcp   — තවත් MCP server එකක් target එකක් විදිහට register
gateway_unregister     — target එකක් අයින් කරනවා
gateway_toggle         — target එකක් enable/disable (config නැති වෙන්නේ නෑ)
gateway_list_targets   — register වෙලා තියෙන targets බලනවා
gateway_describe       — එක target එකක full config (credential masked)
gateway_discover       — MCP target එකක tools list එක ගෙනත් cache කරනවා
gateway_capabilities   — "මට මොනවද කරන්න පුළුවන්?" — හැම capability එකක්ම

UNIFIED AUTH (vault — chmod 600, agent ට key පේන්නේ නෑ)
gateway_set_credential   — secret එකක් vault එකේ store (key එකකින්)
gateway_list_credentials — vault KEYS විතරයි (values කවදාවත් නෑ)
gateway_delete_credential— credential එකක් delete

ROUTING
gateway_add_route    — "මේ pattern එක -> මේ target එක" rule එකක්
gateway_remove_route — route එකක් අයින්
gateway_list_routes  — routes priority order එකේ
gateway_route        — request එකකට හරි target එක තෝරනවා (route + tag match)

LIMITS & AUDIT
gateway_set_limit — per-agent/target rate limit (per minute; 0 = unlimited)
gateway_audit     — මධ්‍යගත audit trail (කවුද මොකද කවදද status latency)
gateway_usage     — target අනුව calls/errors/limits

PROXY & GENERATOR
gateway_call_rest      — REST call එකක් hub හරහා (key inject + rate-limit + log)
gateway_call_tool      — MCP tool call එකක් downstream server එකකට proxy
gateway_generate_adapter — KILLER: OpenAPI/Swagger spec -> සම්පූර්ණ MCP server
                           file එකක් auto-generate + target විදිහට register

Dashboard: /gateway view එක (team dashboard එකෙන් link). targets, routes,
usage, vault keys, audit live පෙන්වනවා.

═══════════════════════════════════════════════════════════
NEW ENV VARS (optional)
═══════════════════════════════════════════════════════════
TEAM_BACKUP_DIR   — backups location (default: <state>/team_backups)
BACKUP_KEEP=10    — keep how many backups
BACKUP_EVERY=15   — backup every N writes
HEARTBEAT_STALE=120 — ping stale threshold (s)
DASHBOARD_PORT=8765 — dashboard port
DASHBOARD_HOST=127.0.0.1 — set 0.0.0.0 for LAN access
OBSIDIAN_VAULT    — vault path for obsidian_sync
WEBHOOK_URL       — default webhook for webhook_notify
GATEWAY_FILE      — hub registry/routes/audit (default: <state>/gateway.json)
GATEWAY_VAULT_FILE— secret vault (chmod 600; default: <state>/gateway_vault.json)
ADAPTER_DIR       — generated adapters location (default: <state>/adapters)
GATEWAY_RATE_PER_MIN=60 — default per-agent/target call limit
GATEWAY_AUDIT_LIMIT=2000 — audit trail cap
GATEWAY_CALL_TIMEOUT=30 — proxied REST call timeout (s)
GATEWAY_MAX_OPS=150 — max ops generated from one OpenAPI spec

═══════════════════════════════════════════════════════════
TYPICAL ADVANCED WORKFLOW
═══════════════════════════════════════════════════════════
1. PM: set_skills for each agent, start_dashboard
2. PM: save_template once, then run_template to spin up a chained workflow
3. Agents: auto-assigned by skill; dependencies auto-block until ready
4. On big decisions: start_debate -> proposals -> critiques -> cast_vote
   -> score_debate -> judge_debate
5. Long sessions: context_checkpoint -> /clear -> load_summary (saves tokens)
6. Watch progress live on the dashboard; check_conflicts if agents overlap
7. If an agent crashes: ping -> recover_tasks -> reassign
8. Done: export_report, obsidian_sync, webhook_notify "project complete"

All state is concurrency-safe (file locking + atomic writes), backwards
compatible with older state files, and works across Claude Code, Cursor,
Codex, and Gemini when they share TEAM_STATE_FILE.
