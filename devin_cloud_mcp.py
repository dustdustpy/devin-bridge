#!/usr/bin/env python3
"""devin-cloud: MCP server for Devin Cloud sessions through the Devin REST API v3.

Accounts live in a JSON file (never in this repo), default ~/.devin-bridge/cloud_accounts.json,
or the path in $DEVIN_CLOUD_CONFIG:

    {"accounts": [{"name": "cloud", "org_id": "org-...", "token": "cog_..."}]}

Each account is a personal access token (or service-user key) plus the organization whose
sessions it should drive. Tokens are only ever sent to the API and never appear in tool output.
Standard library only.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

SERVER_NAME = "devin-cloud"
SERVER_VERSION = "0.1.0"
MCP_PROTOCOL_VERSION = "2024-11-05"
API_BASE = os.environ.get("DEVIN_API_BASE", "https://api.devin.ai").rstrip("/")
CONFIG_PATH = os.path.expanduser(os.environ.get("DEVIN_CLOUD_CONFIG", "~/.devin-bridge/cloud_accounts.json"))
MAX_OUTPUT = int(os.environ.get("DEVIN_CLOUD_MAX_OUTPUT", "60000"))
POLL_SECONDS = 8.0
# status_detail values that mean Devin is done with its turn and waiting on a person
IDLE_DETAILS = {"waiting_for_user", "waiting_for_approval", "finished"}
STOPPED_STATUSES = {"exit", "error", "suspended"}


class CloudError(RuntimeError):
    def __init__(self, message: str, status: Optional[int] = None, problem: Any = None):
        super().__init__(message)
        self.status = status
        self.problem = problem


# ---------------------------------------------------------------------------
# accounts and HTTP


def _load_accounts() -> List[Dict[str, Any]]:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise CloudError(f"account file not found: {CONFIG_PATH}")
    accounts = data.get("accounts") if isinstance(data, dict) else None
    if not isinstance(accounts, list) or not accounts:
        raise CloudError(f"no accounts in {CONFIG_PATH}")
    for acct in accounts:
        if not acct.get("token") or not acct.get("org_id"):
            raise CloudError(f"account {acct.get('name')!r} needs both token and org_id")
    return accounts


def _account(name: Optional[str]) -> Dict[str, Any]:
    accounts = _load_accounts()
    if not name:
        if len(accounts) == 1:
            return accounts[0]
        raise CloudError("several accounts are configured; pass account: " + ", ".join(a.get("name", a["org_id"]) for a in accounts))
    key = str(name).strip().lower()
    for acct in accounts:
        if key in (str(acct.get("name", "")).lower(), str(acct["org_id"]).lower()):
            return acct
    raise CloudError(f"unknown account {name!r}; configured: " + ", ".join(a.get("name", a["org_id"]) for a in accounts))


def _request(acct: Dict[str, Any], method: str, path: str, query: Optional[Dict[str, Any]] = None,
             body: Any = None, timeout: float = 60) -> Any:
    clean_query = {k: v for k, v in (query or {}).items() if v not in (None, "", [])}
    url = API_BASE + path + ("?" + urllib.parse.urlencode(clean_query, doseq=True) if clean_query else "")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {acct['token']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"{SERVER_NAME}-mcp/{SERVER_VERSION}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            problem = json.loads(raw)
        except Exception:
            problem = {"detail": raw[:500]}
        title = problem.get("title") if isinstance(problem, dict) else None
        detail = problem.get("detail") if isinstance(problem, dict) else None
        raise CloudError(f"HTTP {e.code} {title or e.reason}" + (f": {detail}" if detail else ""), e.code, problem)
    except urllib.error.URLError as e:
        raise CloudError(f"cannot reach {API_BASE}: {e.reason}")


def _org_path(acct: Dict[str, Any], suffix: str = "") -> str:
    return f"/v3/organizations/{urllib.parse.quote(acct['org_id'])}{suffix}"


def _resolve(args: Dict[str, Any]):
    """Find (account, devin_id) from a session id, an app.devin.ai URL or an exact session title.

    Without an account, every configured org is searched.
    """
    import re

    raw = str(args.get("session_id") or "").strip()
    if not raw:
        raise CloudError("session_id is required")
    if "/sessions/" in raw:
        raw = raw.rstrip("/").rsplit("/", 1)[-1]
    accounts = [_account(args["account"])] if args.get("account") else _load_accounts()
    if re.fullmatch(r"(devin-)?[0-9a-f]{32}", raw.lower()):
        devin_id = _devin_id(raw)
        if len(accounts) == 1:
            return accounts[0], devin_id
        for acct in accounts:
            try:
                _request(acct, "GET", _org_path(acct, f"/sessions/{devin_id}"))
                return acct, devin_id
            except CloudError as e:
                if e.status not in (403, 404):
                    raise
        raise CloudError(f"session {raw} was not found in any configured org")
    matches = []
    for acct in accounts:
        try:
            page = _request(acct, "GET", _org_path(acct, "/sessions"), {"first": 200})
        except CloudError:
            continue
        for s in page.get("items") or []:
            if str(s.get("title") or "").strip().lower() == raw.lower():
                matches.append((acct, s))
    if len(matches) == 1:
        return matches[0][0], _devin_id(matches[0][1]["session_id"])
    if not matches:
        raise CloudError(f"no session titled {raw!r} in: " + ", ".join(str(a.get("name")) for a in accounts))
    raise CloudError(f"title {raw!r} is ambiguous: " + ", ".join(f"{a.get('name')}:{s['session_id']}" for a, s in matches))


def _devin_id(session_id: str) -> str:
    sid = str(session_id or "").strip()
    if not sid:
        raise CloudError("session_id is required")
    if "/sessions/" in sid:  # a pasted app.devin.ai URL
        sid = sid.rstrip("/").rsplit("/", 1)[-1]
    return sid if sid.startswith("devin-") else f"devin-{sid}"


def _when(ts: Any) -> Optional[str]:
    if not isinstance(ts, (int, float)) or ts <= 0:
        return None
    seconds = ts / 1000.0 if ts > 1e12 else float(ts)
    return _dt.datetime.fromtimestamp(seconds, _dt.timezone.utc).isoformat(timespec="seconds")


def _session_view(s: Dict[str, Any]) -> Dict[str, Any]:
    keys = ("session_id", "title", "status", "status_detail", "devin_mode", "origin", "url", "acus_consumed",
            "is_archived", "tags", "pull_requests", "parent_session_id", "category", "structured_output")
    out = {k: s.get(k) for k in keys if s.get(k) not in (None, [], {})}
    out["created"] = _when(s.get("created_at"))
    out["updated"] = _when(s.get("updated_at"))
    return out


def _all_messages(acct: Dict[str, Any], devin_id: str, limit_pages: int = 50) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    after = None
    for _ in range(limit_pages):
        page = _request(acct, "GET", _org_path(acct, f"/sessions/{devin_id}/messages"), {"first": 200, "after": after})
        items.extend(page.get("items") or [])
        if not page.get("has_next_page") or not page.get("end_cursor"):
            break
        after = page["end_cursor"]
    return items


def _message_view(m: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    text = str(m.get("message") or "")
    if max_chars and len(text) > max_chars:
        text = text[:max_chars] + f" …[+{len(text) - max_chars} chars]"
    return {"source": m.get("source"), "at": _when(m.get("created_at")), "user": m.get("username"), "message": text}


def _wait_for_turn(acct: Dict[str, Any], devin_id: str, seen: set, wait_seconds: float, max_chars: int) -> Dict[str, Any]:
    """Poll until Devin stops working (or wait_seconds pass); return the messages that are new."""
    deadline = time.monotonic() + wait_seconds
    session: Dict[str, Any] = {}
    time.sleep(min(POLL_SECONDS, wait_seconds))
    while True:
        session = _request(acct, "GET", _org_path(acct, f"/sessions/{devin_id}"))
        idle = session.get("status") in STOPPED_STATUSES or session.get("status_detail") in IDLE_DETAILS
        if idle or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    new = [m for m in _all_messages(acct, devin_id) if m.get("event_id") not in seen]
    return {
        "finished_waiting": bool(session.get("status") in STOPPED_STATUSES or session.get("status_detail") in IDLE_DETAILS),
        "session": _session_view(session),
        "new_messages": [_message_view(m, max_chars) for m in new],
    }


# ---------------------------------------------------------------------------
# tools


def tool_accounts(args: Dict[str, Any]) -> Any:
    out = []
    for acct in _load_accounts():
        row: Dict[str, Any] = {"account": acct.get("name"), "org_id": acct["org_id"]}
        if args.get("verify", True):
            try:
                me = _request(acct, "GET", "/v3/self")
                row.update({k: me.get(k) for k in ("principal_type", "user_name", "api_key_name") if me.get(k)})
                token_org = me.get("devin_sessions_org_id") or me.get("org_id")
                row["token_org_id"] = token_org
                row["token_ok"] = True
                if token_org and token_org != acct["org_id"]:
                    row["warning"] = ("this token's sessions org differs from the configured org_id; "
                                      "session calls will likely get 403")
            except CloudError as e:
                row.update({"token_ok": False, "error": str(e)})
        out.append(row)
    return {"config": CONFIG_PATH, "accounts": out}


def _list_one(acct: Dict[str, Any], args: Dict[str, Any]) -> Dict[str, Any]:
    query: Dict[str, Any] = {"first": max(1, min(int(args.get("first") or 20), 200)), "after": args.get("after")}
    if args.get("is_archived") is not None:
        query["is_archived"] = "true" if args["is_archived"] else "false"
    if args.get("tags"):
        query["tags"] = list(args["tags"])
    page = _request(acct, "GET", _org_path(acct, "/sessions"), query)
    items = [_session_view(s) for s in page.get("items") or []]
    return {"account": acct.get("name"), "org_id": acct["org_id"], "count": len(items), "total": page.get("total"),
            "has_next_page": page.get("has_next_page"), "end_cursor": page.get("end_cursor"), "sessions": items}


def tool_list_sessions(args: Dict[str, Any]) -> Any:
    if args.get("account"):
        return _list_one(_account(args["account"]), args)
    results = []
    for acct in _load_accounts():
        try:
            results.append(_list_one(acct, args))
        except CloudError as e:
            results.append({"account": acct.get("name"), "org_id": acct["org_id"], "error": str(e)})
    return {"accounts": results}


def tool_get_session(args: Dict[str, Any]) -> Any:
    acct, devin_id = _resolve(args)
    s = _request(acct, "GET", _org_path(acct, f"/sessions/{devin_id}"))
    return {"account": acct.get("name"), "session": _session_view(s)}


def tool_messages(args: Dict[str, Any]) -> Any:
    acct, devin_id = _resolve(args)
    msgs = _all_messages(acct, devin_id)
    last = int(args.get("last") or 30)
    max_chars = int(args.get("max_chars") or 4000)
    shown = msgs[-last:] if last > 0 else msgs
    return {"account": acct.get("name"), "session_id": devin_id, "total_messages": len(msgs), "shown": len(shown),
            "messages": [_message_view(m, max_chars) for m in shown]}


def tool_send_message(args: Dict[str, Any]) -> Any:
    acct, devin_id = _resolve(args)
    text = str(args.get("message") or "").strip()
    if not text:
        raise CloudError("message is required")
    wait = max(0.0, min(float(args.get("wait_seconds") or 0), 1800.0))
    seen = {m.get("event_id") for m in _all_messages(acct, devin_id)} if wait else set()
    s = _request(acct, "POST", _org_path(acct, f"/sessions/{devin_id}/messages"), body={"message": text})
    out: Dict[str, Any] = {"account": acct.get("name"), "sent": True, "session": _session_view(s)}
    if wait:
        out.update(_wait_for_turn(acct, devin_id, seen, wait, int(args.get("max_chars") or 4000)))
    return out


def tool_create_session(args: Dict[str, Any]) -> Any:
    acct = _account(args.get("account"))
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise CloudError("prompt is required")
    body: Dict[str, Any] = {"prompt": prompt}
    for key in ("title", "tags", "devin_mode", "max_acu_limit", "repos", "platform", "playbook_id", "knowledge_ids"):
        if args.get(key) not in (None, "", []):
            body[key] = args[key]
    s = _request(acct, "POST", _org_path(acct, "/sessions"), body=body)
    out: Dict[str, Any] = {"account": acct.get("name"), "created": True, "session": _session_view(s)}
    wait = max(0.0, min(float(args.get("wait_seconds") or 0), 1800.0))
    if wait and s.get("session_id"):
        out.update(_wait_for_turn(acct, _devin_id(s["session_id"]), set(), wait, int(args.get("max_chars") or 4000)))
    return out


def tool_wait(args: Dict[str, Any]) -> Any:
    acct, devin_id = _resolve(args)
    msgs = _all_messages(acct, devin_id)
    keep_last = int(args.get("include_last") or 0)
    seen = {m.get("event_id") for m in (msgs[:-keep_last] if keep_last else msgs)}
    wait = max(1.0, min(float(args.get("wait_seconds") or 300), 1800.0))
    return {"account": acct.get("name"), **_wait_for_turn(acct, devin_id, seen, wait, int(args.get("max_chars") or 4000))}


def tool_archive(args: Dict[str, Any]) -> Any:
    acct, devin_id = _resolve(args)
    action = "unarchive" if args.get("unarchive") else "archive"
    s = _request(acct, "POST", _org_path(acct, f"/sessions/{devin_id}/{action}"))
    return {"account": acct.get("name"), action: True, "session": _session_view(s)}


def tool_terminate(args: Dict[str, Any]) -> Any:
    if args.get("confirm") is not True:
        raise CloudError("terminate is irreversible: pass confirm=true once the user has agreed")
    acct, devin_id = _resolve(args)
    s = _request(acct, "DELETE", _org_path(acct, f"/sessions/{devin_id}"),
                 {"archive": "true" if args.get("archive") else None})
    return {"account": acct.get("name"), "terminated": True, "session": _session_view(s)}


ACCOUNT_PROP = {"type": "string", "description": "Account name from the config (e.g. cloud, control) or its org_id. "
                                                "Optional for session tools: every org is searched."}
SESSION_PROP = {"type": "string", "description": "Session id (bare or devin-…), its app.devin.ai URL, or its exact title (e.g. codex1)"}

TOOLS: List[Dict[str, Any]] = [
    {"name": "devin_cloud_accounts",
     "description": "List the configured Devin Cloud accounts (orgs) and check each token with GET /v3/self.",
     "inputSchema": {"type": "object", "properties": {"verify": {"type": "boolean", "default": True}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "devin_cloud_list_sessions",
     "description": "List Devin Cloud sessions, newest first. Without account it lists every configured org.",
     "inputSchema": {"type": "object", "properties": {
         "account": ACCOUNT_PROP, "first": {"type": "integer", "default": 20, "maximum": 200},
         "after": {"type": "string", "description": "end_cursor from a previous page"},
         "is_archived": {"type": "boolean"}, "tags": {"type": "array", "items": {"type": "string"}}}},
     "annotations": {"readOnlyHint": True}},
    {"name": "devin_cloud_get_session",
     "description": "Get one Devin Cloud session: status, status_detail (working / waiting_for_user / finished…), ACUs, PRs, URL.",
     "inputSchema": {"type": "object", "properties": {"account": ACCOUNT_PROP, "session_id": SESSION_PROP},
                     "required": ["session_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "devin_cloud_messages",
     "description": "Read a Devin Cloud session's conversation (user and Devin messages), last N by default.",
     "inputSchema": {"type": "object", "properties": {
         "account": ACCOUNT_PROP, "session_id": SESSION_PROP, "last": {"type": "integer", "default": 30},
         "max_chars": {"type": "integer", "default": 4000, "description": "Truncate each message to this many chars (0 = no limit)"}},
         "required": ["session_id"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "devin_cloud_send_message",
     "description": "Send a message to a Devin Cloud session (resumes a sleeping one). With wait_seconds it polls until "
                    "Devin stops working and returns its new messages. Show the user the exact text before sending.",
     "inputSchema": {"type": "object", "properties": {
         "account": ACCOUNT_PROP, "session_id": SESSION_PROP, "message": {"type": "string"},
         "wait_seconds": {"type": "number", "default": 0, "maximum": 1800}, "max_chars": {"type": "integer", "default": 4000}},
         "required": ["session_id", "message"]}},
    {"name": "devin_cloud_create_session",
     "description": "Start a new Devin Cloud session in an org (uses ACUs). Optional title, tags, devin_mode, max_acu_limit, repos.",
     "inputSchema": {"type": "object", "properties": {
         "account": ACCOUNT_PROP, "prompt": {"type": "string"}, "title": {"type": "string"},
         "tags": {"type": "array", "items": {"type": "string"}},
         "devin_mode": {"type": "string", "enum": ["normal", "fast", "fusion", "lite", "ultra", "fast-ultra",
                                                    "swe-2-medium", "swe-2-high", "swe-2-max"]},
         "max_acu_limit": {"type": "integer"}, "repos": {"type": "array", "items": {"type": "string"}},
         "platform": {"type": "string"}, "wait_seconds": {"type": "number", "default": 0, "maximum": 1800}},
         "required": ["account", "prompt"]}},
    {"name": "devin_cloud_wait",
     "description": "Wait until a Devin Cloud session stops working (waiting_for_user / finished / stopped) and return the messages that arrived meanwhile.",
     "inputSchema": {"type": "object", "properties": {
         "account": ACCOUNT_PROP, "session_id": SESSION_PROP, "wait_seconds": {"type": "number", "default": 300, "maximum": 1800},
         "include_last": {"type": "integer", "default": 0, "description": "Also return the last N existing messages"}},
         "required": ["session_id"]}},
    {"name": "devin_cloud_archive_session",
     "description": "Archive a Devin Cloud session (sleeps it; reversible with unarchive=true).",
     "inputSchema": {"type": "object", "properties": {
         "account": ACCOUNT_PROP, "session_id": SESSION_PROP, "unarchive": {"type": "boolean", "default": False}},
         "required": ["session_id"]}},
    {"name": "devin_cloud_terminate_session",
     "description": "Terminate a Devin Cloud session for good (cannot be resumed). Requires confirm=true after the user agrees.",
     "inputSchema": {"type": "object", "properties": {
         "account": ACCOUNT_PROP, "session_id": SESSION_PROP, "confirm": {"type": "boolean"},
         "archive": {"type": "boolean", "default": False}},
         "required": ["session_id", "confirm"]}},
]

TOOL_FUNCS = {
    "devin_cloud_accounts": tool_accounts,
    "devin_cloud_list_sessions": tool_list_sessions,
    "devin_cloud_get_session": tool_get_session,
    "devin_cloud_messages": tool_messages,
    "devin_cloud_send_message": tool_send_message,
    "devin_cloud_create_session": tool_create_session,
    "devin_cloud_wait": tool_wait,
    "devin_cloud_archive_session": tool_archive,
    "devin_cloud_terminate_session": tool_terminate,
}


# ---------------------------------------------------------------------------
# MCP stdio


def _text(value: Any) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
    return text if len(text) <= MAX_OUTPUT else text[:MAX_OUTPUT] + f"\n… truncated {len(text) - MAX_OUTPUT} chars"


def _call_tool(params: Dict[str, Any]) -> Dict[str, Any]:
    fn = TOOL_FUNCS.get(params.get("name"))
    if not fn:
        return {"content": [{"type": "text", "text": f"Unknown tool: {params.get('name')}"}], "isError": True}
    try:
        return {"content": [{"type": "text", "text": _text(fn(params.get("arguments") or {}))}], "isError": False}
    except CloudError as e:
        detail = {"error": str(e), "status": e.status}
        return {"content": [{"type": "text", "text": _text(detail)}], "isError": True}
    except Exception as e:  # noqa: BLE001
        detail = {"error": f"{type(e).__name__}: {e}"}
        if os.environ.get("DEVIN_CLOUD_DEBUG"):
            detail["traceback"] = traceback.format_exc(limit=8)
        return {"content": [{"type": "text", "text": _text(detail)}], "isError": True}


def _handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if method == "initialize":
        requested = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": req_id, "result": {
            "protocolVersion": requested if isinstance(requested, str) else MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Devin Cloud sessions via the Devin API v3. Start with devin_cloud_list_sessions; read with "
                            "devin_cloud_messages; send with devin_cloud_send_message (show the user the text first).",
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": req_id, "result": _call_tool(params)}
    if method in ("prompts/list", "resources/list", "resources/templates/list"):
        key = {"prompts/list": "prompts", "resources/list": "resources", "resources/templates/list": "resourceTemplates"}[method]
        return {"jsonrpc": "2.0", "id": req_id, "result": {key: []}}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main() -> int:
    out = getattr(sys.stdout, "buffer", sys.stdout)
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except Exception:
            continue
        if not (msg.get("method") and "id" in msg):
            continue  # notifications and stray responses
        resp = _handle(msg)
        if resp is not None:
            out.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
            out.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
