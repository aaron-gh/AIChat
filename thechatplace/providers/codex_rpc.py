"""A managed, read-only app-server connection for background workers.

Concurrent list/history requests share one reader and are matched by ID, not
arrival order. Notifications are discarded in this phase. Unexpected server
requests close the connection rather than granting tools or credentials.
No inference or automatic request replay is supported (#184) (#171).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import queue
import threading

from .. import __version__, platform_paths


class CodexRpcError(RuntimeError):
    """Public diagnostics never include raw RPC payloads or stderr."""


@dataclass
class _Pending:
    ready: threading.Event = field(default_factory=threading.Event)
    result: dict | None = None
    error: CodexRpcError | None = None


class ReadOnlyCodexConnection:
    METHODS = frozenset({"account/read", "thread/list", "thread/read"})
    MAX_FRAME = 32 * 1024 * 1024
    MAX_REQUEST = 64 * 1024
    MAX_PENDING = 32

    def __init__(self, executable=None, *, timeout=20.0, start=None, initialize=True):
        if timeout <= 0:
            raise ValueError("Codex request timeout must be positive")
        self._timeout = timeout
        self._lock = threading.Lock()
        self._initialize_lock = threading.Lock()
        self._initialized = False
        self._pending: dict[int, _Pending] = {}
        self._next_id = 0
        self._closed = False
        self._cleanup_done = threading.Event()
        self._cleanup_failed = False
        self._outgoing = queue.Queue()
        self._process = (start or platform_paths.start_codex_server)(executable)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._writer = threading.Thread(target=self._write, daemon=True)
        self._reader.start()
        self._writer.start()
        if initialize:
            self.initialize()

    @property
    def closed(self) -> bool:
        with self._lock:
            return self._closed

    def initialize(self):
        """Allow the owner to register this process before the handshake.

        Shutdown can then interrupt a blocked handshake instead of waiting
        until the constructor returns to learn which process it owns.
        """
        with self._initialize_lock:
            if self._initialized:
                return
            self._initialize()

    def _initialize(self):
        try:
            self._request("initialize", {"clientInfo": {"name": "thechatplace",
                           "title": "The Chat Place", "version": __version__}})
            self._enqueue({"method": "initialized", "params": {}})
            self._initialized = True
        except Exception:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def request(self, method: str, params: dict) -> dict:
        if method not in self.METHODS:
            raise ValueError("Method is not supported by the read-only Codex connection")
        if not self._initialized:
            raise CodexRpcError("Codex connection is not initialized")
        return self._request(method, params)

    def _request(self, method, params):
        if not isinstance(params, dict):
            raise ValueError("Codex request parameters must be an object")
        pending = _Pending()
        with self._lock:
            if self._closed:
                raise CodexRpcError("Codex connection closed")
            if len(self._pending) >= self.MAX_PENDING:
                raise CodexRpcError("Too many pending Codex requests")
            self._next_id += 1
            request_id = self._next_id
            payload = self._encode({"id": request_id, "method": method, "params": params})
            self._pending[request_id] = pending
            self._outgoing.put(payload)
        if not pending.ready.wait(self._timeout):
            self._fail("Codex request timed out")
            raise CodexRpcError("Codex request timed out")
        if pending.error:
            raise pending.error
        return pending.result

    def _encode(self, message):
        payload = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(payload) > self.MAX_REQUEST:
            raise ValueError("Codex request is too large")
        return payload

    def _enqueue(self, message):
        payload = self._encode(message)
        with self._lock:
            if self._closed:
                raise CodexRpcError("Codex connection closed")
            self._outgoing.put(payload)

    def _write(self):
        try:
            while (payload := self._outgoing.get()) is not None:
                self._process.stdin.write(payload)
                self._process.stdin.flush()
        except (OSError, ValueError):
            self._fail("Codex connection write failed")

    def _read(self):
        try:
            while True:
                raw = self._process.stdout.readline(self.MAX_FRAME + 1)
                if not raw:
                    self._fail("Codex connection closed")
                    return
                if len(raw) > self.MAX_FRAME or not raw.endswith(b"\n"):
                    raise ValueError("Invalid frame")
                message = json.loads(raw.decode("utf-8"))
                self._receive(message)
        except (OSError, ValueError, TypeError, RecursionError):
            self._fail("Codex returned an invalid protocol message")

    def _receive(self, message):
        if not isinstance(message, dict):
            raise ValueError("Invalid protocol object")
        if "method" in message:
            if not isinstance(message["method"], str) or "id" in message:
                raise ValueError("Unexpected server request")
            return  # no notification callbacks until the execution phase
        request_id = message.get("id")
        if type(request_id) is not int:
            raise ValueError("Invalid response id")
        with self._lock:
            if self._closed:
                return
            pending = self._pending.get(request_id)
            if pending is None:
                raise ValueError("Unknown response id")
            if "error" in message and "result" not in message:
                pending.error = CodexRpcError("Codex request failed")
            elif "error" not in message and isinstance(message.get("result"), dict):
                pending.result = message["result"]
            else:
                raise ValueError("Invalid response")
            del self._pending[request_id]
            pending.ready.set()

    def _fail(self, reason):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for pending in self._pending.values():
                pending.error = CodexRpcError(reason)
                pending.ready.set()
            self._pending.clear()
            self._outgoing.put(None)
        try:
            self._process.close()
        except Exception:  # noqa: BLE001 - redact cleanup failures too
            self._cleanup_failed = True
        finally:
            self._cleanup_done.set()

    def close(self):
        """Wait for process cleanup, including a failure on another worker."""
        self._fail("Codex connection closed")
        if not self._cleanup_done.wait(10) or self._cleanup_failed:
            raise CodexRpcError("Codex process cleanup failed")
