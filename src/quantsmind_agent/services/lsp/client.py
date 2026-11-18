"""Language Server Protocol client (WP3 of v0.12.0).

Manages an external language server process over stdio using JSON-RPC 2.0.
Handles process startup/shutdown, message framing, request/response
correlation, notifications, initialization/capability negotiation, workspace
lifecycle, server crash/restart, and graceful degradation when no language
server is configured.

Nothing here imports Qt; the client runs on a worker thread and communicates
with the GUI through callbacks.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.services.lsp.protocol import (
    CompletionParams,
    DefinitionParams,
    DidChangeTextDocumentParams,
    DidCloseTextDocumentParams,
    DidOpenTextDocumentParams,
    DocumentSymbolParams,
    HoverParams,
    InitializeParams,
    InitializeResult,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    LSPMessage,
    ReferenceParams,
    TextDocumentItem,
    frame_message,
    parse_message,
)

__all__ = [
    "LSPClient",
    "LSPClientError",
    "LSPServerError",
]

#: Default timeout for waiting on a response from the server.
DEFAULT_REQUEST_TIMEOUT = 30.0

#: Maximum number of pending requests before refusing new ones.
MAX_PENDING_REQUESTS = 128


class LSPClientError(QuantsMindError):
    """General LSP client error."""

    code = "QM_LSP_CLIENT_ERROR"


class LSPServerError(LSPClientError):
    """Error reported by the language server."""

    code = "QM_LSP_SERVER_ERROR"


class LSPClient:
    """Manages an external language server process.

    Parameters
    ----------
    server_command:
        Command line to start the language server, e.g.
        ``["pylsp"]`` or ``["rust-analyzer"]``.
    root_uri:
        The workspace root URI, e.g. ``"file:///home/user/project"``.
    root_path:
        The workspace root path, e.g. ``"/home/user/project"``.
    on_message:
        Callback invoked for every notification received from the server.
    on_error:
        Callback invoked when the server reports an error or the client
        encounters a transport error.
    on_exit:
        Callback invoked when the server process exits.
    """

    def __init__(
        self,
        *,
        server_command: list[str],
        root_uri: str | None = None,
        root_path: str | None = None,
        on_message: Callable[[LSPMessage], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_exit: Callable[[int | None], None] | None = None,
    ) -> None:
        self.server_command = server_command
        self.root_uri = root_uri
        self.root_path = root_path
        self.on_message = on_message
        self.on_error = on_error
        self.on_exit = on_exit

        self._process: subprocess.Popen[bytes] | None = None
        self._reader_thread: threading.Thread | None = None
        self._writer_lock = threading.Lock()
        self._pending: dict[int | str, tuple[dict[str, Any], float]] = {}
        self._next_id = 1
        self._initialized = False
        self._server_capabilities: dict[str, Any] = {}
        self._closed = False
        self._lock = threading.Lock()
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diag_lock = threading.Lock()

        # Buffer for partial reads.
        self._buffer = b""

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the language server process.

        Raises :class:`LSPClientError` if the server cannot be started.
        """

        if self._closed:
            raise LSPClientError("LSP client is closed")

        with self._lock:
            if self._process is not None:
                return

            try:
                self._process = subprocess.Popen(
                    self.server_command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.root_path,
                    env=os.environ.copy(),
                )
            except OSError as exc:
                raise LSPClientError(f"Failed to start language server: {exc}") from exc

            self._reader_thread = threading.Thread(
                target=self._reader_loop, args=(self._process,), daemon=True
            )
            self._reader_thread.start()

        # Send initialize request.
        self.initialize(
            InitializeParams(
                process_id=self._process.pid,
                root_uri=self.root_uri,
                root_path=self.root_path,
            )
        )

    def initialize(self, params: InitializeParams) -> InitializeResult:
        """Send the initialize request and wait for the result."""

        response = self._send_request("initialize", asdict(params))
        if response.error is not None:
            raise LSPServerError(
                f"Initialize failed: {response.error.get('message', 'unknown error')}",
                details={"error": response.error},
            )

        result = InitializeResult(
            capabilities=response.result.get("capabilities", {}) if response.result else {},
            server_info=response.result.get("serverInfo") if response.result else None,
        )
        self._initialized = True
        self._server_capabilities = result.capabilities

        # Send initialized notification after initialize succeeds.
        self._send_notification("initialized", {})

        return result

    def shutdown(self) -> None:
        """Shut down the language server process."""

        with self._lock:
            if self._closed:
                return

        # Send shutdown request (best effort) while still open so the
        # closed-guard in _send_request does not refuse it.
        with contextlib.suppress(Exception):
            self._send_request("shutdown", {})

        # Send exit notification.
        with contextlib.suppress(Exception):
            self._send_notification("exit", {})

        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()

        # Wake any in-flight waiters so they fail fast instead of hanging.
        for future, _ in pending:
            future["event"].set()

        # Close stdin so a well-behaved server exits promptly.
        if self._process is not None and self._process.stdin is not None:
            with contextlib.suppress(Exception):
                self._process.stdin.close()

        # Terminate the process.
        if self._process is not None:
            try:
                if self._process.poll() is None:
                    self._process.terminate()
                    self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(Exception):
                    self._process.kill()
                with contextlib.suppress(Exception):
                    self._process.wait(timeout=5)
            except Exception:  # pragma: no cover - defensive
                pass

        if self._reader_thread is not None:
            self._reader_thread.join(timeout=5)

    def is_initialized(self) -> bool:
        """Whether the server has been initialized."""

        return self._initialized

    def server_capabilities(self) -> dict[str, Any]:
        """Return the server capabilities from initialize."""

        return dict(self._server_capabilities)

    # -- requests ----------------------------------------------------------

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        *,
        cancel_event: threading.Event | None = None,
    ) -> JSONRPCResponse:
        """Send a request and wait for the response.

        Raises :class:`LSPClientError` on timeout, cancellation or
        communication error.
        """

        return self._send_request(method, params, timeout, cancel_event=cancel_event)

    def _send_request(
        self,
        method: str,
        params: dict[str, Any] | None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
        *,
        cancel_event: threading.Event | None = None,
    ) -> JSONRPCResponse:
        """Send a JSON-RPC request and block until the response arrives."""

        with self._lock:
            if self._closed:
                raise LSPClientError("LSP client is closed")
            if self._process is None or self._process.stdout is None:
                raise LSPClientError("Language server is not running")
            if len(self._pending) >= MAX_PENDING_REQUESTS:
                raise LSPClientError(
                    f"Too many pending LSP requests (limit {MAX_PENDING_REQUESTS})"
                )

            request_id = self._next_id
            self._next_id += 1

            request = JSONRPCRequest(
                jsonrpc="2.0",
                method=method,
                params=params,
                id=request_id,
            )

            future: dict[str, Any] = {
                "response": None,
                "event": threading.Event(),
                "cancelled": False,
            }
            self._pending[request_id] = (future, time.monotonic() + timeout)

        # Write the request outside the lock to avoid blocking the reader.
        self._write_message(LSPMessage(**request.to_dict()))

        # Wait for the response, a cancellation, or the deadline.
        future_event: threading.Event = future["event"]
        deadline = time.monotonic() + timeout
        while not future_event.is_set():
            if cancel_event is not None and cancel_event.is_set():
                self.cancel_request(request_id)
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                with self._lock:
                    self._pending.pop(request_id, None)
                raise LSPClientError(f"Request '{method}' timed out after {timeout}s")
            future_event.wait(timeout=min(0.05, remaining))

        with self._lock:
            pending = self._pending.pop(request_id, None)

        if pending is None:
            raise LSPClientError(f"Request '{request_id}' was removed unexpectedly")

        future, _ = pending
        if future.get("cancelled"):
            raise LSPClientError(f"Request '{request_id}' was cancelled")
        response_dict = future.get("response")
        if response_dict is None:
            raise LSPClientError(f"Request '{request_id}' returned no response")
        return JSONRPCResponse(
            jsonrpc=response_dict.get("jsonrpc", "2.0"),
            result=response_dict.get("result"),
            error=response_dict.get("error"),
            id=response_dict.get("id"),
        )

    # -- notifications -----------------------------------------------------

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification (no response expected)."""

        self._send_notification(method, params or {})

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        notification = JSONRPCNotification(method=method, params=params)
        self._write_message(LSPMessage(**notification.to_dict()))

    # -- document lifecycle ------------------------------------------------

    def did_open(self, uri: str, language_id: str, version: int, text: str) -> None:
        """Notify the server that a document was opened."""

        self.notify(
            "textDocument/didOpen",
            asdict(
                DidOpenTextDocumentParams(
                    text_document=TextDocumentItem(
                        uri=uri, language_id=language_id, version=version, text=text
                    )
                )
            ),
        )

    def did_change(
        self,
        uri: str,
        version: int,
        changes: list[dict[str, Any]],
    ) -> None:
        """Notify the server that a document changed."""

        self.notify(
            "textDocument/didChange",
            asdict(
                DidChangeTextDocumentParams(
                    text_document={"uri": uri, "version": version},
                    content_changes=changes,
                )
            ),
        )

    def did_close(self, uri: str) -> None:
        """Notify the server that a document was closed."""

        self.notify(
            "textDocument/didClose",
            asdict(DidCloseTextDocumentParams(text_document={"uri": uri})),
        )

    # -- language requests ------------------------------------------------

    def document_symbol(self, uri: str) -> list[dict[str, Any]]:
        """Request document symbols for a file."""

        response = self.request(
            "textDocument/documentSymbol",
            asdict(DocumentSymbolParams(text_document={"uri": uri})),
        )
        if response.error is not None:
            raise LSPServerError(
                f"documentSymbol failed: {response.error.get('message', 'unknown')}",
                details={"error": response.error},
            )
        return response.result or []

    def completion(
        self,
        uri: str,
        line: int,
        column: int,
    ) -> list[dict[str, Any]]:
        """Request completion items at a position."""

        response = self.request(
            "textDocument/completion",
            asdict(
                CompletionParams(
                    text_document={"uri": uri},
                    position={"line": line, "character": column},
                )
            ),
        )
        if response.error is not None:
            raise LSPServerError(
                f"completion failed: {response.error.get('message', 'unknown')}",
                details={"error": response.error},
            )
        result = response.result or {}
        items = result.get("items", [])
        return items if isinstance(items, list) else []

    def hover(self, uri: str, line: int, column: int) -> dict[str, Any] | None:
        """Request hover information at a position."""

        response = self.request(
            "textDocument/hover",
            asdict(
                HoverParams(
                    text_document={"uri": uri},
                    position={"line": line, "character": column},
                )
            ),
        )
        if response.error is not None:
            raise LSPServerError(
                f"hover failed: {response.error.get('message', 'unknown')}",
                details={"error": response.error},
            )
        return response.result or None

    def definition(self, uri: str, line: int, column: int) -> list[dict[str, Any]]:
        """Request definition locations for a position."""

        response = self.request(
            "textDocument/definition",
            asdict(
                DefinitionParams(
                    text_document={"uri": uri},
                    position={"line": line, "character": column},
                )
            ),
        )
        if response.error is not None:
            raise LSPServerError(
                f"definition failed: {response.error.get('message', 'unknown')}",
                details={"error": response.error},
            )
        result = response.result or []
        return result if isinstance(result, list) else []

    def references(
        self,
        uri: str,
        line: int,
        column: int,
    ) -> list[dict[str, Any]]:
        """Request reference locations for a position."""

        response = self.request(
            "textDocument/references",
            asdict(
                ReferenceParams(
                    text_document={"uri": uri},
                    position={"line": line, "character": column},
                    context={"includeDeclaration": False},
                )
            ),
        )
        if response.error is not None:
            raise LSPServerError(
                f"references failed: {response.error.get('message', 'unknown')}",
                details={"error": response.error},
            )
        result = response.result or []
        return result if isinstance(result, list) else []

    # -- internal ----------------------------------------------------------

    def _write_message(self, message: LSPMessage) -> None:
        """Write a framed message to the server's stdin."""

        if self._closed or self._process is None or self._process.stdin is None:
            raise LSPClientError("Language server is not running")

        data = frame_message(message)
        with self._writer_lock:
            try:
                self._process.stdin.write(data)
                self._process.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise LSPClientError(f"Failed to write to language server: {exc}") from exc

    def _reader_loop(self, process: subprocess.Popen[bytes]) -> None:
        """Read messages from the server's stdout incrementally.

        Uses ``os.read`` on the raw fd so each call returns as soon as any
        bytes are available (``BufferedReader.read(n)`` would block until
        ``n`` bytes or EOF, stalling small responses until the 30s timeout).
        """

        assert process.stdout is not None
        fd = process.stdout.fileno()
        buffer = b""

        while not self._closed:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break

            if not chunk:
                # Process exited.
                break

            buffer += chunk

            while True:
                msg = parse_message(buffer)
                if msg is None:
                    break
                # Remove processed bytes from buffer.
                header_end = buffer.index(b"\r\n\r\n")
                header_block = buffer[:header_end].decode("utf-8")
                content_length = 0
                for line in header_block.split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        try:
                            content_length = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            content_length = 0
                        break
                body_start = header_end + 4
                consumed = body_start + content_length
                buffer = buffer[consumed:]

                self._dispatch_message(msg)

        # Process exited.
        exit_code = process.wait()
        with self._lock:
            self._closed = True
            pending = list(self._pending.values())
            self._pending.clear()
        for future, _ in pending:
            future["event"].set()
        if self.on_error is not None:
            self.on_error(f"Language server process exited with code {exit_code}")
        if self.on_exit is not None:
            self.on_exit(exit_code)

    def _dispatch_message(self, msg: LSPMessage) -> None:
        """Dispatch an incoming message to the appropriate handler."""

        if msg.id is not None and msg.method is None:
            # Response to a request. Do NOT pop here: _send_request owns
            # popping so each response resolves its request exactly once.
            # Requests from the server (id + method) are forwarded below.
            with self._lock:
                pending = self._pending.get(msg.id)
            if pending is not None:
                future, _ = pending
                if not future["event"].is_set():
                    future["response"] = msg.to_dict()
                    future["event"].set()
            return
        if msg.method is not None:
            # Notification (or server-to-client request) from the server.
            if msg.method == "textDocument/publishDiagnostics" and msg.params:
                self._store_diagnostics(msg.params)
            if self.on_message is not None:
                self.on_message(msg)

    # -- diagnostics -------------------------------------------------------

    def _store_diagnostics(self, params: dict[str, Any]) -> None:
        uri = str(params.get("uri", ""))
        diags = params.get("diagnostics", [])
        if not uri:
            return
        with self._diag_lock:
            self._diagnostics[uri] = list(diags) if isinstance(diags, list) else []

    def diagnostics(self, uri: str) -> list[dict[str, Any]]:
        """Stored diagnostics for ``uri`` (empty when none published)."""
        with self._diag_lock:
            return list(self._diagnostics.get(uri, []))

    def all_diagnostics(self) -> dict[str, list[dict[str, Any]]]:
        with self._diag_lock:
            return {k: list(v) for k, v in self._diagnostics.items()}

    def clear_diagnostics(self, uri: str | None = None) -> None:
        with self._diag_lock:
            if uri is None:
                self._diagnostics.clear()
            else:
                self._diagnostics.pop(uri, None)

    # -- cancellation ------------------------------------------------------

    def cancel_request(self, request_id: int | str) -> bool:
        """Send ``$/cancelRequest`` for an in-flight request.

        Returns True when the id was pending (the cancel was sent), False
        otherwise. The waiting `_send_request` observes the cancellation
        event and raises instead of returning a stale response.
        """
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None:
                return False
            future, _ = pending
            future["cancelled"] = True
        with contextlib.suppress(Exception):
            self._send_notification("$/cancelRequest", {"id": request_id})
        future["event"].set()
        return True
