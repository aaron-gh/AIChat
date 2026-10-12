"""Check the local read-only app-server contract without sending a prompt.

Print only aggregate counts and account type, never titles, thread IDs,
messages or tokens. Use --codex for an executable outside the shell's PATH.
This is a development probe, not the production transport (#184) (#171).
"""
from __future__ import annotations

import argparse
import json
import queue
import shutil
import subprocess
import threading
import time


class Probe:
    def __init__(self, executable):
        self.process = subprocess.Popen([executable, "app-server", "--listen", "stdio://"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True)
        self.incoming = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            for line in self.process.stdout:
                self.incoming.put(json.loads(line))
        except (ValueError, OSError):
            pass
        finally:
            self.incoming.put(None)

    def send(self, message):
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method, params):
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 20
        while True:
            try:
                message = self.incoming.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise RuntimeError(f"{method}: timed out") from None
            if message is None:
                raise RuntimeError(f"{method}: app-server closed")
            if "method" in message:
                if "id" in message:
                    # There must be no approval or tool request during this
                    # read-only probe. Stop instead of answering it implicitly.
                    raise RuntimeError(f"{method}: unexpected server request")
                continue
            if message.get("id") != request_id:
                raise RuntimeError(f"{method}: unexpected response id")
            if "error" in message:
                raise RuntimeError(f"{method}: RPC failed")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError(f"{method}: invalid result")
            return result

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    args = parser.parse_args()
    if not args.codex:
        parser.error("Codex executable not found; pass --codex")
    probe = Probe(args.codex)
    try:
        probe.request("initialize", {"clientInfo": {"name": "thechatplace_probe",
                                                   "title": "The Chat Place development probe",
                                                   "version": "1"}})
        probe.send({"method": "initialized", "params": {}})
        account = probe.request("account/read", {"refreshToken": False}).get("account")
        summary = {"initialized": True,
                   "account_type": account.get("type") if isinstance(account, dict) else None}
        for archived in (False, True):
            count, cursor, cursors = 0, None, set()
            while True:
                params = {"limit": 100, "archived": archived,
                          "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"]}
                if cursor:
                    params["cursor"] = cursor
                result = probe.request("thread/list", params)
                data = result.get("data")
                if not isinstance(data, list):
                    raise RuntimeError("thread/list: invalid data")
                count += len(data)
                cursor = result.get("nextCursor")
                if cursor is None:
                    break
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise RuntimeError("thread/list: invalid pagination")
                cursors.add(cursor)
            summary["archived_threads" if archived else "active_threads"] = count
        print(json.dumps(summary, indent=2))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
