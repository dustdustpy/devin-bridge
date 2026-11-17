# devin-bridge MCP server

`devin-bridge` is a local stdio MCP server that lets Devin sessions coordinate other local Devin sessions through the authenticated ACP host.

## Files

- Server source/repo: `/home/ubuntu/devin-bridge/devin_bridge_mcp.py`
- MCP registration: `/home/ubuntu/.config/devin/mcp_config.json`
- Queue file: `/home/ubuntu/.local/share/devin/cli/devin_bridge_queue.json`
- Queue lock: `/home/ubuntu/.local/share/devin/cli/devin_bridge_queue.lock`
- Dump directory: `/home/ubuntu/.local/share/devin/cli/session_dumps/`
- Agent usage doc: `/home/ubuntu/AGENTS.md`
- Sophos workspace doc: `/home/ubuntu/sophos/AGENTS.md`

## Registration

The server is registered globally as `devin-bridge` in `mcp_config.json`:

```json
{
  "mcpServers": {
    "devin-bridge": {
      "command": "python3",
      "args": ["/home/ubuntu/devin-bridge/devin_bridge_mcp.py"],
      "env": {"PYTHONUNBUFFERED": "1"}
    }
  }
}
```

Inside a Devin session, call `mcp_list_tools` for `devin-bridge` before `mcp_call_tool`.

## Main tools

- `devin_bridge_status` — verify ACP host/session DB access.
- `devin_list_sessions` — list local sessions.
- `devin_session_status` — inspect a session.
- `devin_resume_session` — attach/resume a session.
- `devin_new_session` — create a session with `cwd`, optional `model`, `mode`, `prompt`.
- `devin_send_message` — send a prompt immediately.
- `devin_send_slash_command` — bridge-level slash commands.
- `devin_stop_session` — cancel active turn.
- `devin_redirect_session` — stop then send a new instruction.
- `devin_wait_request` — wait for `wait=false` prompt request.
- `devin_get_config_options` — inspect config options.
- `devin_set_model` — set model.
- `devin_set_config_option` — set mode/config.
- `devin_dump_session` — dump transcript to Markdown.
- `devin_list_slash_commands` — list supported commands.
- `devin_queue_prompt` — enqueue a prompt.
- `devin_queue_status` — inspect prompt queue.
- `devin_process_queue` — send queued prompts sequentially.
- `devin_queue_cancel` — cancel a queued job.
- `devin_close_session` — close/detach resumed session.
- `devin_delete_session` — delete a local session through ACP.

## Slash-like commands

```text
/ls | /sessions
/resume <session_id>
/status <session_id>
/model <session_id> [model]
/mode <session_id> [mode]
/stop <session_id>
/dump <session_id> [tail]
/send <session_id> <message>
/redirect <session_id> <instruction>
/queue <session_id> <message>
/queue-status [session_id]
/runqueue [session_id|queue_id]
/cancel-queue <queue_id>
/new [cwd] [prompt]
/close <session_id>
/delete <session_id>
```

## Queue lifecycle

Jobs are persisted in `devin_bridge_queue.json` and move through:

```text
queued -> sending -> done
                 \-> failed
queued -> cancelled
```

Enqueue does not send. Use `devin_process_queue` or `/runqueue` to send jobs.

## Important limitations

- `cwd` is controlled by ACP and is the filesystem workspace.
- Sidebar folders are UI-side grouping and are not reliably controllable through local ACP. `sidebar_folder` is best-effort metadata only.
- ACP-only sessions need at least one persisted prompt before SQLite transcript dumping works.
- Sending a prompt to a session with held work may resume old work; use `devin_redirect_session` or `devin_stop_session` first.
- `devin_delete_session` is destructive; use only for sessions intended for removal.

## Smoke test

```bash
python3 -m py_compile /home/ubuntu/devin-bridge/devin_bridge_mcp.py
devin mcp list
```
