# Changelog

All notable changes to this project are documented here.

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
