"""LSP manager + Tier-A fallback (WP4 of v0.12.0).

Owns at most one :class:`LSPClient` per workspace. When no language server
is configured or the configured binary is not on PATH, intelligence falls
back to the existing Tier-A AST tools (``code_index``/``code_imports``)
instead of failing. Nothing here downloads a language server.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

from quantsmind_agent.code_imports import importers_of
from quantsmind_agent.code_index import parse_symbols
from quantsmind_agent.services.lsp.client import LSPClient, LSPClientError

__all__ = ["LSPManager", "configured_server_command"]


def configured_server_command(workspace_root: str | Path | None) -> list[str] | None:
    """Resolve a configured language-server command, or None.

    Order: ``$QM_LSP_COMMAND`` (JSON list or whitespace-split), then
    ``<workspace>/.quantsmind/lsp.json`` (``{"command": [...]}``). No
    automatic download and no probing beyond ``shutil.which``.
    """
    raw = os.environ.get("QM_LSP_COMMAND", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                cmd = [str(x) for x in parsed]
                return cmd if shutil.which(cmd[0]) else None
        except json.JSONDecodeError:
            parts = raw.split()
            if parts and shutil.which(parts[0]):
                return parts
        return None
    if workspace_root:
        cfg = Path(workspace_root) / ".quantsmind" / "lsp.json"
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
            cmd_raw = data.get("command")
            if isinstance(cmd_raw, list) and cmd_raw:
                cmd = [str(x) for x in cmd_raw]
                return cmd if shutil.which(cmd[0]) else None
        except (OSError, json.JSONDecodeError, AttributeError):
            return None
    return None


class LSPManager:
    """Lazy, single-server LSP owner with Tier-A fallback."""

    def __init__(
        self,
        workspace_root: str | Path | None,
        *,
        server_command: list[str] | None = None,
    ) -> None:
        self._root = str(workspace_root) if workspace_root else None
        self._command = (
            server_command
            if server_command is not None
            else configured_server_command(workspace_root)
        )
        self._client: LSPClient | None = None
        self._lock = threading.Lock()
        self._versions: dict[str, int] = {}
        self._gen = 0  # stale-request generation counter
        self._cancel_events: dict[int, threading.Event] = {}

    @property
    def available(self) -> bool:
        return self._command is not None and bool(shutil.which(self._command[0]))

    @property
    def initialized(self) -> bool:
        return self._client is not None and self._client.is_initialized()

    @property
    def status(self) -> str:
        if not self._command:
            return "no language server configured (Tier-A AST fallback active)"
        if not shutil.which(self._command[0]):
            return f"language server not found: {self._command[0]} (Tier-A AST fallback active)"
        if self.initialized:
            return f"language server ready: {' '.join(self._command)}"
        return f"language server configured, not started: {' '.join(self._command)}"

    def ensure_started(self) -> LSPClient | None:
        """Start the server lazily. Returns None (never raises) when unavailable."""
        if not self.available:
            return None
        with self._lock:
            if self._client is not None and self._client.is_initialized():
                return self._client
            assert self._command is not None
            root_uri = f"file:///{self._root.replace(chr(92), '/')}" if self._root else None
            client = LSPClient(
                server_command=list(self._command), root_uri=root_uri, root_path=self._root
            )
            try:
                client.start()
            except LSPClientError:
                with contextlib.suppress(Exception):
                    client.shutdown()
                return None
            self._client = client
            return client

    # -- document lifecycle -------------------------------------------------
    def did_open(self, uri: str, language_id: str, text: str) -> None:
        with self._lock:
            self._versions[uri] = self._versions.get(uri, 0) + 1
            version = self._versions[uri]
            client = self._client
        if client is not None and client.is_initialized():
            with contextlib.suppress(LSPClientError):
                client.did_open(uri, language_id, version, text)
        else:
            self.ensure_started()

    def did_change(self, uri: str, text: str) -> None:
        with self._lock:
            self._versions[uri] = self._versions.get(uri, 0) + 1
            version = self._versions[uri]
            client = self._client
        if client is not None and client.is_initialized():
            with contextlib.suppress(LSPClientError):
                client.did_change(uri, version, [{"text": text}])

    def did_close(self, uri: str) -> None:
        with self._lock:
            client = self._client
        if client is not None and client.is_initialized():
            with contextlib.suppress(LSPClientError):
                client.did_close(uri)

    # -- diagnostics ---------------------------------------------------------
    def diagnostics(self, uri: str) -> list[dict[str, Any]]:
        client = self._client
        return client.diagnostics(uri) if client is not None else []

    def all_diagnostics(self) -> dict[str, list[dict[str, Any]]]:
        client = self._client
        return client.all_diagnostics() if client is not None else {}

    # -- intelligence with cancellation + Tier-A fallback --------------------
    def _request_with_cancel(self, method: str, params: dict[str, Any]) -> Any:
        client = self.ensure_started()
        if client is None:
            return None
        with self._lock:
            self._gen += 1
            token = self._gen
            event = threading.Event()
            self._cancel_events[token] = event
        try:
            resp = client.request(method, params, timeout=5, cancel_event=event)
            return resp.result
        except LSPClientError:
            return None
        finally:
            with self._lock:
                self._cancel_events.pop(token, None)

    def cancel_stale(self) -> None:
        """Cancel every in-flight editor request (stale content guard)."""
        with self._lock:
            events = list(self._cancel_events.values())
        for event in events:
            event.set()

    def hover(self, uri: str, line: int, column: int) -> dict[str, Any] | None:
        result = self._request_with_cancel(
            "textDocument/hover",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        return result if isinstance(result, dict) else None

    def definition(self, uri: str, line: int, column: int) -> list[dict[str, Any]]:
        result = self._request_with_cancel(
            "textDocument/definition",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        return result if isinstance(result, list) else []

    def references(self, uri: str, line: int, column: int) -> list[dict[str, Any]]:
        result = self._request_with_cancel(
            "textDocument/references",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": column},
                "context": {"includeDeclaration": False},
            },
        )
        return result if isinstance(result, list) else []

    def completion(self, uri: str, line: int, column: int) -> list[dict[str, Any]]:
        result = self._request_with_cancel(
            "textDocument/completion",
            {"textDocument": {"uri": uri}, "position": {"line": line, "character": column}},
        )
        if isinstance(result, dict):
            items = result.get("items", [])
            return items if isinstance(items, list) else []
        return result if isinstance(result, list) else []

    # Tier-A AST fallback (always available, never raises for bad input).
    def symbols_fallback(self, text: str) -> Any:
        try:
            return parse_symbols(text)
        except Exception:
            return []

    def importers_fallback(self, target_path: str) -> Any:
        if not self._root:
            return []
        try:
            from quantsmind_agent.services.workspace.containment import Workspace

            return importers_of(Workspace(Path(self._root)), target_path)
        except Exception:
            return []

    def shutdown(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        self.cancel_stale()
        if client is not None:
            with contextlib.suppress(Exception):
                client.shutdown()
