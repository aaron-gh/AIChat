"""Read-only Codex history through app-server, never raw rollout files (#184).

The caller supplies an initialized RPC connection. This adapter deliberately
has no resume, start, archive or delete requests; externally discovered chats
must remain read-only until the owned-session execution phase.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol

from ..sessions import IDLE, NEEDS_YOU, WORKING, SessionInfo
from ..transcript import ASSISTANT, ERROR, PLAN, TOOL, TOOL_RESULT, USER, ChatMessage, Transcript
from .base import Snapshot


class RpcConnection(Protocol):
    def request(self, method: str, params: dict) -> dict:
        ...


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _time(value) -> int:
    return value * 1000 if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _timestamp(value) -> str:
    if not _time(value):
        return ""
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def session_from_thread(thread: dict, archived: bool) -> SessionInfo:
    native_id = _text(thread.get("id"))
    if not native_id:
        raise ValueError("Codex thread has no id")
    status = thread.get("status")
    status = status if isinstance(status, dict) else {}
    kind = status.get("type")
    # This process cannot prove that a thread not loaded here is idle in
    # another client. Keep that uncertainty visible, never guess from age.
    state, detail = "status unavailable", ""
    if kind == "idle":
        state = IDLE
    elif kind == "active":
        flags = status.get("activeFlags")
        flags = flags if isinstance(flags, list) else []
        if "waitingOnApproval" in flags or "waitingOnUserInput" in flags:
            state, detail = NEEDS_YOU, "waiting in Codex"
        else:
            state = WORKING
    elif kind == "systemError":
        state, detail = NEEDS_YOU, "Codex reported an error"
    title = _text(thread.get("name")) or _text(thread.get("preview"))
    return SessionInfo("external", f"codex:{native_id}", title.split("\n", 1)[0] or "Untitled session",
                       _text(thread.get("cwd")), native_id, provider="codex",
                       state=state, detail=detail, archived=archived,
                       created_ms=_time(thread.get("createdAt")),
                       last_activity_ms=_time(thread.get("updatedAt")))


def transcript_from_thread(thread: dict) -> Transcript:
    """Convert known protocol items; future item types are skipped safely."""
    transcript = Transcript(model=_text(thread.get("model")))
    if thread.get("historyMode") == "paginated":
        raise ValueError("Paginated Codex history needs a supported pagination reader")
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise ValueError("Codex did not return readable turn history")
    seen = set()
    for turn in turns:
        if not isinstance(turn, dict) or not isinstance(turn.get("items"), list):
            transcript.unreadable_lines += 1
            continue
        if turn.get("itemsView") in ("notLoaded", "summary"):
            raise ValueError("Codex returned incomplete turn history")
        transcript.turns += 1
        timestamp = _timestamp(turn.get("startedAt"))
        for item in turn["items"]:
            if not isinstance(item, dict):
                transcript.unreadable_lines += 1
                continue
            item_id = _text(item.get("id"))
            key = f'{_text(turn.get("id"))}:{item_id}' if item_id else ""
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            kind, text = item.get("type"), ""
            if kind in ("agentMessage", "plan"):
                text = _text(item.get("text"))
                message_kind = ASSISTANT if kind == "agentMessage" else PLAN
            elif kind == "userMessage":
                content = item.get("content")
                if not isinstance(content, list):
                    transcript.unreadable_lines += 1
                    continue
                pieces = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "text":
                        pieces.append(_text(part.get("text")))
                    elif part.get("type") in ("image", "localImage"):
                        pieces.append("[Image]")
                    elif part.get("type") in ("skill", "mention"):
                        pieces.append(_text(part.get("name")) or _text(part.get("path")))
                text, message_kind = "\n".join(pieces), USER
            elif kind == "commandExecution":
                text = _text(item.get("command"))
                message_kind = TOOL
            elif kind == "fileChange":
                changes = item.get("changes")
                if not isinstance(changes, list):
                    transcript.unreadable_lines += 1
                    continue
                text = "\n".join(_text(c.get("path")) + "\n" + _text(c.get("diff"))
                                 for c in changes if isinstance(c, dict))
                message_kind = TOOL
            elif kind == "mcpToolCall":
                text = f'{_text(item.get("server"))}: {_text(item.get("tool"))}'
                message_kind = TOOL
            else:
                continue
            if text:
                transcript.messages.append(ChatMessage(message_kind, text, timestamp, key,
                                                       assistant_name="Codex"))
            if kind == "commandExecution" and _text(item.get("aggregatedOutput")):
                transcript.messages.append(ChatMessage(TOOL_RESULT, item["aggregatedOutput"],
                                                       timestamp, key + ":output"))
            if kind == "mcpToolCall":
                result = item.get("result")
                content = result.get("content") if isinstance(result, dict) else None
                if isinstance(content, list):
                    output = "\n".join(_text(part.get("text")) for part in content
                                       if isinstance(part, dict) and part.get("type") == "text")
                    if output:
                        transcript.messages.append(ChatMessage(TOOL_RESULT, output, timestamp,
                                                               key + ":output"))
                error = item.get("error")
                if isinstance(error, dict) and _text(error.get("message")):
                    transcript.messages.append(ChatMessage(ERROR, error["message"], timestamp,
                                                           key + ":error"))
        error = turn.get("error")
        if isinstance(error, dict) and _text(error.get("message")):
            transcript.messages.append(ChatMessage(ERROR, error["message"], timestamp,
                                                   _text(turn.get("id")) + ":error"))
    return transcript


class CodexReader:
    def __init__(self, connection: RpcConnection, native_id: str):
        self.connection, self.native_id = connection, native_id
        self.transcript = Transcript()

    def refresh(self) -> bool:
        response = self.connection.request("thread/read", {"threadId": self.native_id,
                                                         "includeTurns": True})
        thread = response.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != self.native_id:
            raise ValueError("Codex returned a different or missing thread")
        history = transcript_from_thread(thread)
        changed = history != self.transcript
        self.transcript = history
        return changed


class CodexProvider:
    provider_id = "codex"

    def __init__(self, connection: RpcConnection):
        self.connection = connection

    def collect(self) -> Snapshot:
        sessions, ids = [], set()
        for archived in (False, True):
            cursor, cursors = None, set()
            while True:
                params = {"archived": archived, "limit": 100,
                          "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"],
                          "sortKey": "updated_at"}
                if cursor is not None:
                    params["cursor"] = cursor
                response = self.connection.request("thread/list", params)
                data = response.get("data")
                if not isinstance(data, list):
                    raise ValueError("Codex returned an invalid thread list")
                for thread in data:
                    if not isinstance(thread, dict):
                        raise ValueError("Codex returned invalid thread metadata")
                    if thread.get("parentThreadId"):
                        continue
                    info = session_from_thread(thread, archived)
                    if info.identity not in ids:
                        ids.add(info.identity)
                        sessions.append(info)
                cursor = response.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ValueError("Codex returned invalid or repeated pagination cursor")
                cursors.add(cursor)
        return Snapshot(sessions=sessions)

    def _check(self, session):
        if session.provider != self.provider_id or not session.cli_session_id:
            raise ValueError("Not a Codex session")

    def has_history(self, session) -> bool:
        self._check(session)
        return True  # availability is verified by the background thread/read

    def open_reader(self, session) -> CodexReader:
        self._check(session)
        return CodexReader(self.connection, session.cli_session_id)

    def last_reply(self, session) -> str:
        reader = self.open_reader(session)
        reader.refresh()
        reply = reader.transcript.last_reply()
        return reply.text if reply else ""
