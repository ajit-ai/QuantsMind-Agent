"""Deterministic fake LSP server for tests (WP3). Not shipped in the app."""

from __future__ import annotations

import json
import sys
import time

_counter = 0


def _send(resp: dict) -> None:
    out = json.dumps(resp).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(out) + out)
    sys.stdout.buffer.flush()


def main() -> None:
    global _counter
    while True:
        headers = b""
        while b"\r\n\r\n" not in headers:
            chunk = sys.stdin.buffer.read(1)
            if not chunk:
                return
            headers += chunk
        length = 0
        for line in headers.decode().split("\r\n"):
            if line.lower().startswith("content-length:"):
                length = int(line.split(":")[1].strip())
        body = sys.stdin.buffer.read(length)
        if not body:
            return
        msg = json.loads(body)
        method = msg.get("method")
        mid = msg.get("id")
        if method == "initialize":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"capabilities": {"hoverProvider": True}, "serverInfo": {"name": "fake"}}})
            # Publish one diagnostic after initialize to exercise notifications.
            _send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": "file:///x.py", "diagnostics": [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}, "severity": 1, "message": "fake error"}]}})
        elif method == "shutdown":
            time.sleep(0.05)
            _send({"jsonrpc": "2.0", "id": mid, "result": None})
        elif method == "exit":
            return
        elif method == "textDocument/hover":
            _send({"jsonrpc": "2.0", "id": mid, "result": {"contents": {"value": "hi"}}})
        elif method == "$/cancelRequest":
            continue
        elif mid is not None:
            _send({"jsonrpc": "2.0", "id": mid, "result": {}})


if __name__ == "__main__":
    main()
