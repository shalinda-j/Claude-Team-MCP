# Team Coordinator MCP — Complete Feature Guide (65 tools)

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
