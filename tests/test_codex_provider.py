"""Synthetic protocol records based on app-server 0.162.0-alpha.17.2 schema."""
import pytest

from thechatplace.providers.codex import CodexProvider, transcript_from_thread
from thechatplace.session_controller import SessionController
from thechatplace.sessions import SessionInfo, NEEDS_YOU, WORKING
from thechatplace.transcript import ASSISTANT, USER, TOOL, TOOL_RESULT


def thread(native_id="one", **extra):
    return dict(id=native_id, name="Build", preview="Preview", cwd="/example",
                createdAt=100, updatedAt=200, status={"type": "notLoaded"}, **extra)


def history(native_id="one"):
    return thread(native_id, turns=[{"id": "turn-1", "startedAt": 100, "items": [
        {"type": "userMessage", "id": "u", "content": [{"type": "text", "text": "Hi"}]},
        {"type": "commandExecution", "id": "cmd", "command": "pytest", "aggregatedOutput": "Passed"},
        {"type": "agentMessage", "id": "a", "text": "Ready"},
    ]}])


class Connection:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, params):
        self.calls.append((method, params))
        return self.responses.pop(0)


def test_lists_all_pages_and_archives_without_loading_threads():
    connection = Connection([
        {"data": [thread()], "nextCursor": "page-2"},
        {"data": [thread(), thread("two"), thread("child", parentThreadId="one")], "nextCursor": None},
        {"data": [thread("old")], "nextCursor": None},
    ])
    snapshot = CodexProvider(connection).collect()
    assert [s.key for s in snapshot.sessions] == ["codex:one", "codex:two", "codex:old"]
    assert snapshot.sessions[-1].archived
    assert snapshot.sessions[0].state == "status unavailable"
    assert snapshot.sessions[0].created_ms == 100_000
    assert snapshot.desktop_cli_ids == set() and snapshot.live == {}
    assert [method for method, _ in connection.calls] == ["thread/list"] * 3
    assert connection.calls[1][1]["cursor"] == "page-2"
    assert "appServer" in connection.calls[0][1]["sourceKinds"]
    assert connection.calls[-1][1]["archived"] is True


def test_repeated_cursor_fails_instead_of_looping():
    connection = Connection([{"data": [], "nextCursor": "same"}] * 2)
    with pytest.raises(ValueError, match="pagination cursor"):
        CodexProvider(connection).collect()


def test_reads_backend_history_without_resume_or_claude_paths():
    connection = Connection([{"thread": history()}] * 3)
    provider = CodexProvider(connection)
    info = SessionInfo("external", "codex:one", "Build", "/example", "one", provider="codex")
    reader = provider.open_reader(info)
    assert reader.refresh()
    assert not reader.refresh()
    assert [(m.kind, m.text) for m in reader.transcript.messages] == [
        (USER, "Hi"), (TOOL, "pytest"), (TOOL_RESULT, "Passed"), (ASSISTANT, "Ready")]
    assert reader.transcript.last_reply().label == "Codex"
    assert provider.last_reply(info) == "Ready"
    assert all(method == "thread/read" and params["includeTurns"]
               for method, params in connection.calls)


def test_read_rejects_wrong_thread_without_replacing_history():
    connection = Connection([{"thread": history()}, {"thread": history("other")}])
    info = SessionInfo("external", "codex:one", "Build", "", "one", provider="codex")
    reader = CodexProvider(connection).open_reader(info)
    reader.refresh()
    with pytest.raises(ValueError, match="different or missing thread"):
        reader.refresh()
    assert reader.transcript.last_reply().text == "Ready"


def test_future_items_are_skipped_and_malformed_known_items_counted():
    data = history()
    data["turns"][0]["items"] += [
        {"type": "futureType", "id": "future", "text": "Private reasoning"},
        {"type": "userMessage", "id": "bad", "content": "wrong type"},
        {"type": "agentMessage", "id": "a", "text": "Ready"},
    ]
    transcript = transcript_from_thread(data)
    assert transcript.unreadable_lines == 1
    assert [m.text for m in transcript.messages].count("Ready") == 1
    assert not any("Private reasoning" in m.text for m in transcript.messages)


def test_missing_turns_is_an_error_not_empty_history():
    with pytest.raises(ValueError, match="readable turn history"):
        transcript_from_thread(thread())


@pytest.mark.parametrize("view", ["summary", "notLoaded"])
def test_incomplete_turn_history_is_rejected(view):
    data = history()
    data["turns"][0]["itemsView"] = view
    with pytest.raises(ValueError, match="incomplete turn history"):
        transcript_from_thread(data)


def test_paginated_history_is_not_silently_truncated():
    with pytest.raises(ValueError, match="pagination reader"):
        transcript_from_thread(thread(historyMode="paginated", turns=[]))


def test_mcp_results_errors_and_file_diffs_are_readable():
    data = history()
    data["turns"][0]["items"] += [
        {"type": "mcpToolCall", "id": "mcp", "server": "docs", "tool": "read",
         "result": {"content": [{"type": "text", "text": "Documentation"}]},
         "error": {"message": "Tool failed"}},
        {"type": "fileChange", "id": "file", "changes": [{"path": "a.py", "diff": "+new line"}]},
    ]
    messages = transcript_from_thread(data).messages
    assert any(m.kind == TOOL_RESULT and m.text == "Documentation" for m in messages)
    assert any(m.text == "Tool failed" for m in messages)
    assert any("+new line" in m.text for m in messages)


def test_codex_rejects_foreign_session_before_rpc():
    connection = Connection([])
    provider = CodexProvider(connection)
    foreign = SessionInfo("own", "own:one", "Claude", "", "one")
    with pytest.raises(ValueError, match="Not a Codex session"):
        provider.open_reader(foreign)
    assert not connection.calls


def test_controller_routes_codex_history_with_its_own_speaker():
    connection = Connection([{"thread": history()}])
    controller = SessionController([CodexProvider(connection)])
    info = SessionInfo("external", "codex:one", "Build", "", "one", provider="codex")
    assert controller.has_history(info)
    assert controller.read(info).messages[-1].label == "Codex"
