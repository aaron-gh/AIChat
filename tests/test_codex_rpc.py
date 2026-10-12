from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
import threading
from io import BytesIO
from types import SimpleNamespace

import pytest

from thechatplace import platform_paths
from thechatplace.providers.codex_rpc import CodexRpcError, ReadOnlyCodexConnection


@pytest.fixture
def server():
    owned = []

    def start(executable):
        tree = platform_paths.ProcessTree()
        argv = [sys.executable, "-u", str(Path(__file__).with_name("fake_codex_server.py"))]
        if executable:
            argv.append(executable)
        process = subprocess.Popen(argv,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, **tree.popen_kwargs())
        tree.attach(process)
        child = platform_paths.CodexServerProcess(process, tree)
        owned.append(child)
        return child

    yield start, owned
    for child in owned:
        child.close()


def test_handshake_partial_utf8_lines_and_context_cleanup(server):
    start, owned = server
    with ReadOnlyCodexConnection(start=start) as connection:
        assert connection.request("thread/read", {"threadId": "café\u2028chat"}) == {
            "echo": {"threadId": "café\u2028chat"}}
    assert owned[0].process.poll() is not None
    assert owned[0].stdin.closed and owned[0].stdout.closed
    connection.close()
    with pytest.raises(CodexRpcError, match="closed"):
        connection.request("thread/list", {})


def test_interleaved_notifications_and_reverse_responses_route_by_id(server):
    start, _ = server
    with ReadOnlyCodexConnection(start=start) as connection, ThreadPoolExecutor(2) as pool:
        first = pool.submit(connection.request, "thread/read", {"mode": "pair", "threadId": "one"})
        second = pool.submit(connection.request, "thread/read", {"mode": "pair", "threadId": "two"})
        assert first.result(timeout=5)["echo"]["threadId"] == "one"
        assert second.result(timeout=5)["echo"]["threadId"] == "two"


@pytest.mark.parametrize("mode", ["exit", "malformed", "nested", "server-request", "unknown-id", "bad-result"])
def test_protocol_failures_close_process_without_exposing_private_payloads(server, mode):
    start, owned = server
    connection = ReadOnlyCodexConnection(start=start)
    with pytest.raises(CodexRpcError) as error:
        connection.request("thread/read", {"mode": mode})
    assert "private" not in str(error.value) and "approve" not in str(error.value)
    connection.close()
    assert owned[0].process.poll() is not None
    connection._reader.join(timeout=5)
    assert not connection._reader.is_alive()
    assert owned[0].process.poll() is not None


def test_rpc_error_is_redacted_and_other_requests_remain_usable(server):
    start, _ = server
    with ReadOnlyCodexConnection(start=start) as connection:
        with pytest.raises(CodexRpcError, match="^Codex request failed$"):
            connection.request("thread/read", {"mode": "error"})
        assert connection.request("thread/list", {}) == {"echo": {}}


def test_timeout_fails_all_waiters_and_does_not_replay_requests(server):
    start, owned = server
    with ReadOnlyCodexConnection(start=start, timeout=1) as connection, ThreadPoolExecutor(2) as pool:
        first = pool.submit(connection.request, "thread/read", {"mode": "hang", "threadId": "one"})
        second = pool.submit(connection.request, "thread/read", {"mode": "hang", "threadId": "two"})
        for future in (first, second):
            with pytest.raises(CodexRpcError, match="timed out"):
                future.result(timeout=5)
        assert len(owned) == 1 and owned[0].process.poll() is not None
        with pytest.raises(CodexRpcError, match="closed"):
            connection.request("thread/list", {})


def test_shutdown_unblocks_pending_read_without_waiting_for_timeout(server):
    start, owned = server
    connection = ReadOnlyCodexConnection(start=start)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(connection.request, "thread/read", {"mode": "hang"})
        connection.close()
        with pytest.raises(CodexRpcError, match="closed"):
            future.result(timeout=5)
    connection._reader.join(timeout=5)
    connection._writer.join(timeout=5)
    assert not connection._reader.is_alive() and not connection._writer.is_alive()
    assert owned[0].process.poll() is not None


def test_read_only_methods_and_request_limits_fail_without_poisoning_connection(server):
    start, _ = server
    with ReadOnlyCodexConnection(start=start) as connection:
        for method in ("turn/start", "thread/resume", "thread/fork", "initialize", "account/login/start"):
            with pytest.raises(ValueError, match="read-only"):
                connection.request(method, {})
        with pytest.raises(ValueError, match="too large"):
            connection.request("thread/read", {"text": "x" * connection.MAX_REQUEST})
        assert connection.request("thread/list", {}) == {"echo": {}}


def test_oversize_response_fails_connection(server):
    start, _ = server
    with ReadOnlyCodexConnection(start=start) as connection:
        connection.MAX_FRAME = 80
        with pytest.raises(CodexRpcError, match="invalid protocol"):
            connection.request("thread/read", {"text": "x" * 100})


@pytest.mark.parametrize("header,suffix", [(b"#!/usr/bin/node\n", ""),
    (b"#!/bin/sh\n", ""), (b"@echo off", ".cmd"), (b"MZ", ".bat"), (b"text", "")])
def test_cli_discovery_rejects_scripts(tmp_path, header, suffix):
    path = tmp_path / ("codex" + suffix)
    path.write_bytes(header)
    path.chmod(0o755)
    assert platform_paths.find_codex(which=lambda name: str(path), native_candidates=[]) is None
    with pytest.raises(OSError, match="native Codex"):
        platform_paths.start_codex_server(str(path))


def test_cli_discovery_finds_native_candidate_after_rejected_shim(tmp_path):
    shim, native = tmp_path / "codex.cmd", tmp_path / "codex.exe"
    shim.write_bytes(b"@echo off")
    native.write_bytes(b"MZ native fixture")
    native.chmod(0o755)
    assert platform_paths.find_codex(which=lambda name: str(shim), native_candidates=[native]) == str(native)


def test_failed_handshake_closes_child_before_constructor_raises(server):
    start, owned = server
    with pytest.raises(CodexRpcError, match="request failed"):
        ReadOnlyCodexConnection("bad-init", start=start)
    assert owned[0].process.poll() is not None
    assert owned[0].stdin.closed and owned[0].stdout.closed


def test_pending_request_limit_preserves_existing_request_until_shutdown(server):
    start, _ = server
    with ReadOnlyCodexConnection(start=start) as connection, ThreadPoolExecutor(1) as pool:
        connection.MAX_PENDING = 1
        queued = threading.Event()
        encode = connection._encode

        def encoded(message):
            result = encode(message)
            if message.get("params", {}).get("mode") == "hang":
                queued.set()
            return result

        connection._encode = encoded
        waiting = pool.submit(connection.request, "thread/read", {"mode": "hang"})
        assert queued.wait(5)
        with pytest.raises(CodexRpcError, match="Too many pending"):
            connection.request("thread/list", {})
        connection.close()
        with pytest.raises(CodexRpcError, match="closed"):
            waiting.result(timeout=5)


def test_concurrent_close_waits_for_existing_cleanup(server):
    start, owned = server
    connection = ReadOnlyCodexConnection(start=start)
    child = owned[0]
    close = child.close
    entered, release = threading.Event(), threading.Event()

    def delayed_close():
        entered.set()
        assert release.wait(5)
        close()

    child.close = delayed_close
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(connection.close)
        assert entered.wait(5)
        second = pool.submit(connection.close)
        try:
            assert not first.done() and not second.done()
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    assert child.process.poll() is not None


@pytest.mark.parametrize("attach_fails", [False, True])
def test_native_launch_uses_stdio_process_tree_and_cleans_up_attach_failure(tmp_path, monkeypatch,
                                                                          attach_fails):
    executable = tmp_path / "native codex.exe"
    executable.write_bytes(b"MZ native fixture")
    executable.chmod(0o755)
    calls = []
    process = SimpleNamespace(stdin=BytesIO(), stdout=BytesIO(),
                              wait=lambda timeout: calls.append(("wait", timeout)),
                              kill=lambda: calls.append("process kill"))

    class Tree:
        def popen_kwargs(self):
            return {"creationflags": 123}

        def attach(self, child):
            assert child is process
            calls.append("attach")
            if attach_fails:
                raise OSError("attach failed")

        def kill(self):
            calls.append("tree kill")

        def close(self):
            calls.append("tree close")

    def popen(argv, **kwargs):
        assert argv == [str(executable), "app-server", "--listen", "stdio://"]
        assert kwargs == dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, creationflags=123)
        return process

    monkeypatch.setattr(platform_paths, "ProcessTree", Tree)
    monkeypatch.setattr(subprocess, "Popen", popen)
    if attach_fails:
        with pytest.raises(OSError, match="attach failed"):
            platform_paths.start_codex_server(str(executable))
    else:
        child = platform_paths.start_codex_server(str(executable))
        child.close()
        child.close()
    assert calls == ["attach", "tree kill", ("wait", 5), "tree close"]
    assert process.stdin.closed and process.stdout.closed
