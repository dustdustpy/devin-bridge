#!/usr/bin/env python3
"""Devin local-session bridge MCP server.

This server lets a Devin session inspect and safely control other local Devin
sessions through the already-running Devin ACP host. It deliberately keeps two
paths separate:

* SQLite is used only for read-only transcript/log inspection.
* ACP is used for state-changing operations such as resume/prompt/cancel.
"""

from __future__ import annotations

import datetime as _dt
import glob
import json
import os
from pathlib import Path
import queue as _queue
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

if os.name == "posix":
    import fcntl  # type: ignore
else:
    fcntl = None  # type: ignore
if os.name == "nt":
    import msvcrt  # type: ignore
else:
    msvcrt = None  # type: ignore

HOME = Path.home()
IS_WINDOWS = os.name == "nt"


def _devin_data_dir() -> Path:
    override = os.environ.get("DEVIN_DATA_DIR")
    if override:
        return Path(os.path.expanduser(override))
    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA") or str(HOME / "AppData" / "Roaming")
        return Path(appdata) / "devin" / "cli"
    return HOME / ".local/share/devin/cli"


DATA_DIR = _devin_data_dir()
DB_PATH = DATA_DIR / "sessions.db"
DUMP_DIR = DATA_DIR / "session_dumps"
QUEUE_PATH = DATA_DIR / "devin_bridge_queue.json"
QUEUE_LOCK_PATH = DATA_DIR / "devin_bridge_queue.lock"
SCRIPT_PATH = Path(__file__).resolve()
SERVER_NAME = "devin-bridge"
SERVER_VERSION = "0.2.0"
MCP_PROTOCOL_VERSION = "2024-11-05"

LOCK_PATTERNS = (
    str(HOME / ".devin-server/cli/acp-host-*.lock"),
    str(HOME / ".devin/cli/acp-host-*.lock"),
    str(HOME / ".devin-server/cli/*acp*lock"),
    str(DATA_DIR / "acp-host-*.lock"),
)

MAX_TOOL_OUTPUT_CHARS = int(os.environ.get("DEVIN_BRIDGE_MAX_OUTPUT", "60000"))
DEFAULT_PROMPT_TIMEOUT = float(os.environ.get("DEVIN_BRIDGE_PROMPT_TIMEOUT", "180"))


class BridgeError(RuntimeError):
    pass


class AcpError(BridgeError):
    def __init__(self, message: str, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


def _utc_ts() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _truncate(text: str, limit: int = MAX_TOOL_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n… truncated {len(text) - limit} chars …"


def _json_text(value: Any) -> str:
    return _truncate(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _tool_result(value: Any, is_error: bool = False) -> Dict[str, Any]:
    text = value if isinstance(value, str) else _json_text(value)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _error_result(exc: BaseException) -> Dict[str, Any]:
    detail = {
        "error": str(exc) or exc.__class__.__name__,
        "type": exc.__class__.__name__,
    }
    if isinstance(exc, AcpError):
        detail["code"] = exc.code
        detail["data"] = exc.data
    if os.environ.get("DEVIN_BRIDGE_DEBUG"):
        detail["traceback"] = traceback.format_exc(limit=10)
    return _tool_result(detail, is_error=True)


def _first_existing_dir(*paths: Optional[str]) -> str:
    for p in paths:
        if p:
            pp = Path(os.path.expanduser(p))
            if pp.is_dir():
                return str(pp.resolve())
    return str(HOME)


def _sqlite_ro() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise BridgeError(f"Devin session DB not found: {DB_PATH}")
    uri = f"file:{DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _table_cols(con: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]


def _session_row(session_id: str) -> Optional[Dict[str, Any]]:
    with _sqlite_ro() as con:
        cols = set(_table_cols(con, "sessions"))
        wanted = [
            "id",
            "title",
            "working_directory",
            "backend_type",
            "model",
            "agent_mode",
            "status",
            "created_at",
            "updated_at",
            "last_activity_at",
            "main_chain_id",
        ]
        fields = [c for c in wanted if c in cols]
        row = con.execute(
            f"SELECT {', '.join(fields)} FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None


def resolve_session_id(session_id: str) -> str:
    session_id = (session_id or "").strip()
    if not session_id:
        raise BridgeError("session_id is required")
    with _sqlite_ro() as con:
        exact = con.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
        if exact:
            return exact[0]
        rows = con.execute(
            "SELECT id FROM sessions WHERE id LIKE ? OR title LIKE ? LIMIT 20",
            (session_id + "%", "%" + session_id + "%"),
        ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if rows:
        raise BridgeError(
            "session_id is ambiguous; matches: " + ", ".join(r[0] for r in rows[:20])
        )
    raise BridgeError(f"session not found: {session_id}")


def _acp_session_id(session_id: str) -> str:
    """Resolve DB-backed ids, but still allow a freshly-created ACP session id."""
    session_id = (session_id or "").strip()
    if not session_id:
        raise BridgeError("session_id is required")
    try:
        return resolve_session_id(session_id)
    except BridgeError:
        return session_id


def list_sessions_db(limit: int = 50, query: Optional[str] = None) -> List[Dict[str, Any]]:
    with _sqlite_ro() as con:
        cols = set(_table_cols(con, "sessions"))
        order = "last_activity_at" if "last_activity_at" in cols else (
            "updated_at" if "updated_at" in cols else "created_at"
        )
        where = ""
        params: List[Any] = []
        if query:
            clauses = []
            if "id" in cols:
                clauses.append("id LIKE ?")
                params.append(f"%{query}%")
            if "title" in cols:
                clauses.append("title LIKE ?")
                params.append(f"%{query}%")
            if "working_directory" in cols:
                clauses.append("working_directory LIKE ?")
                params.append(f"%{query}%")
            where = "WHERE " + " OR ".join(clauses)
        sql = f"SELECT * FROM sessions {where} ORDER BY {order} DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        rows = [dict(r) for r in con.execute(sql, params)]
    keep = [
        "id",
        "title",
        "working_directory",
        "backend_type",
        "model",
        "agent_mode",
        "status",
        "created_at",
        "updated_at",
        "last_activity_at",
        "main_chain_id",
    ]
    return [{k: r.get(k) for k in keep if k in r} for r in rows]


def _normalize_content(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in ("text", "output_text") and item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("text"):
                    parts.append(str(item["text"]))
                elif item.get("type"):
                    parts.append(f"[{item.get('type')}]")
                else:
                    parts.append(json.dumps(item, ensure_ascii=False))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False)


def _parse_message(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw)
    except Exception:
        return {"role": "unknown", "content": str(raw)}
    if isinstance(value, dict):
        return value
    return {"role": "unknown", "content": _normalize_content(value)}


def _message_chain(session_id: str) -> List[Dict[str, Any]]:
    session_id = resolve_session_id(session_id)
    with _sqlite_ro() as con:
        srow = con.execute(
            "SELECT main_chain_id FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not srow or not srow[0]:
            raise BridgeError(f"session has no main_chain_id: {session_id}")
        cols = set(_table_cols(con, "message_nodes"))
        wanted = [
            "node_id",
            "parent_node_id",
            "chat_message",
            "created_at",
            "updated_at",
            "event_type",
            "ordinal",
        ]
        fields = [c for c in wanted if c in cols]
        chain: List[Dict[str, Any]] = []
        seen = set()
        cur = srow[0]
        while cur is not None:
            if cur in seen:
                raise BridgeError(f"cycle in message chain at node {cur}")
            seen.add(cur)
            row = con.execute(
                f"SELECT {', '.join(fields)} FROM message_nodes WHERE session_id=? AND node_id=?",
                (session_id, cur),
            ).fetchone()
            if not row:
                break
            item = dict(row)
            chain.append(item)
            cur = item.get("parent_node_id")
        chain.reverse()
        return chain


def dump_session_log(
    session_id: str,
    output_path: Optional[str] = None,
    tail: int = 0,
    include_tools: bool = True,
    include_system: bool = False,
) -> Dict[str, Any]:
    sid = resolve_session_id(session_id)
    meta = _session_row(sid) or {"id": sid}
    chain = _message_chain(sid)
    if tail and tail > 0:
        chain = chain[-tail:]

    lines = [
        f"# Devin session dump: {sid}",
        "",
        f"- title: {meta.get('title')}",
        f"- working_directory: {meta.get('working_directory')}",
        f"- model: {meta.get('model')}",
        f"- agent_mode: {meta.get('agent_mode')}",
        f"- backend_type: {meta.get('backend_type')}",
        f"- last_activity_at: {meta.get('last_activity_at')}",
        f"- dumped_at: {_utc_ts()}",
        "",
    ]
    included = 0
    for idx, node in enumerate(chain, 1):
        msg = _parse_message(node.get("chat_message"))
        role = str(msg.get("role") or msg.get("type") or "unknown")
        if role == "system" and not include_system:
            continue
        if role == "tool" and not include_tools:
            continue
        content = _normalize_content(msg.get("content"))
        tool_calls = msg.get("tool_calls") or msg.get("toolCalls") or []
        if not content and not tool_calls:
            continue
        included += 1
        lines.extend(
            [
                f"## node {idx} `{node.get('node_id')}` — {role}",
                "",
            ]
        )
        if node.get("created_at"):
            lines.append(f"- created_at: {node.get('created_at')}")
        if content:
            lines.extend(["", content, ""])
        if include_tools and tool_calls:
            lines.append("### tool_calls")
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    lines.append(f"- `{tc}`")
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                name = tc.get("name") or fn.get("name") or tc.get("tool_name") or "tool"
                args = tc.get("arguments") or fn.get("arguments") or tc.get("input") or ""
                lines.append(f"- `{name}` `{str(args)[:2000]}`")
            lines.append("")
        lines.append("")

    if not output_path:
        stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        DUMP_DIR.mkdir(parents=True, exist_ok=True)
        output_path = str(DUMP_DIR / f"{sid}-{stamp}.md")
    out = Path(os.path.expanduser(output_path)).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    return {
        "session_id": sid,
        "title": meta.get("title"),
        "output_path": str(out),
        "nodes_total": len(chain),
        "messages_included": included,
        "bytes": out.stat().st_size,
    }


def _queue_default() -> Dict[str, Any]:
    return {"version": 1, "jobs": []}


def _lock_file(fp) -> None:
    if fcntl is not None:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
    elif msvcrt is not None:
        fp.seek(0, os.SEEK_END)
        if fp.tell() == 0:
            fp.write("\0")
            fp.flush()
        fp.seek(0)
        msvcrt.locking(fp.fileno(), msvcrt.LK_LOCK, 1)


def _unlock_file(fp) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:
            fp.seek(0)
            msvcrt.locking(fp.fileno(), msvcrt.LK_UNLCK, 1)
    except Exception:
        pass


def _queue_mutate(mutator):
    QUEUE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with QUEUE_LOCK_PATH.open("a+", encoding="utf-8") as lock_file:
        _lock_file(lock_file)
        try:
            if QUEUE_PATH.exists():
                try:
                    data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
                except Exception:
                    data = _queue_default()
            else:
                data = _queue_default()
            if not isinstance(data.get("jobs"), list):
                data["jobs"] = []
            result = mutator(data)
            tmp = QUEUE_PATH.with_suffix(QUEUE_PATH.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, QUEUE_PATH)
            return result
        finally:
            _unlock_file(lock_file)


def _queue_read() -> Dict[str, Any]:
    if not QUEUE_PATH.exists():
        return _queue_default()
    try:
        data = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _queue_default()
    if not isinstance(data.get("jobs"), list):
        data["jobs"] = []
    return data


def _queue_add_job(
    session_id: str,
    message: str,
    cwd: Optional[str] = None,
    from_session_id: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    sid = _acp_session_id(session_id)
    job = {
        "id": f"q-{int(time.time())}-{uuid.uuid4().hex[:8]}",
        "session_id": sid,
        "message": message,
        "cwd": cwd,
        "from_session_id": from_session_id,
        "timeout_seconds": timeout_seconds,
        "status": "queued",
        "created_at": _utc_ts(),
        "updated_at": _utc_ts(),
        "request_id": None,
        "result": None,
        "error": None,
    }

    def add(data):
        data["jobs"].append(job)
        return job

    return _queue_mutate(add)


def _queue_claim(session_id: Optional[str] = None, queue_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    sid_filter = _acp_session_id(session_id) if session_id else None

    def claim(data):
        for job in data["jobs"]:
            if job.get("status") != "queued":
                continue
            if queue_id and job.get("id") != queue_id:
                continue
            if sid_filter and job.get("session_id") != sid_filter:
                continue
            job["status"] = "sending"
            job["updated_at"] = _utc_ts()
            return dict(job)
        return None

    return _queue_mutate(claim)


def _queue_update_job(queue_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    def update(data):
        for job in data["jobs"]:
            if job.get("id") == queue_id:
                job.update(fields)
                job["updated_at"] = _utc_ts()
                return dict(job)
        return None

    return _queue_mutate(update)


def _queue_cancel_job(queue_id: str) -> Optional[Dict[str, Any]]:
    def cancel(data):
        for job in data["jobs"]:
            if job.get("id") != queue_id:
                continue
            if job.get("status") == "queued":
                job["status"] = "cancelled"
                job["updated_at"] = _utc_ts()
            return dict(job)
        return None

    return _queue_mutate(cancel)


def _queue_summary(jobs: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for job in jobs:
        status = str(job.get("status") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts


def _load_lock() -> Tuple[Path, Dict[str, Any]]:
    candidates: List[Path] = []
    for pattern in LOCK_PATTERNS:
        candidates.extend(Path(p) for p in glob.glob(os.path.expanduser(pattern)))
    candidates = sorted(set(candidates), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        if data.get("token") and data.get("port"):
            return path, data
    raise BridgeError(
        "No Devin ACP host lockfile found. Start Devin CLI/Desktop first; "
        f"looked for: {', '.join(LOCK_PATTERNS)}"
    )


def _bridge_mcp_server() -> Dict[str, Any]:
    return {
        "name": SERVER_NAME,
        "command": sys.executable or "python3",
        "args": [str(SCRIPT_PATH)],
        "env": [],
    }


def _find_devin_command() -> str:
    override = os.environ.get("DEVIN_ACP_COMMAND") or os.environ.get("DEVIN_CLI")
    if override:
        return override
    found = shutil.which("devin") or shutil.which("devin.exe")
    if found:
        return found
    if IS_WINDOWS:
        localapp = os.environ.get("LOCALAPPDATA") or str(HOME / "AppData" / "Local")
        candidates = [
            Path(localapp) / "Programs/Devin/resources/app/extensions/windsurf/devin/bin/devin.exe",
            Path(localapp) / "Programs/Windsurf/resources/app/extensions/windsurf/devin/bin/devin.exe",
        ]
        for cand in candidates:
            if cand.is_file():
                return str(cand)
    raise BridgeError(
        "devin CLI binary not found; set DEVIN_ACP_COMMAND to the devin executable path"
    )


_TOKEN_CACHE: Optional[str] = None
_TOKEN_RESOLVED = False


def _desktop_secret_db() -> Optional[Path]:
    if not IS_WINDOWS:
        return None
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    for name in ("Devin", "devin", "Windsurf", "windsurf", "Devin Insiders"):
        cand = Path(appdata) / name / "User" / "globalStorage" / "state.vscdb"
        if cand.is_file():
            return cand
    return None


def _load_api_token() -> Optional[str]:
    """Resolve a Devin session token for spawned `devin acp` children.

    Order: explicit env vars, then (Windows only) the Devin Desktop secret
    store, which holds Electron safeStorage values that are AES-GCM encrypted
    with the DPAPI-protected os_crypt key from "Local State". This only works
    for the same Windows user that owns the Desktop profile.
    """
    global _TOKEN_CACHE, _TOKEN_RESOLVED
    if _TOKEN_RESOLVED:
        return _TOKEN_CACHE
    _TOKEN_RESOLVED = True
    for var in ("DEVIN_BRIDGE_API_KEY", "DEVIN_API_KEY", "WINDSURF_API_KEY", "CODEIUM_API_KEY"):
        value = os.environ.get(var)
        if value:
            _TOKEN_CACHE = value.strip()
            return _TOKEN_CACHE
    if not IS_WINDOWS:
        return None
    try:
        import base64
        import ctypes
        import ctypes.wintypes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        db = _desktop_secret_db()
        if not db:
            return None

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        def unprotect(data: bytes) -> Optional[bytes]:
            blob_in = DATA_BLOB(
                len(data),
                ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)),
            )
            blob_out = DATA_BLOB()
            if not ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            ):
                return None
            try:
                return ctypes.string_at(blob_out.pbData, blob_out.cbData)
            finally:
                ctypes.windll.kernel32.LocalFree(blob_out.pbData)

        local_state = db.parents[2] / "Local State"
        ls = json.loads(local_state.read_text(encoding="utf-8"))
        enc_key = base64.b64decode(ls["os_crypt"]["encrypted_key"])
        key = unprotect(enc_key[5:])
        if not key:
            return None
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
        try:
            rows = con.execute(
                "SELECT key, value FROM ItemTable WHERE key LIKE 'secret://%'"
            ).fetchall()
        finally:
            con.close()
        for skey, val in rows:
            if "sessions" not in str(skey):
                continue
            if isinstance(val, bytes):
                val = val.decode("utf-8", "replace")
            try:
                raw = bytes(json.loads(val)["data"])
            except Exception:
                continue
            if raw[:3] != b"v10":
                continue
            try:
                sessions = json.loads(AESGCM(key).decrypt(raw[3:15], raw[15:], None))
            except Exception:
                continue
            if isinstance(sessions, list):
                for sess in sessions:
                    token = (sess or {}).get("accessToken")
                    if token:
                        _TOKEN_CACHE = token
                        return _TOKEN_CACHE
    except Exception:
        return None
    return None


def _token_source() -> str:
    for var in ("DEVIN_BRIDGE_API_KEY", "DEVIN_API_KEY", "WINDSURF_API_KEY", "CODEIUM_API_KEY"):
        if os.environ.get(var):
            return f"env:{var}"
    return "desktop-secret" if _load_api_token() else "none"


class AcpClient:
    def __init__(self) -> None:
        self.sock: Optional[socket.socket] = None
        self.file: Any = None
        self.proc: Optional[subprocess.Popen] = None
        self.transport: Optional[str] = None
        self.devin_cmd: Optional[str] = None
        self.next_id = 0
        self.pending: Dict[Any, Dict[str, Any]] = {}
        self.completed: Dict[Any, Dict[str, Any]] = {}
        self.updates: List[Dict[str, Any]] = []
        self.resumed: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.lock_path: Optional[Path] = None
        self.lock_data: Dict[str, Any] = {}
        self.initialized = False
        self._rx: "_queue.Queue[Any]" = _queue.Queue()
        self._eof = object()
        self.stderr_tail: List[str] = []

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        try:
            if self.file:
                self.file.close()
        except Exception:
            pass
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None
        self.sock = None
        self.file = None
        self.initialized = False
        while True:
            try:
                self._rx.get_nowait()
            except _queue.Empty:
                break

    def _reader_loop(self, stream: Any) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    self._rx.put(json.loads(line.decode("utf-8")))
                except Exception:
                    continue
        except Exception:
            pass
        self._rx.put(self._eof)

    def _stderr_loop(self, stream: Any) -> None:
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                try:
                    text = line.decode("utf-8", "replace").rstrip()
                except Exception:
                    continue
                self.stderr_tail.append(text)
                if len(self.stderr_tail) > 200:
                    self.stderr_tail = self.stderr_tail[-200:]
        except Exception:
            pass

    def _spawn_acp(self, attach_cwd: str) -> None:
        cmd = _find_devin_command()
        env = dict(os.environ)
        env.pop("ACP_BACKEND", None)
        if not env.get("WINDSURF_API_KEY"):
            token = _load_api_token()
            if token:
                env["WINDSURF_API_KEY"] = token
        cwd = attach_cwd if os.path.isdir(attach_cwd) else str(HOME)
        kwargs: Dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self.proc = subprocess.Popen(
            [cmd, "acp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            **kwargs,
        )
        self.file = self.proc.stdin
        self.transport = "stdio"
        self.devin_cmd = cmd
        threading.Thread(target=self._reader_loop, args=(self.proc.stdout,), daemon=True).start()
        threading.Thread(target=self._stderr_loop, args=(self.proc.stderr,), daemon=True).start()

    def _connect(self) -> None:
        if self.initialized and (self.sock or self.file):
            return
        self.close()
        attach_cwd = os.environ.get("DEVIN_ACP_ATTACH_CWD") or str(HOME)

        # Preferred transport on Linux: the persistent ACP host socket.
        try:
            self.lock_path, self.lock_data = _load_lock()
        except BridgeError:
            self.lock_path, self.lock_data = None, {}

        use_tcp = bool(self.lock_data)
        if use_tcp:
            try:
                port = int(self.lock_data["port"])
                self.sock = socket.create_connection(("127.0.0.1", port), timeout=15)
                self.sock.settimeout(None)
                self.file = self.sock.makefile("wb", buffering=0)
                self.transport = "tcp"
                threading.Thread(
                    target=self._reader_loop,
                    args=(self.sock.makefile("rb"),),
                    daemon=True,
                ).start()
            except Exception:
                use_tcp = False
                self.close()

        if not use_tcp:
            self._spawn_acp(attach_cwd)

        if self.transport == "tcp":
            handshake = {
                "token": self.lock_data["token"],
                "protocol": int(self.lock_data.get("protocol") or 5),
                "agent": {"id": "devin-cli", "cwd": attach_cwd},
            }
            self._write_raw(handshake)
            hs = self._read_raw(timeout=15)
            if not hs or not hs.get("ok"):
                raise BridgeError(f"ACP handshake rejected: {hs}")

        self.next_id += 1
        init_id = self.next_id
        self._write_raw(
            {
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": True, "writeTextFile": True},
                        "terminal": True,
                        "session": {"configOptions": {"boolean": {}}},
                    },
                    "clientInfo": {"name": "devin-bridge-mcp", "version": SERVER_VERSION},
                },
            }
        )
        while True:
            msg = self._read_raw(timeout=30)
            if not msg:
                continue
            if msg.get("id") == init_id:
                if "error" in msg:
                    err = msg["error"]
                    raise AcpError(err.get("message") or "ACP initialize failed", err.get("code"), err.get("data"))
                break
            self._handle_side_message(msg)
        self.initialized = True

    # -- wire io -----------------------------------------------------------

    def _write_raw(self, obj: Dict[str, Any]) -> None:
        if not self.file:
            raise BridgeError("ACP stream is not connected")
        try:
            self.file.write((json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
            self.file.flush()
        except (BrokenPipeError, OSError) as e:
            self.initialized = False
            raise BridgeError(f"ACP stream write failed: {e}") from e

    def _read_raw(self, timeout: float = 30, raise_on_timeout: bool = True) -> Optional[Dict[str, Any]]:
        if not (self.sock or self.file):
            raise BridgeError("ACP stream is not connected")
        try:
            msg = self._rx.get(timeout=timeout)
        except _queue.Empty:
            if raise_on_timeout:
                raise TimeoutError(f"timed out waiting for ACP response ({timeout}s)")
            return None
        if msg is self._eof:
            self.initialized = False
            detail = f"; last stderr: {self.stderr_tail[-1]}" if self.stderr_tail else ""
            raise EOFError(f"ACP stream closed{detail}")
        return msg

    def _handle_side_message(self, msg: Dict[str, Any]) -> None:
        if "id" in msg and ("result" in msg or "error" in msg):
            self.completed[msg["id"]] = msg
            return
        if msg.get("method"):
            item = {"received_at": _utc_ts(), "message": msg}
            self.updates.append(item)
            if len(self.updates) > 1000:
                self.updates = self.updates[-1000:]

    def send_request(self, method: str, params: Optional[Dict[str, Any]] = None) -> int:
        self._connect()
        with self.lock:
            self.next_id += 1
            req_id = self.next_id
            self.pending[req_id] = {"method": method, "params": params or {}, "sent_at": time.time()}
            self._write_raw({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}})
            return req_id

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._connect()
        with self.lock:
            self._write_raw({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def wait(self, req_id: int, timeout: float = 30, updates_for_session: Optional[str] = None) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        collected: List[Dict[str, Any]] = []
        while True:
            if req_id in self.completed:
                msg = self.completed.pop(req_id)
                self.pending.pop(req_id, None)
                if "error" in msg:
                    err = msg.get("error") or {}
                    raise AcpError(err.get("message") or "ACP request failed", err.get("code"), err.get("data"))
                result = msg.get("result")
                if updates_for_session and collected:
                    if isinstance(result, dict):
                        result = dict(result)
                        result["_bridge_updates"] = collected
                return result if isinstance(result, dict) else {"result": result}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"timed out waiting for ACP request id {req_id} ({timeout}s)")
            msg = self._read_raw(timeout=min(remaining, 30), raise_on_timeout=False)
            if not msg:
                continue
            if msg.get("method") and updates_for_session:
                params = msg.get("params") or {}
                if params.get("sessionId") == updates_for_session:
                    collected.append(msg)
            self._handle_side_message(msg)

    def call(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: float = 30,
        updates_for_session: Optional[str] = None,
    ) -> Dict[str, Any]:
        req_id = self.send_request(method, params)
        return self.wait(req_id, timeout=timeout, updates_for_session=updates_for_session)


ACP = AcpClient()


def _session_from_acp(session_id: str) -> Optional[Dict[str, Any]]:
    try:
        result = ACP.call("session/list", {}, timeout=30)
        sessions = result.get("sessions") if isinstance(result, dict) else result
        if not isinstance(sessions, list):
            return None
        for sess in sessions:
            if isinstance(sess, dict) and sess.get("sessionId") == session_id:
                return sess
    except Exception:
        return None
    return None


_RESUME_METHODS = ["session/load", "session/resume"] if IS_WINDOWS else ["session/resume", "session/load"]


def _resume(session_id: str, cwd: Optional[str] = None, force: bool = False) -> Dict[str, Any]:
    sid = _acp_session_id(session_id)
    if not force and sid in ACP.resumed:
        return ACP.resumed[sid]
    meta = _session_row(sid) or {}
    selected_cwd = _first_existing_dir(cwd, meta.get("working_directory"), str(HOME))
    last_err: Optional[Exception] = None
    for idx, method in enumerate(list(_RESUME_METHODS)):
        try:
            result = ACP.call(
                method,
                {"sessionId": sid, "cwd": selected_cwd, "mcpServers": [_bridge_mcp_server()]},
                timeout=60,
            )
            if idx != 0:
                _RESUME_METHODS.insert(0, _RESUME_METHODS.pop(_RESUME_METHODS.index(method)))
            ACP.resumed[sid] = result
            return result
        except Exception as e:
            last_err = e
    raise last_err or BridgeError("session resume failed")


def _flatten_select_options(options: Any) -> List[Dict[str, Any]]:
    flat: List[Dict[str, Any]] = []
    if not isinstance(options, list):
        return flat
    for opt in options:
        if not isinstance(opt, dict):
            continue
        if isinstance(opt.get("options"), list):
            flat.extend(_flatten_select_options(opt["options"]))
        elif "value" in opt:
            flat.append(opt)
    return flat


def _config_summary(config_options: Any) -> List[Dict[str, Any]]:
    if not isinstance(config_options, list):
        return []
    summary = []
    for opt in config_options:
        if not isinstance(opt, dict):
            continue
        flat = _flatten_select_options(opt.get("options"))
        summary.append(
            {
                "id": opt.get("id"),
                "name": opt.get("name"),
                "category": opt.get("category"),
                "type": opt.get("type"),
                "currentValue": opt.get("currentValue"),
                "choices": len(flat) if flat else None,
            }
        )
    return summary


def _find_config_option(config_options: Any, config_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(config_options, list):
        return None
    for opt in config_options:
        if isinstance(opt, dict) and opt.get("id") == config_id:
            return opt
    for opt in config_options:
        if isinstance(opt, dict) and opt.get("category") == config_id:
            return opt
    return None


def _resolve_select_value(option: Dict[str, Any], requested: Any) -> str:
    if not isinstance(requested, str):
        raise BridgeError(f"value for {option.get('id')} must be a string")
    value = requested.strip()
    flat = _flatten_select_options(option.get("options"))
    for opt in flat:
        if opt.get("value") == value:
            return value
    aliases = {
        "swe": "swe-2-high",
        "swe-2": "swe-2-high",
        "swe2": "swe-2-high",
        "swe-2-max": "swe-2-high",
        "max": "swe-2-high",
        "adaptive": "adaptive",
        "opus": "claude-opus-5-5-medium",
        "sonnet": "claude-sonnet-5-5-medium",
        "gpt": "gpt-6-astra-medium",
        "astra": "gpt-6-astra-medium",
        "gemini": "gemini-4-carbon-medium",
        "kimi": "kimi-k3-1-pro-max",
        "glm": "glm-5-3-max",
    }
    lowered = value.lower()
    if lowered in aliases:
        for opt in flat:
            if opt.get("value") == aliases[lowered]:
                return aliases[lowered]
    exact_names = [
        opt for opt in flat
        if str(opt.get("name", "")).lower() == lowered or str(opt.get("group", "")).lower() == lowered
    ]
    if len(exact_names) == 1:
        return str(exact_names[0]["value"])
    fuzzy = [
        opt for opt in flat
        if lowered in str(opt.get("value", "")).lower() or lowered in str(opt.get("name", "")).lower()
    ]
    if len(fuzzy) == 1:
        return str(fuzzy[0]["value"])
    if fuzzy:
        candidates = ", ".join(str(o.get("value")) for o in fuzzy[:20])
        raise BridgeError(f"ambiguous {option.get('id')} value '{requested}' — candidates: {candidates}")
    choices = ", ".join(str(o.get("value")) for o in flat[:40])
    raise BridgeError(f"invalid {option.get('id')} value '{requested}'. Choices include: {choices}")


def _set_config(session_id: str, config_id: str, value: Any, cwd: Optional[str] = None) -> Dict[str, Any]:
    resumed = _resume(session_id, cwd=cwd)
    options = resumed.get("configOptions", [])
    option = _find_config_option(options, config_id)
    if not option:
        ids = [o.get("id") for o in options if isinstance(o, dict)]
        raise BridgeError(f"config option '{config_id}' not found; available: {ids}")
    actual_value = value
    if option.get("type") == "select" or option.get("options"):
        actual_value = _resolve_select_value(option, value)
    params = {"sessionId": _acp_session_id(session_id), "configId": option["id"], "value": actual_value}
    if option.get("type") == "boolean":
        if isinstance(actual_value, str):
            actual_value = actual_value.lower() in ("1", "true", "yes", "on")
        params["type"] = "boolean"
        params["value"] = bool(actual_value)
    result = ACP.call("session/set_config_option", params, timeout=30)
    sid = _acp_session_id(session_id)
    if sid in ACP.resumed and isinstance(result.get("configOptions"), list):
        ACP.resumed[sid] = dict(ACP.resumed[sid])
        ACP.resumed[sid]["configOptions"] = result["configOptions"]
    return {
        "session_id": sid,
        "config_id": option["id"],
        "value": actual_value,
        "config_summary": _config_summary(result.get("configOptions")),
    }


def _extract_agent_chunks(messages: Iterable[Dict[str, Any]], session_id: str) -> str:
    chunks: List[str] = []
    for item in messages:
        msg = item.get("message", item)
        if msg.get("method") != "session/update":
            continue
        params = msg.get("params") or {}
        if params.get("sessionId") != session_id:
            continue
        update = params.get("update") or params
        kind = update.get("sessionUpdate") or update.get("type")
        if kind != "agent_message_chunk":
            continue
        content = update.get("content")
        if isinstance(content, dict):
            chunks.append(str(content.get("text") or ""))
        elif isinstance(content, list):
            chunks.append(_normalize_content(content))
        elif isinstance(content, str):
            chunks.append(content)
    return "".join(chunks)


def _send_prompt(
    session_id: str,
    text: str,
    wait: bool = True,
    timeout: float = DEFAULT_PROMPT_TIMEOUT,
    cwd: Optional[str] = None,
) -> Dict[str, Any]:
    if not text:
        raise BridgeError("message/prompt text is required")
    sid = _acp_session_id(session_id)
    resume_result = _resume(sid, cwd=cwd)
    req_id = ACP.send_request(
        "session/prompt",
        {"sessionId": sid, "prompt": [{"type": "text", "text": text}]},
    )
    base: Dict[str, Any] = {"session_id": sid, "request_id": req_id, "sent": True, "wait": bool(wait)}
    if not wait:
        return base
    result = ACP.wait(req_id, timeout=timeout, updates_for_session=sid)
    updates = result.pop("_bridge_updates", []) if isinstance(result, dict) else []
    base.update(
        {
            "result": result,
            "response_text": _extract_agent_chunks(updates, sid),
            "updates_seen": len(updates),
        }
    )
    return base


def _wait_seconds(value: Any, default: float = 30.0) -> float:
    try:
        return max(0.1, min(float(value), 3600.0))
    except Exception:
        return default


# ---------------------------------------------------------------------------
# Tool implementations


def tool_bridge_status(args: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "ok": True,
        "platform": os.name,
        "server": {"name": SERVER_NAME, "version": SERVER_VERSION, "script": str(SCRIPT_PATH)},
        "data_dir": str(DATA_DIR),
        "db_path": str(DB_PATH),
        "db_exists": DB_PATH.exists(),
        "token_source": _token_source(),
        "transport": ACP.transport,
    }
    if ACP.lock_path:
        out["acp_lock"] = str(ACP.lock_path)
        out["acp_host_pid"] = ACP.lock_data.get("pid")
        out["protocol"] = ACP.lock_data.get("protocol")
    if ACP.devin_cmd:
        out["devin_command"] = ACP.devin_cmd
    try:
        out["devin_command_resolved"] = _find_devin_command()
    except Exception as e:
        out["devin_command_error"] = str(e)
    try:
        ACP._connect()
        out["acp_initialized"] = ACP.initialized
        out["acp_transport"] = ACP.transport
    except Exception as e:
        out["ok"] = False
        out["acp_error"] = str(e)
        if ACP.stderr_tail:
            out["acp_stderr_tail"] = ACP.stderr_tail[-10:]
    return out


def tool_list_sessions(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(args.get("limit") or 50)
    query = args.get("query")
    source = args.get("source") or "auto"
    if source in ("auto", "acp"):
        try:
            result = ACP.call("session/list", {}, timeout=30)
            sessions = result.get("sessions") if isinstance(result, dict) else result
            if isinstance(sessions, list):
                if query:
                    q = str(query).lower()
                    sessions = [
                        s for s in sessions
                        if q in json.dumps(s, ensure_ascii=False).lower()
                    ]
                return {"source": "acp", "sessions": sessions[:limit], "count": min(len(sessions), limit)}
        except Exception as e:
            if source == "acp":
                raise
            acp_error = str(e)
        else:
            acp_error = None
    sessions = list_sessions_db(limit=limit, query=query)
    out = {"source": "db", "sessions": sessions, "count": len(sessions)}
    if 'acp_error' in locals() and acp_error:
        out["acp_error"] = acp_error
    return out


def tool_session_status(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = _acp_session_id(str(args.get("session_id") or ""))
    meta = _session_row(sid) or {"id": sid}
    acp = _session_from_acp(sid)
    out: Dict[str, Any] = {"session_id": sid, "db": meta, "acp": acp}
    if args.get("include_config"):
        resumed = _resume(sid, cwd=args.get("cwd"))
        out["configOptions"] = resumed.get("configOptions", [])
        out["resume_meta"] = resumed.get("_meta", {})
    return out


def tool_dump_session(args: Dict[str, Any]) -> Dict[str, Any]:
    return dump_session_log(
        str(args.get("session_id") or ""),
        output_path=args.get("output_path"),
        tail=int(args.get("tail") or 0),
        include_tools=bool(args.get("include_tools", True)),
        include_system=bool(args.get("include_system", False)),
    )


def tool_resume_session(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = _acp_session_id(str(args.get("session_id") or ""))
    result = _resume(sid, cwd=args.get("cwd"))
    meta = dict(result)
    if "_meta" in meta and isinstance(meta["_meta"], dict):
        meta["_meta"] = {k: v for k, v in meta["_meta"].items() if "historySkeleton" not in k}
    return {"session_id": sid, "resumed": True, "result": meta}


def tool_new_session(args: Dict[str, Any]) -> Dict[str, Any]:
    cwd = _first_existing_dir(args.get("cwd"), str(HOME))
    params = {"cwd": cwd, "mcpServers": [_bridge_mcp_server()]}
    meta: Dict[str, Any] = {}
    if args.get("title"):
        meta["cognition.ai/sessionTitle"] = str(args["title"])
    sidebar_folder = args.get("sidebar_folder") or args.get("folder")
    if sidebar_folder:
        folder = str(sidebar_folder)
        org_id = str(args.get("org_id") or "local")
        meta.update(
            {
                "cognition.ai/folder": folder,
                "cognition.ai/folderName": folder,
                "cognition.ai/sessionGroupIds": [folder],
                "cognition.ai/sidebarFoldersByOrg": {org_id: [folder]},
            }
        )
    if meta:
        params["_meta"] = meta
    result = ACP.call("session/new", params, timeout=60)
    sid = result.get("sessionId") or result.get("id")
    if not sid:
        raise BridgeError(f"session/new did not return sessionId: {result}")
    ACP.resumed[sid] = result
    compact = {k: v for k, v in result.items() if k not in ("configOptions", "_meta")}
    compact["config_summary"] = _config_summary(result.get("configOptions"))
    out: Dict[str, Any] = {"session_id": sid, "cwd": cwd, "created": True, "result": compact}
    if args.get("model"):
        out["model"] = _set_config(sid, "model", args["model"], cwd=cwd)
    if args.get("mode"):
        out["mode"] = _set_config(sid, "mode", args["mode"], cwd=cwd)
    thinking = args.get("thinking") or args.get("thought_level")
    if thinking:
        out["thinking"] = _set_config(sid, "thought_level", thinking, cwd=cwd)
    if args.get("prompt"):
        prompt = str(args["prompt"])
        wait = bool(args.get("wait", False))
        timeout = _wait_seconds(args.get("timeout_seconds"), DEFAULT_PROMPT_TIMEOUT)
        out["prompt"] = _send_prompt(sid, prompt, wait=wait, timeout=timeout, cwd=cwd)
    return out


def tool_send_message(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = _acp_session_id(str(args.get("session_id") or ""))
    msg = str(args.get("message") or "")
    from_sid = args.get("from_session_id")
    if from_sid:
        msg = f"[devin-bridge from {from_sid}] {msg}"
    return _send_prompt(
        sid,
        msg,
        wait=bool(args.get("wait", True)),
        timeout=_wait_seconds(args.get("timeout_seconds"), DEFAULT_PROMPT_TIMEOUT),
        cwd=args.get("cwd"),
    )


def tool_slash_command(args: Dict[str, Any]) -> Dict[str, Any]:
    command = str(args.get("command") or "").strip()
    if not command:
        raise BridgeError("command is required")
    if not command.startswith("/"):
        command = "/" + command
    try:
        parts = shlex.split(command)
    except ValueError as e:
        raise BridgeError(f"invalid slash command quoting: {e}") from e
    if not parts:
        raise BridgeError("command is required")
    name = parts[0].lower()
    rest = parts[1:]
    sid = str(args.get("session_id") or "").strip()
    cwd = args.get("cwd")

    if name in ("/help", "/commands"):
        return tool_list_slash_commands({"session_id": sid, "cwd": cwd})
    if name in ("/ls", "/sessions"):
        return {"handled": True, "command": name, "result": tool_list_sessions({"query": " ".join(rest) or None})}
    if name == "/resume":
        target = sid or (rest[0] if rest else "")
        if not target:
            raise BridgeError("/resume requires session_id or an argument")
        return {"handled": True, "command": name, "result": tool_resume_session({"session_id": target, "cwd": cwd})}
    if name == "/status":
        target = sid or (rest[0] if rest else "")
        if not target:
            raise BridgeError("/status requires session_id or an argument")
        return {"handled": True, "command": name, "result": tool_session_status({"session_id": target, "cwd": cwd})}
    if name in ("/model", "/mode"):
        target = sid
        if not target:
            raise BridgeError(f"{name} requires session_id")
        if not rest:
            resumed = _resume(target, cwd=cwd)
            option = _find_config_option(resumed.get("configOptions"), name.lstrip("/"))
            return {
                "handled": True,
                "command": name,
                "session_id": _acp_session_id(target),
                "current": option.get("currentValue") if option else None,
                "config_summary": _config_summary(resumed.get("configOptions")),
            }
        return {
            "handled": True,
            "command": name,
            "result": _set_config(target, name.lstrip("/"), rest[0], cwd=cwd),
        }
    if name in ("/stop", "/cancel"):
        target = sid or (rest[0] if rest else "")
        if not target:
            raise BridgeError(f"{name} requires session_id or an argument")
        return {"handled": True, "command": name, "result": tool_stop_session({"session_id": target})}
    if name == "/dump":
        target = sid or (rest[0] if rest else "")
        tail = (int(rest[0]) if rest else 0) if sid else (int(rest[1]) if len(rest) > 1 else 0)
        if not target:
            raise BridgeError("/dump requires session_id or an argument")
        return {
            "handled": True,
            "command": name,
            "result": tool_dump_session({"session_id": target, "tail": tail}),
        }
    if name == "/send":
        if sid:
            target, text = sid, " ".join(rest)
        else:
            if len(rest) < 2:
                raise BridgeError("/send requires <session_id> <message>")
            target, text = rest[0], " ".join(rest[1:])
        return {
            "handled": True,
            "command": name,
            "result": _send_prompt(
                target,
                text,
                wait=bool(args.get("wait", True)),
                timeout=_wait_seconds(args.get("timeout_seconds"), DEFAULT_PROMPT_TIMEOUT),
                cwd=cwd,
            ),
        }
    if name == "/redirect":
        if sid:
            target, instruction = sid, " ".join(rest)
        else:
            if len(rest) < 2:
                raise BridgeError("/redirect requires <session_id> <instruction>")
            target, instruction = rest[0], " ".join(rest[1:])
        return {
            "handled": True,
            "command": name,
            "result": tool_redirect_session(
                {
                    "session_id": target,
                    "instruction": instruction,
                    "wait": args.get("wait", False),
                    "timeout_seconds": args.get("timeout_seconds", DEFAULT_PROMPT_TIMEOUT),
                    "cwd": cwd,
                }
            ),
        }
    if name in ("/queue", "/enqueue"):
        if not rest:
            return {"handled": True, "command": name, "result": tool_queue_status({"session_id": sid or None})}
        if sid:
            target, text = sid, " ".join(rest)
        else:
            if len(rest) < 2:
                raise BridgeError(f"{name} requires <session_id> <message>, or no args for queue status")
            target, text = rest[0], " ".join(rest[1:])
        return {
            "handled": True,
            "command": name,
            "result": tool_queue_prompt({"session_id": target, "message": text, "cwd": cwd}),
        }
    if name == "/queue-status":
        return {"handled": True, "command": name, "result": tool_queue_status({"session_id": sid or (rest[0] if rest else None)})}
    if name == "/runqueue":
        opts: Dict[str, Any] = {"session_id": sid or None}
        if rest:
            if rest[0].startswith("q-"):
                opts["queue_id"] = rest[0]
            else:
                opts["session_id"] = rest[0]
        return {"handled": True, "command": name, "result": tool_process_queue(opts)}
    if name == "/cancel-queue":
        queue_id = rest[0] if rest else str(args.get("queue_id") or "")
        if not queue_id:
            raise BridgeError("/cancel-queue requires a queue_id")
        return {"handled": True, "command": name, "result": tool_queue_cancel({"queue_id": queue_id})}
    if name == "/new":
        new_cwd = rest[0] if rest else str(HOME)
        prompt = " ".join(rest[1:]) if len(rest) > 1 else str(args.get("prompt") or "")
        return {
            "handled": True,
            "command": name,
            "result": tool_new_session({"cwd": new_cwd, "prompt": prompt, "wait": args.get("wait", False)}),
        }
    if name == "/close":
        target = sid or (rest[0] if rest else "")
        if not target:
            raise BridgeError("/close requires session_id or an argument")
        return {
            "handled": True,
            "command": name,
            "result": tool_close_session({"session_id": target, "stop_first": args.get("stop_first", False)}),
        }
    if name == "/delete":
        target = sid or (rest[0] if rest else "")
        if not target:
            raise BridgeError("/delete requires session_id or an argument")
        return {
            "handled": True,
            "command": name,
            "result": tool_delete_session({"session_id": target, "stop_first": args.get("stop_first", False)}),
        }

    if args.get("as_text"):
        return _send_prompt(
            sid,
            command,
            wait=bool(args.get("wait", True)),
            timeout=_wait_seconds(args.get("timeout_seconds"), DEFAULT_PROMPT_TIMEOUT),
            cwd=cwd,
        )
    return {
        "handled": False,
        "command": name,
        "error": "That UI slash command is not exposed by ACP as a prompt. Use the matching devin-bridge tool, or set as_text=true to send it as literal text.",
        "supported_commands": [
            "/help", "/commands", "/ls", "/sessions", "/resume <id>", "/status <id>",
            "/model [value]", "/mode [value]", "/stop [id]", "/cancel [id]",
            "/dump <id> [tail]", "/send <id> <message>", "/redirect <id> <instruction>",
            "/queue <id> <message>", "/queue-status [id]", "/runqueue [id|queue_id]",
            "/cancel-queue <queue_id>", "/new [cwd] [prompt]", "/close <id>",
        ],
    }


def tool_stop_session(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = _acp_session_id(str(args.get("session_id") or ""))
    ACP.notify("session/cancel", {"sessionId": sid})
    wait_seconds = _wait_seconds(args.get("wait_seconds"), 10)
    deadline = time.monotonic() + wait_seconds
    last_acp = None
    while time.monotonic() < deadline:
        try:
            last_acp = _session_from_acp(sid)
        except Exception:
            last_acp = None
        state = json.dumps(last_acp or {}).lower()
        if not any(x in state for x in ("working", "running", "busy", "processing")):
            break
        time.sleep(0.5)
    return {"session_id": sid, "cancel_sent": True, "session": last_acp}


def tool_redirect_session(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = _acp_session_id(str(args.get("session_id") or ""))
    instruction = str(args.get("instruction") or "").strip()
    if not instruction:
        raise BridgeError("instruction is required")
    out: Dict[str, Any] = {"session_id": sid, "redirect_instruction": instruction}
    if bool(args.get("stop_first", True)):
        out["stop"] = tool_stop_session({"session_id": sid, "wait_seconds": args.get("wait_seconds", 10)})
    message = (
        "[devin-bridge redirect] Stop the current approach/task and re-orient to this instruction. "
        "Do not continue the previous plan unless it still applies.\n\n"
        f"New instruction: {instruction}"
    )
    out["prompt"] = _send_prompt(
        sid,
        message,
        wait=bool(args.get("wait", False)),
        timeout=_wait_seconds(args.get("timeout_seconds"), DEFAULT_PROMPT_TIMEOUT),
        cwd=args.get("cwd"),
    )
    return out


def tool_wait_request(args: Dict[str, Any]) -> Dict[str, Any]:
    try:
        req_id = int(args.get("request_id"))
    except Exception:
        raise BridgeError("request_id must be the integer returned by devin_send_message")
    timeout = _wait_seconds(args.get("timeout_seconds"), DEFAULT_PROMPT_TIMEOUT)
    pending = ACP.pending.get(req_id, {})
    sid = None
    if isinstance(pending.get("params"), dict):
        sid = pending["params"].get("sessionId")
    result = ACP.wait(req_id, timeout=timeout, updates_for_session=sid)
    updates = result.pop("_bridge_updates", []) if isinstance(result, dict) else []
    return {
        "request_id": req_id,
        "session_id": sid,
        "result": result,
        "response_text": _extract_agent_chunks(updates, sid) if sid else "",
        "updates_seen": len(updates),
    }


def tool_get_config_options(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = _acp_session_id(str(args.get("session_id") or ""))
    result = _resume(sid, cwd=args.get("cwd"))
    return {"session_id": sid, "configOptions": result.get("configOptions", [])}


def tool_set_model(args: Dict[str, Any]) -> Dict[str, Any]:
    return _set_config(
        str(args.get("session_id") or ""),
        "model",
        args.get("model"),
        cwd=args.get("cwd"),
    )


def tool_set_config_option(args: Dict[str, Any]) -> Dict[str, Any]:
    config_id = str(args.get("config_id") or "")
    if not config_id:
        raise BridgeError("config_id is required")
    return _set_config(
        str(args.get("session_id") or ""),
        config_id,
        args.get("value"),
        cwd=args.get("cwd"),
    )


def tool_list_slash_commands(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = str(args.get("session_id") or "").strip()
    commands: List[Dict[str, Any]] = []
    if sid:
        sid = _acp_session_id(sid)
        _resume(sid, cwd=args.get("cwd"))
        # Give the agent a short window to emit available_commands_update.
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            try:
                msg = ACP._read_raw(timeout=0.2)
            except Exception:
                break
            ACP._handle_side_message(msg)
        for item in ACP.updates:
            msg = item.get("message", {})
            params = msg.get("params") or {}
            update = params.get("update") or params
            if update.get("sessionUpdate") == "available_commands_update":
                cmds = update.get("availableCommands") or update.get("commands") or []
                if isinstance(cmds, list):
                    commands = cmds
    fallback = [
        {"name": "/ls", "description": "List sessions"},
        {"name": "/resume", "description": "Resume a session"},
        {"name": "/model", "description": "Select model"},
        {"name": "/mode", "description": "Select agent mode"},
        {"name": "/fork", "description": "Fork the session"},
        {"name": "/rename", "description": "Rename session"},
        {"name": "/steps", "description": "Show recent steps"},
        {"name": "/revert", "description": "Revert a step"},
        {"name": "/handoff", "description": "Hand off session"},
        {"name": "/open", "description": "Open session UI"},
        {"name": "/ssh", "description": "SSH into session VM"},
        {"name": "/delete", "description": "Delete a local session"},
    ]
    return {"session_id": sid or None, "available_commands": commands, "common_commands": fallback}


def tool_close_session(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = _acp_session_id(str(args.get("session_id") or ""))
    if args.get("stop_first"):
        tool_stop_session({"session_id": sid, "wait_seconds": args.get("wait_seconds", 10)})
    result = ACP.call("session/close", {"sessionId": sid}, timeout=30)
    ACP.resumed.pop(sid, None)
    return {"session_id": sid, "closed": True, "result": result}


def tool_delete_session(args: Dict[str, Any]) -> Dict[str, Any]:
    sid = _acp_session_id(str(args.get("session_id") or ""))
    if args.get("stop_first"):
        tool_stop_session({"session_id": sid, "wait_seconds": args.get("wait_seconds", 10)})
    result = ACP.call("session/delete", {"sessionId": sid}, timeout=30)
    ACP.resumed.pop(sid, None)
    return {"session_id": sid, "deleted": True, "result": result}


def tool_queue_prompt(args: Dict[str, Any]) -> Dict[str, Any]:
    session_id = str(args.get("session_id") or "").strip()
    message = str(args.get("message") or "")
    if not session_id:
        raise BridgeError("session_id is required")
    if not message:
        raise BridgeError("message is required")
    job = _queue_add_job(
        session_id,
        message,
        cwd=args.get("cwd"),
        from_session_id=args.get("from_session_id"),
        timeout_seconds=args.get("timeout_seconds"),
    )
    out = {"queued": True, "job": job, "queue_path": str(QUEUE_PATH)}
    if args.get("process"):
        out["process"] = tool_process_queue(
            {
                "queue_id": job["id"],
                "timeout_seconds": args.get("timeout_seconds"),
                "limit": 1,
            }
        )
    return out


def tool_queue_status(args: Dict[str, Any]) -> Dict[str, Any]:
    data = _queue_read()
    jobs = list(data.get("jobs", []))
    session_id = args.get("session_id")
    status = args.get("status")
    queue_id = args.get("queue_id")
    if session_id:
        sid = _acp_session_id(str(session_id))
        jobs = [j for j in jobs if j.get("session_id") == sid]
    if status:
        jobs = [j for j in jobs if j.get("status") == status]
    if queue_id:
        jobs = [j for j in jobs if j.get("id") == queue_id]
    limit = int(args.get("limit") or 100)
    jobs = jobs[-limit:]
    return {
        "queue_path": str(QUEUE_PATH),
        "count": len(jobs),
        "status_counts": _queue_summary(list(data.get("jobs", []))),
        "jobs": jobs,
    }


def tool_process_queue(args: Dict[str, Any]) -> Dict[str, Any]:
    limit = max(1, min(int(args.get("limit") or 10), 100))
    session_id = args.get("session_id")
    queue_id = args.get("queue_id")
    stop_on_error = bool(args.get("stop_on_error", False))
    processed: List[Dict[str, Any]] = []
    for _ in range(limit):
        job = _queue_claim(session_id=session_id, queue_id=queue_id)
        if not job:
            break
        try:
            result = _send_prompt(
                job["session_id"],
                job["message"],
                wait=True,
                timeout=_wait_seconds(job.get("timeout_seconds") or args.get("timeout_seconds"), DEFAULT_PROMPT_TIMEOUT),
                cwd=job.get("cwd"),
            )
            updated = _queue_update_job(
                job["id"],
                status="done",
                request_id=result.get("request_id"),
                result=result,
                error=None,
            )
            processed.append({"job": updated or job, "ok": True})
        except Exception as e:
            updated = _queue_update_job(job["id"], status="failed", error=str(e))
            processed.append({"job": updated or job, "ok": False, "error": str(e)})
            if stop_on_error:
                break
    remaining = sum(1 for j in _queue_read().get("jobs", []) if j.get("status") == "queued")
    return {"processed": processed, "processed_count": len(processed), "queued_remaining": remaining}


def tool_queue_cancel(args: Dict[str, Any]) -> Dict[str, Any]:
    queue_id = str(args.get("queue_id") or "")
    if not queue_id:
        raise BridgeError("queue_id is required")
    job = _queue_cancel_job(queue_id)
    if not job:
        raise BridgeError(f"queue job not found: {queue_id}")
    return {"job": job, "cancelled": job.get("status") == "cancelled"}


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "devin_bridge_status",
        "description": "Check whether the Devin ACP host and local session DB are reachable.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "devin_list_sessions",
        "description": "List local Devin sessions, newest first. Uses ACP when available, with SQLite fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 500},
                "query": {"type": "string", "description": "Filter by id/title/workspace"},
                "source": {"type": "string", "enum": ["auto", "acp", "db"], "default": "auto"},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "devin_session_status",
        "description": "Get metadata and live status for a Devin session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "cwd": {"type": "string", "description": "Fallback workspace for sessions whose recorded cwd no longer exists"},
                "include_config": {"type": "boolean", "default": False},
            },
            "required": ["session_id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "devin_resume_session",
        "description": "Resume/load a local Devin session through ACP and return config options/status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "cwd": {"type": "string", "description": "Workspace override if the stored cwd is missing"},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "devin_new_session",
        "description": "Create a new local Devin chat/session through ACP. Optionally set model and send an initial prompt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "cwd": {"type": "string", "description": "Workspace directory for the new session"},
                "sidebar_folder": {"type": "string", "description": "Best-effort metadata only; Devin sidebar folder grouping is UI-side and may ignore this over local ACP"},
                "org_id": {"type": "string", "description": "Sidebar folder org key; defaults to local"},
                "title": {"type": "string"},
                "model": {"type": "string", "description": "Model value or fuzzy name, e.g. swe-2-high (SWE-2) or swe-2-flash-medium"},
                "mode": {"type": "string", "description": "Session mode: accept-edits, smart, ask, plan, or bypass"},
                "thinking": {"type": "string", "description": "Thinking level for models that support it: medium, high, or max"},
                "prompt": {"type": "string"},
                "wait": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "default": 180},
            },
        },
    },
    {
        "name": "devin_send_message",
        "description": "Send a normal text prompt/message to another local Devin session and optionally wait for its response.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message": {"type": "string"},
                "from_session_id": {"type": "string", "description": "Optional sender session id to include in message text"},
                "cwd": {"type": "string", "description": "Workspace override for resuming"},
                "wait": {"type": "boolean", "default": True},
                "timeout_seconds": {"type": "number", "default": 180},
            },
            "required": ["session_id", "message"],
        },
    },
    {
        "name": "devin_send_slash_command",
        "description": "Run a bridge-supported slash command semantically (for example /model swe-2-max, /mode plan, /stop, /ls, /dump). Unknown UI-only commands can be sent literally with as_text=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Target session for commands that need one"},
                "command": {"type": "string"},
                "cwd": {"type": "string"},
                "wait": {"type": "boolean", "default": True},
                "timeout_seconds": {"type": "number", "default": 180},
                "as_text": {"type": "boolean", "default": False, "description": "Send unknown commands literally through session/prompt"},
                "prompt": {"type": "string", "description": "Optional initial prompt for /new"},
                "stop_first": {"type": "boolean", "default": False, "description": "Stop before /close"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "devin_stop_session",
        "description": "Cancel/stop the active turn in a Devin session using ACP session/cancel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "wait_seconds": {"type": "number", "default": 10},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "devin_redirect_session",
        "description": "Stop a session first, then send a new instruction to change direction/steer it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "instruction": {"type": "string"},
                "stop_first": {"type": "boolean", "default": True},
                "wait_seconds": {"type": "number", "default": 10},
                "wait": {"type": "boolean", "default": False},
                "timeout_seconds": {"type": "number", "default": 180},
                "cwd": {"type": "string"},
            },
            "required": ["session_id", "instruction"],
        },
    },
    {
        "name": "devin_wait_request",
        "description": "Wait for a prompt request_id returned by devin_send_message with wait=false.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "integer"},
                "timeout_seconds": {"type": "number", "default": 180},
            },
            "required": ["request_id"],
        },
    },
    {
        "name": "devin_get_config_options",
        "description": "Resume a session and return ACP config options (model, mode, context options).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["session_id"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "devin_set_model",
        "description": "Set a session's model through ACP config options. Accepts values like swe-2-max or fuzzy names like swe/opus.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "model": {"type": "string"},
                "cwd": {"type": "string"},
            },
            "required": ["session_id", "model"],
        },
    },
    {
        "name": "devin_set_config_option",
        "description": "Set any ACP session config option by configId, e.g. mode=plan/ask/accept-edits/bypass.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "config_id": {"type": "string"},
                "value": {"type": ["string", "boolean"]},
                "cwd": {"type": "string"},
            },
            "required": ["session_id", "config_id", "value"],
        },
    },
    {
        "name": "devin_dump_session",
        "description": "Dump a local Devin session transcript to a markdown file from SQLite (read-only).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "output_path": {"type": "string", "description": "Optional absolute output file path"},
                "tail": {"type": "integer", "default": 0, "description": "Only include the last N nodes"},
                "include_tools": {"type": "boolean", "default": True},
                "include_system": {"type": "boolean", "default": False},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "devin_list_slash_commands",
        "description": "List slash commands advertised by a session, plus common Devin commands as fallback.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "cwd": {"type": "string"},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "devin_queue_prompt",
        "description": "Enqueue a prompt/message for a Devin session in the shared bridge queue. It is not sent until devin_process_queue runs, unless process=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "message": {"type": "string"},
                "from_session_id": {"type": "string"},
                "cwd": {"type": "string"},
                "timeout_seconds": {"type": "number", "default": 180},
                "process": {"type": "boolean", "default": False},
            },
            "required": ["session_id", "message"],
        },
    },
    {
        "name": "devin_queue_status",
        "description": "Inspect the shared devin-bridge prompt queue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "queue_id": {"type": "string"},
                "status": {"type": "string", "enum": ["queued", "sending", "done", "failed", "cancelled"]},
                "limit": {"type": "integer", "default": 100},
            },
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "devin_process_queue",
        "description": "Process queued bridge prompts sequentially and wait for each session response.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Only process jobs for this session"},
                "queue_id": {"type": "string", "description": "Only process one queue job"},
                "limit": {"type": "integer", "default": 10},
                "timeout_seconds": {"type": "number", "default": 180},
                "stop_on_error": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "devin_queue_cancel",
        "description": "Mark a still-queued bridge queue job as cancelled so it will not be sent.",
        "inputSchema": {
            "type": "object",
            "properties": {"queue_id": {"type": "string"}},
            "required": ["queue_id"],
        },
    },
    {
        "name": "devin_close_session",
        "description": "Close/detach a resumed ACP session. Fails while busy unless stop_first is used.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "stop_first": {"type": "boolean", "default": False},
                "wait_seconds": {"type": "number", "default": 10},
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "devin_delete_session",
        "description": "Delete a local Devin session through ACP session/delete. This removes the session from the local session list/database.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "stop_first": {"type": "boolean", "default": False},
                "wait_seconds": {"type": "number", "default": 10},
            },
            "required": ["session_id"],
        },
    },
]

TOOL_FUNCS = {
    "devin_bridge_status": tool_bridge_status,
    "devin_list_sessions": tool_list_sessions,
    "devin_session_status": tool_session_status,
    "devin_resume_session": tool_resume_session,
    "devin_new_session": tool_new_session,
    "devin_send_message": tool_send_message,
    "devin_send_slash_command": tool_slash_command,
    "devin_stop_session": tool_stop_session,
    "devin_redirect_session": tool_redirect_session,
    "devin_wait_request": tool_wait_request,
    "devin_get_config_options": tool_get_config_options,
    "devin_set_model": tool_set_model,
    "devin_set_config_option": tool_set_config_option,
    "devin_dump_session": tool_dump_session,
    "devin_list_slash_commands": tool_list_slash_commands,
    "devin_queue_prompt": tool_queue_prompt,
    "devin_queue_status": tool_queue_status,
    "devin_process_queue": tool_process_queue,
    "devin_queue_cancel": tool_queue_cancel,
    "devin_close_session": tool_close_session,
    "devin_delete_session": tool_delete_session,
}


PROMPTS: List[Dict[str, Any]] = [
    {
        "name": "devin-sessions",
        "description": "List local Devin sessions through devin-bridge",
        "arguments": [{"name": "query", "description": "Optional filter", "required": False}],
    },
    {
        "name": "devin-chat",
        "description": "Send a message/prompt to another Devin session",
        "arguments": [
            {"name": "session_id", "description": "Target session id", "required": True},
            {"name": "message", "description": "Message to send", "required": True},
            {"name": "wait", "description": "true/false", "required": False},
        ],
    },
    {
        "name": "devin-new-chat",
        "description": "Create a new local Devin session and optionally send its first prompt",
        "arguments": [
            {"name": "cwd", "description": "Workspace directory", "required": True},
            {"name": "prompt", "description": "Initial prompt", "required": False},
            {"name": "model", "description": "Model value/name", "required": False},
        ],
    },
    {
        "name": "devin-stop",
        "description": "Stop/cancel the current turn in another Devin session",
        "arguments": [{"name": "session_id", "description": "Target session id", "required": True}],
    },
    {
        "name": "devin-redirect",
        "description": "Stop then send a new instruction to steer another Devin session",
        "arguments": [
            {"name": "session_id", "description": "Target session id", "required": True},
            {"name": "instruction", "description": "New direction/instruction", "required": True},
        ],
    },
    {
        "name": "devin-dump",
        "description": "Dump a Devin session transcript to a markdown file",
        "arguments": [
            {"name": "session_id", "description": "Target session id", "required": True},
            {"name": "tail", "description": "Optional last N nodes", "required": False},
        ],
    },
    {
        "name": "devin-model",
        "description": "Set the model for a local Devin session",
        "arguments": [
            {"name": "session_id", "description": "Target session id", "required": True},
            {"name": "model", "description": "Model value/name", "required": True},
        ],
    },
    {
        "name": "devin-queue",
        "description": "Add a prompt to the shared devin-bridge queue for a Devin session",
        "arguments": [
            {"name": "session_id", "description": "Target session id", "required": True},
            {"name": "message", "description": "Prompt/message to queue", "required": True},
            {"name": "process", "description": "true/false", "required": False},
        ],
    },
]


def _prompt_get(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    q = args.get("query", "")
    session_id = args.get("session_id", "<session-id>")
    message = args.get("message", "<message>")
    cwd = args.get("cwd", str(HOME))
    instruction = args.get("instruction", "<new instruction>")
    model = args.get("model", "<model>")
    tail = args.get("tail", "0")

    if name == "devin-sessions":
        text = "Use the devin-bridge MCP tool devin_list_sessions to list local Devin sessions."
        if q:
            text += f" Filter by: {q}"
    elif name == "devin-chat":
        wait = args.get("wait", "true")
        text = (
            f"Use devin_send_message to send this message to Devin session `{session_id}` "
            f"with wait={wait}: {message}"
        )
    elif name == "devin-new-chat":
        prompt = args.get("prompt", "")
        text = f"Use devin_new_session to create a local Devin session in `{cwd}`."
        if model != "<model>":
            text += f" Set model to `{model}`."
        if prompt:
            text += f" Then send initial prompt: {prompt}"
    elif name == "devin-stop":
        text = f"Use devin_stop_session to cancel the active turn in Devin session `{session_id}`."
    elif name == "devin-redirect":
        text = (
            f"Use devin_redirect_session on `{session_id}` to stop current work and redirect it. "
            f"Instruction: {instruction}"
        )
    elif name == "devin-dump":
        text = f"Use devin_dump_session for `{session_id}` with tail={tail}; report the output path."
    elif name == "devin-model":
        text = f"Use devin_set_model to set Devin session `{session_id}` model to `{model}`."
    elif name == "devin-queue":
        process = args.get("process", "false")
        text = (
            f"Use devin_queue_prompt to enqueue this prompt for Devin session `{session_id}` "
            f"with process={process}: {message}"
        )
    else:
        raise BridgeError(f"unknown prompt: {name}")

    return {
        "description": next((p.get("description") for p in PROMPTS if p["name"] == name), name),
        "messages": [{"role": "user", "content": {"type": "text", "text": text}}],
    }


# ---------------------------------------------------------------------------
# MCP stdio JSON-RPC


def _write_mcp(msg: Dict[str, Any]) -> None:
    data = (json.dumps(msg, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    out = getattr(sys.stdout, "buffer", sys.stdout)
    out.write(data)
    out.flush()


def _rpc_error(req_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
    err = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _handle_call_tool(params: Dict[str, Any]) -> Dict[str, Any]:
    name = params.get("name")
    args = params.get("arguments") or {}
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return _tool_result(f"Unknown tool: {name}", is_error=True)
    try:
        return _tool_result(fn(args))
    except Exception as e:
        return _error_result(e)


def _handle_request(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = msg.get("id")
    method = msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        requested = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": requested if isinstance(requested, str) else MCP_PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {},
                    "prompts": {},
                    "logging": {},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": (
                    "Bridge for local Devin sessions. Use devin_list_sessions first, then "
                    "devin_dump_session for read-only logs or devin_send_message/devin_stop_session/ "
                    "devin_redirect_session for control. Prompts are exposed as slash commands."
                ),
            },
        }
    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        return {"jsonrpc": "2.0", "id": req_id, "result": _handle_call_tool(params)}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": PROMPTS}}
    if method == "prompts/get":
        try:
            result = _prompt_get(params.get("name"), params.get("arguments") or {})
        except Exception as e:
            return _rpc_error(req_id, -32602, str(e))
        return {"jsonrpc": "2.0", "id": req_id, "result": result}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}
    if method == "resources/templates/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resourceTemplates": []}}
    if method == "completion/complete":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"completion": {"values": [], "total": 0, "hasMore": False}},
        }
    if method == "logging/setLevel":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    if method == "shutdown":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}
    return _rpc_error(req_id, -32601, f"Method not found: {method}")


def main() -> int:
    # MCP stdout is reserved for protocol. All diagnostics belong on stderr.
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except Exception:
            continue
        try:
            if msg.get("method") and "id" in msg:
                resp = _handle_request(msg)
                if resp is not None:
                    _write_mcp(resp)
            elif msg.get("method"):
                # MCP lifecycle notifications.
                if msg.get("method") == "exit":
                    break
            # Ignore client responses.
        except Exception as e:
            if "id" in msg:
                _write_mcp(_rpc_error(msg.get("id"), -32603, str(e)))
    ACP.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
