# Claude Team MCP

> A Model Context Protocol (MCP) server that turns multiple AI coding agents — across Claude Code, Cursor, Codex CLI, Gemini CLI, or any MCP-compatible client — into a coordinated team that chats, debates, remembers, audits security, and works in parallel on the same project.

![License](https://img.shields.io/badge/license-MIT-green)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![MCP](https://img.shields.io/badge/protocol-MCP-orange)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

---

## Overview

Most AI coding assistants work one terminal at a time. **Claude Team MCP** lets several agents run side by side and behave like a real software team: a Project Manager hands out tasks, a Backend and Frontend engineer build in parallel, a QA agent reviews — and they talk to each other through a shared channel while it happens.

Beyond simple messaging, agents can **debate** a decision (propose → critique → revise → a judge decides), share a persistent **project memory**, spawn and close their own **terminal windows**, and maintain a self-contained **"second brain"** knowledge graph on disk.

Everything is coordinated through a single shared state file, so any number of clients — even different CLIs — that point at the same file join the same world.

## Why it exists

A single agent has one perspective and one context window. Real engineering work benefits from division of labor, review, and disagreement. This project provides the missing coordination layer so independent agents can:

- divide work and track it on a shared board,
- communicate in real time without manual copy-paste,
- argue toward the *right* solution instead of accepting the first idea,
- preserve decisions and knowledge across sessions (saving tokens), and
- scale up or down by opening/closing agent terminals on demand.

## Features

- **Team coordination** — a shared chat channel and task board every agent can see.
- **Live conversation** — `wait_for_message` blocks until a new message arrives, so agents converse in real time instead of polling.
- **Structured debate** — agents submit proposals, critique each other with explicit agree/disagree + reasoning, revise only on sound arguments, and a judge issues the final verdict (research-backed propose → critique → revise → judge pattern).
- **Project memory** — notes, key-value facts, progress summaries, and a timestamped activity log that persist across sessions.
- **Token optimization** — save a compact summary, clear context, and reload just the summary instead of the full history.
- **Auto-spawn terminals (Windows)** — an agent can open new agent windows and close them when work is done, with a configurable safety limit.
- **Agent availability** — finished agents flip to *idle* automatically and can be re-assigned new work.
- **Second brain** — a self-contained knowledge graph (notes, links, backlinks, categories, tags, daily journal) stored on disk.
- **Concurrency-safe** — file locking + atomic writes mean multiple clients can write at once without corrupting or losing data (verified with 200 concurrent writes).
- **Multi-CLI** — Claude Code, Cursor, Codex CLI, and Gemini CLI all interoperate when pointed at the same state file.
- **Reliability** — read receipts, health-check pings, automatic state backups + restore, and crash recovery that releases a downed agent's tasks back to the board.
- **Workflow engine** — task dependencies (auto-blocking), sub-tasks, priorities, skill-based auto-assignment, and reusable workflow templates.
- **Live dashboard** — a zero-dependency web dashboard showing the board, channel, agents, debate, and timeline, auto-refreshing in your browser.
- **Intelligence** — meaning-based search across memory, smart task routing, conflict detection, debate scoring, and token-saving context checkpoints.
- **Integration** — Obsidian export, git branch/commit linking, worktree suggestions, and Slack/Discord/Teams webhooks.
- **Security audit** — security-role agents can find, triage, fix, and verify vulnerabilities in your own project and produce a report.

## Requirements

- Python 3.8+
- [`mcp`](https://pypi.org/project/mcp/) and [`filelock`](https://pypi.org/project/filelock/)
- At least one MCP-compatible client (Claude Code, Cursor, Codex CLI, Gemini CLI, …)

```bash
pip install -r requirements.txt
```

## Installation

1. Place `team_coordinator.py` somewhere stable, e.g. `D:\mcp\team_coordinator.py` (Windows) or `~/mcp/team_coordinator.py`.
2. Install dependencies: `pip install mcp filelock`.
3. Register the server with your client (see below). For multi-client setups, point every client at the **same** `TEAM_STATE_FILE`.

### Claude Code

```bash
claude mcp add team -s user -- env TEAM_STATE_FILE=D:\mcp\shared_state.json python D:\mcp\team_coordinator.py
claude mcp list
```

### Cursor

`mcp.json`:

```json
{
  "mcpServers": {
    "team": {
      "command": "python",
      "args": ["D:\\mcp\\team_coordinator.py"],
      "env": { "TEAM_STATE_FILE": "D:\\mcp\\shared_state.json" }
    }
  }
}
```

### Codex CLI

`~/.codex/config.toml`:

```toml
[mcp_servers.team]
command = "python"
args = ["D:\\mcp\\team_coordinator.py"]
env = { TEAM_STATE_FILE = "D:\\mcp\\shared_state.json" }
```

### Gemini CLI

`~/.gemini/settings.json` (reduce `wait_for_message` timeout if the client has a short tool timeout):

```json
{
  "mcpServers": {
    "team": {
      "command": "python",
      "args": ["D:\\mcp\\team_coordinator.py"],
      "env": { "TEAM_STATE_FILE": "D:\\mcp\\shared_state.json" }
    }
  }
}
```

Ready-to-copy versions of these live in the [`examples/`](examples/) folder.

## Quick start

Open a few terminals in your project and start your agent client in each. Give each one a role:

**Terminal 1 — PM**
> Join the team as role "PM". Add tasks to the board, assign them to Backend and Frontend, then post a message telling everyone to start and wait for replies.

**Terminal 2 — Backend**
> Join as role "Backend". Wait for instructions, do your task, post updates, and mark it done when finished.

**Terminal 3 — Frontend**
> Join as role "Frontend". Wait until the backend API is ready, then build the UI and update your task.

The agents now coordinate through the shared channel and board.

### Running a debate

> PM: Start a debate on "which database should we use", with Backend and Frontend as debaters and you as judge.

> Backend / Frontend: Submit a proposal with reasoning, read the debate, and critique the other proposals — agree or disagree with solid arguments. Revise only if convinced.

> PM: Weigh the arguments and call the verdict. The decision becomes a task and is saved to memory.

## Tools

The server exposes 73 tools, grouped by purpose.

### Team coordination
`join_team`, `post_message`, `read_channel`, `wait_for_message`, `add_task`, `update_task`, `view_board`

### Agent availability
`set_status`, `who_is_free`, `assign_work`

### Structured debate
`start_debate`, `submit_proposal`, `submit_critique`, `revise_proposal`, `next_round`, `get_debate`, `judge_debate`, `cast_vote`, `vote_tally`, `score_debate`

### Project memory
`save_note`, `search_notes`, `set_fact`, `get_facts`, `save_summary`, `load_summary`, `project_log`

### Auto-spawn terminals (Windows)
`spawn_agent`, `list_running_agents`, `close_agent`

### Second brain
`brain_add`, `brain_search`, `brain_get`, `brain_link`, `brain_backlinks`, `brain_daily`, `brain_list`

### Reliability
`acknowledge`, `read_receipts`, `ping`, `backup_now`, `list_backups`, `restore_backup`, `recover_tasks`, `safe_call`

### Workflow
`set_skills`, `auto_assign`, `save_template`, `list_templates`, `run_template`

### Observability
`start_dashboard`, `stop_dashboard`, `metrics`, `timeline`, `export_report`

### Intelligence
`smart_search`, `suggest_route`, `check_conflicts`, `context_checkpoint`

### Integration
`network_info`, `obsidian_sync`, `git_link`, `suggest_worktrees`, `webhook_notify`

### Security audit
`report_finding`, `list_findings`, `get_finding`, `triage_finding`, `assign_fix`, `verify_fix`, `security_report`, `start_security_audit`

### Reset
`reset_team`

## Security audit workflow

Beyond building software, the team can audit its own project for vulnerabilities. Spin up security-role agents (SAST auditor, secret hunter, dependency scanner, config auditor), and they record findings on a shared board with severity levels, debate whether each is real, triage them, assign fixes to developer agents, verify the fixes actually hold, and produce a Markdown security report. This is purely defensive — it helps a team find and fix weaknesses in their own codebase.

```
Security-Lead: start a security audit for this project.
Auditors: scan and report_finding for each issue (with severity + location).
Team: debate which findings are real vs false positives.
Security-Lead: triage, then assign_fix to a developer agent.
Auditor: verify_fix after the fix, then security_report.
```

## Configuration

All settings are environment variables, set when you register the server.

| Variable | Default | Purpose |
| --- | --- | --- |
| `TEAM_STATE_FILE` | `D:/mcp/shared_state.json` (Windows) / `~/.claude_team_state.json` | Shared state path. **Use the same value for every client.** |
| `BRAIN_DIR` | `D:/mcp/second_brain` | Where the second brain is stored. |
| `MAX_AGENTS` | `6` | Auto-spawn safety limit. |
| `AGENT_STALE_SECONDS` | `300` | Idle time before an agent is marked offline. |
| `MSG_ROTATE_LIMIT` | `2000` | Channel size before old messages are archived out. |
| `LOG_ROTATE_LIMIT` | `2000` | Activity-log size cap. |

## How it works

The server keeps all team state in a single JSON file. Every read is direct; every write happens under a file lock and is written atomically (temp file + rename), so concurrent agents — even in different CLIs — never clobber each other. The second brain lives in its own locked file on disk and is never touched by team resets.

`wait_for_message` implements lightweight long-polling with adaptive back-off: an agent calling it blocks until a new message appears, giving real-time conversation without busy-waiting.

## Safety notes

- **Auto-spawned agents each consume tokens independently.** The `MAX_AGENTS` limit guards against runaway spawning; raise it deliberately.
- Auto-spawning launches clients with permission prompts skipped so agents can run unattended — **only use this in trusted projects.**
- To avoid edit conflicts when several agents work in one repository, give each agent its own [git worktree](https://git-scm.com/docs/git-worktree); use the MCP board for coordination and worktrees for file isolation.
- Restart your client after changing the server file so new tools load.

## Project structure

```
claude-team-mcp/
├── team_coordinator.py     # the MCP server (all 35 tools)
├── requirements.txt
├── examples/               # ready-to-copy client configs
│   ├── claude_code.md
│   ├── cursor_mcp.json
│   ├── codex_config.toml
│   └── gemini_settings.json
├── CHANGELOG.md
├── LICENSE
└── README.md
```

## Contributing

Issues and pull requests are welcome. Please keep changes backwards-compatible with existing state files where possible, and include a note in `CHANGELOG.md`.

## License

Released under the [MIT License](LICENSE).

## Author

**Shalinda Jayasinghe**
GitHub: [@shalinda-jayasinghe](https://github.com/shalinda-jayasinghe)

> Note: replace `shalinda-jayasinghe` above and in the clone URL with your actual GitHub username if it differs.
