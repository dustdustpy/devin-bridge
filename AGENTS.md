# Devin local session bridge

If the `devin-bridge` MCP server is available, use it to coordinate other local Devin sessions. Always call `mcp_list_tools` for `devin-bridge` first, then call `mcp_call_tool` with the listed schemas.

## Rules

- Do not prompt a session that may have held/active work unless the user explicitly asks. Prefer `devin_queue_prompt`, then let the user or coordinator process it.
- Before steering an existing session, check `devin_session_status` or `devin_list_sessions`.
- To change direction safely, use `devin_redirect_session`; it cancels the active turn before sending the new instruction.
- For long-running work, use `devin_send_message` with `wait=false`, keep `request_id`, then use `devin_wait_request` or `devin_stop_session`. A `status: running` result (the turn outlasted `timeout_seconds`) works the same way: keep its `request_id` and call `devin_wait_request`.
- A session open in Devin Desktop or a CLI window is locked to that process. Sending to it fails with `SessionLockedError`. Do not retry in a loop: queue the prompt, or ask the user to close the session there.
- After a finished prompt the bridge force-closes the session (`release`, default on) so the user can open it in Desktop. That also ends background shells the turn left running; pass `release: false` when the session must keep them.
- Permission prompts from the target session are answered by `permission` (`reject` by default, or `allow_once`, `allow_always`, `cancel`). Check `agent_requests` in the result to see what was refused.
- `cwd` is the filesystem working directory. Sidebar folders/groups are UI-side grouping; `sidebar_folder` is only best-effort metadata and is not guaranteed to move a session in the UI.

## Common calls

List sessions:

```json
{"tool":"devin_list_sessions","arguments":{}}
```

Create a session in a folder:

```json
{"tool":"devin_new_session","arguments":{"cwd":"/home/ubuntu/sophos","sidebar_folder":"sophos","prompt":"Say hello and stop","model":"swe-2-max","mode":"bypass"}}
```

Send a prompt immediately:

```json
{"tool":"devin_send_message","arguments":{"session_id":"SESSION_ID","message":"Your instruction","wait":true}}
```

Queue prompts for one or more sessions:

```json
{"tool":"devin_queue_prompt","arguments":{"session_id":"SESSION_ID","message":"Reply only: q1"}}
{"tool":"devin_queue_status","arguments":{}}
{"tool":"devin_process_queue","arguments":{"limit":10}}
```

Cancel a queued prompt before it sends:

```json
{"tool":"devin_queue_cancel","arguments":{"queue_id":"QUEUE_ID"}}
```

Stop or redirect a session:

```json
{"tool":"devin_stop_session","arguments":{"session_id":"SESSION_ID"}}
{"tool":"devin_redirect_session","arguments":{"session_id":"SESSION_ID","instruction":"New instruction"}}
```

Dump transcript:

```json
{"tool":"devin_dump_session","arguments":{"session_id":"SESSION_ID","tail_messages":50}}
```

Delete a test session only when explicitly asked:

```json
{"tool":"devin_delete_session","arguments":{"session_id":"SESSION_ID","stop_first":true}}
```

Change model/mode:

```json
{"tool":"devin_set_model","arguments":{"session_id":"SESSION_ID","model":"swe-2-max"}}
{"tool":"devin_set_config_option","arguments":{"session_id":"SESSION_ID","config_id":"mode","value":"bypass"}}
```

## Slash-like commands

`devin_send_slash_command` supports:

```text
/ls | /sessions
/new [cwd] [prompt]
/status <session_id>
/send <session_id> <message>
/queue <session_id> <message>
/queue-status [session_id]
/runqueue [session_id|queue_id]
/cancel-queue <queue_id>
/stop <session_id>
/redirect <session_id> <instruction>
/dump <session_id> [tail]
/model <session_id> [model]
/mode <session_id> [mode]
/close <session_id>
/delete <session_id>
```

Use `as_text=true` only when intentionally sending unknown UI-only slash command text to the target agent.
