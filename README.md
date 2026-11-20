# devin-bridge MCP server

`devin-bridge` is a local stdio MCP server that drives local Devin sessions over ACP. It can list sessions, read transcripts, create sessions, send prompts and wait for the answer, stop or redirect a turn, and queue prompts. Any MCP client can use it: Devin sessions coordinating each other, or Claude Code.

## How it works

- Read-only views (list, status, dump) come from Devin's SQLite session DB.
- Everything that changes state goes through ACP. On Linux the bridge attaches to the persistent ACP host when an `acp-host-*.lock` file exists. Otherwise, and always on Windows, it spawns its own `devin acp` child and loads sessions into it.
- Local sessions run in-process, so **one process holds a session at a time**. The bridge cannot load a session that is open in Devin Desktop or a CLI window. It returns a `SessionLockedError` that says what to do instead.
- After a prompt finishes, the bridge force-closes the session so Desktop/CLI can open it again. This is `release`, on by default.

## Paths

| | Linux | Windows |
|---|---|---|
| Session DB | `~/.local/share/devin/cli/sessions.db` | `%APPDATA%\devin\cli\sessions.db` |
| Devin MCP config | `~/.config/devin/mcp_config.json` | `%APPDATA%\devin\mcp_config.json` |
| Prompt queue | `<data dir>/devin_bridge_queue.json` (+ `.lock`) | same |
| Transcript dumps | `<data dir>/session_dumps/` | same |
| `devin` binary | `devin` on PATH | Devin Desktop's bundled `devin.exe`, then PATH, then `%LOCALAPPDATA%\devin\cli\bin\devin.exe` |

On Windows the bridge prefers the binary bundled with Devin Desktop. Desktop hosts your sessions with it, while the standalone CLI on PATH can be several versions behind and still writes to the same `sessions.db`.

## Register it

Devin, in `mcp_config.json`:

```json
{
  "mcpServers": {
    "devin-bridge": {
      "command": "python3",
      "args": ["/path/to/devin_bridge_mcp.py"],
      "env": {"PYTHONUNBUFFERED": "1"}
    }
  }
}
```

Claude Code, for all projects (run it from cmd or Git Bash: PowerShell's `claude.ps1` swallows the `--`):

```bash
claude mcp add devin-bridge --scope user --env PYTHONIOENCODING=utf-8 --env PYTHONUNBUFFERED=1 -- "C:\path\to\python.exe" "D:\AI Tool\devinmcp\devin-bridge\devin_bridge_mcp.py"
```

On Windows the bridge needs the `cryptography` package. It reads Devin Desktop's session token from Desktop's encrypted secret store and passes it to the spawned `devin acp`. The store is protected with DPAPI, so this only works for the same Windows user. Alternatively, set `DEVIN_BRIDGE_API_KEY`.

## Settings (environment)

| Variable | Default | Effect |
|---|---|---|
| `DEVIN_BRIDGE_PERMISSION` | `reject` | How the bridge answers Devin's permission prompts: `reject`, `allow_once`, `allow_always` or `cancel`. Per call: `permission`. Sessions in `bypass` mode never ask. |
| `DEVIN_BRIDGE_RELEASE` | `1` | Force-close a session after each finished prompt so other Devin clients can open it. Per call: `release`. |
| `DEVIN_BRIDGE_PROMPT_TIMEOUT` | `180` | Seconds a tool call waits for a turn before returning `status: running`. |
| `DEVIN_BRIDGE_PROCESS_MODEL` | Devin's default | `daemon` or `in-process` for the spawned `devin acp`. |
| `DEVIN_ACP_COMMAND` | auto | The `devin` binary to use. |
| `DEVIN_BRIDGE_DEBUG` | off | Include tracebacks in tool errors. |

## Tools

- `devin_bridge_status`: devin binary and version, token source, ACP state, sessions this bridge holds, prompts in flight, recent permission answers.
- `devin_list_sessions`: lists local sessions. Each one is marked `held_by_this_bridge` or `open_elsewhere`.
- `devin_session_status`: inspects one session.
- `devin_new_session`: creates a session with `cwd`, plus optional `model`, `mode`, `prompt`, `permission` and `release`.
- `devin_send_message`: sends a prompt. With `wait=true` you get `status: done` with `response_text`, `tool_calls`, `stop_reason` and `agent_requests`. If the turn outlasts the timeout you get `status: running` with `partial_text` and a `request_id`.
- `devin_wait_request`: keeps waiting on a `request_id` and returns the whole turn's text.
- `devin_stop_session`: cancels a turn this bridge started.
- `devin_redirect_session`: stops the session, then sends a new instruction.
- `devin_resume_session` / `devin_close_session`: load a session into this bridge and hold its lock, or release it (a forced close by default).
- `devin_get_config_options`, `devin_set_model`, `devin_set_config_option`: change model, mode and other options.
- `devin_dump_session`: writes the transcript to Markdown. It covers the main chain since the last context compaction.
- `devin_queue_prompt`, `devin_queue_status`, `devin_process_queue`, `devin_queue_cancel`: the persistent prompt queue. A job whose session is locked elsewhere goes back to `queued`.
- `devin_send_slash_command`, `devin_list_slash_commands`: the slash-command front end.
- `devin_delete_session`: deletes a session. This is destructive.

## Behavior notes

- The bridge does not offer `fs/*` or `terminal/*` to the agent, so Devin uses its own file and shell tools. Before 0.3 the bridge advertised both but never answered them, so any turn that read a file or ran a command hung until the timeout. Any agent request the bridge cannot serve now gets an immediate error reply.
- Permission prompts are answered with the session's policy and listed in the result as `agent_requests`.
- Releasing a session is a forced `session/close`, because a plain close keeps it loaded and locked in this process. The forced close also ends any background shells the turn left running. Pass `release: false` to keep them.
- `devin_stop_session` only reaches turns this bridge started. Turns that Desktop or a CLI window drives have to be stopped there.
- On Windows the spawned `devin acp` sits in a job object, so it dies with the bridge. An orphaned `devin acp` would otherwise keep every session it loaded locked.
- Daemon process model: since 3000.13.4, Devin can run sessions in a shared background daemon, where several clients join one session. `DEVIN_BRIDGE_PROCESS_MODEL=daemon` makes the bridge use it. Desktop sessions only become joinable if Desktop runs in daemon mode too (`process_model` in Devin's `config.json`). If the daemon refuses to start with "exited before it listened at …\daemon\daemon.sock", a stale `daemon.sock` from an earlier crash is the likely cause.

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

## Smoke test

```bash
python -m py_compile devin_bridge_mcp.py
devin mcp list
claude mcp get devin-bridge
```
