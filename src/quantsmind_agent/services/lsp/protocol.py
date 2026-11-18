"""Language Server Protocol (LSP) message types and framing (WP3).

Implements the JSON-RPC 2.0 protocol used by LSP servers over stdio.
This module defines the message types, framing, and serialization used to
communicate with an external language server.

Nothing here imports Qt; the protocol layer is headless and can be tested
without a display server.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CompletionItem",
    "CompletionParams",
    "DefinitionParams",
    "Diagnostic",
    "DidChangeTextDocumentParams",
    "DidCloseTextDocumentParams",
    "DidOpenTextDocumentParams",
    "DocumentSymbol",
    "DocumentSymbolParams",
    "Hover",
    "HoverParams",
    "InitializeParams",
    "InitializeResult",
    "InitializedParams",
    "JSONRPCError",
    "JSONRPCNotification",
    "JSONRPCRequest",
    "JSONRPCResponse",
    "LSPMessage",
    "Location",
    "PublishDiagnosticsParams",
    "ReferenceParams",
    "TextDocumentItem",
    "frame_message",
    "parse_message",
]

#: JSON-RPC 2.0 error codes.
ERROR_PARSE_ERROR = -32700
ERROR_INVALID_REQUEST = -32600
ERROR_METHOD_NOT_FOUND = -32601
ERROR_INVALID_PARAMS = -32602
ERROR_INTERNAL_ERROR = -32603
ERROR_SERVER_ERROR_START = -32099
ERROR_SERVER_ERROR_END = -32000
ERROR_SERVER_NOT_INITIALIZED = -32002
ERROR_UNKNOWN_ERROR_CODE = -32003


@dataclass(slots=True)
class JSONRPCRequest:
    """A JSON-RPC 2.0 request (client -> server, expects a response)."""

    jsonrpc: str = "2.0"
    method: str = ""
    params: dict[str, Any] | None = None
    id: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"jsonrpc": self.jsonrpc, "method": self.method}
        if self.params is not None:
            d["params"] = self.params
        if self.id is not None:
            d["id"] = self.id
        return d


@dataclass(slots=True)
class JSONRPCResponse:
    """A JSON-RPC 2.0 response (server -> client, reply to a request)."""

    jsonrpc: str = "2.0"
    result: Any = None
    error: dict[str, Any] | None = None
    id: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"jsonrpc": self.jsonrpc, "id": self.id}
        if self.error is not None:
            d["error"] = self.error
        else:
            d["result"] = self.result
        return d


@dataclass(slots=True)
class JSONRPCNotification:
    """A JSON-RPC 2.0 notification (no response expected)."""

    jsonrpc: str = "2.0"
    method: str = ""
    params: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"jsonrpc": self.jsonrpc, "method": self.method}
        if self.params is not None:
            d["params"] = self.params
        return d


@dataclass(slots=True)
class JSONRPCError:
    """A JSON-RPC 2.0 error object."""

    code: int = 0
    message: str = ""
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


@dataclass(slots=True)
class LSPMessage:
    """An LSP message: either a request, response, or notification."""

    jsonrpc: str = "2.0"
    method: str | None = None
    params: dict[str, Any] | None = None
    result: Any = None
    error: dict[str, Any] | None = None
    id: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"jsonrpc": self.jsonrpc}
        if self.method is not None:
            d["method"] = self.method
        if self.params is not None:
            d["params"] = self.params
        if self.result is not None:
            d["result"] = self.result
        if self.error is not None:
            d["error"] = self.error
        if self.id is not None:
            d["id"] = self.id
        return d


# ============================================================================
# LSP-specific parameter/result types
# ============================================================================


@dataclass(slots=True)
class InitializeParams:
    """Initialize request parameters (client -> server)."""

    process_id: int | None = None
    root_uri: str | None = None
    root_path: str | None = None
    capabilities: dict[str, Any] | None = None
    trace: str | None = "off"
    workspace_folders: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class InitializeResult:
    """Initialize result (server -> client)."""

    capabilities: dict[str, Any]
    server_info: dict[str, Any] | None = None


@dataclass(slots=True)
class InitializedParams:
    """Initialized notification parameters (client -> server)."""

    pass


@dataclass(slots=True)
class TextDocumentItem:
    """A text document item."""

    uri: str
    language_id: str
    version: int
    text: str


@dataclass(slots=True)
class DidOpenTextDocumentParams:
    """DidOpen notification parameters."""

    text_document: TextDocumentItem


@dataclass(slots=True)
class DidChangeTextDocumentParams:
    """DidChange notification parameters."""

    text_document: dict[str, Any]
    content_changes: list[dict[str, Any]]


@dataclass(slots=True)
class DidCloseTextDocumentParams:
    """DidClose notification parameters."""

    text_document: dict[str, Any]


@dataclass(slots=True)
class DocumentSymbol:
    """A document symbol."""

    name: str
    kind: int
    deprecated: bool = False
    location: dict[str, Any] | None = None
    children: list[DocumentSymbol] | None = None


@dataclass(slots=True)
class DocumentSymbolParams:
    """Document symbol request parameters."""

    text_document: dict[str, Any]


@dataclass(slots=True)
class Diagnostic:
    """A diagnostic result."""

    range: dict[str, Any]
    severity: int | None = None
    code: str | int | None = None
    source: str | None = None
    message: str = ""
    tags: list[int] | None = None
    related_information: list[dict[str, Any]] | None = None


@dataclass(slots=True)
class PublishDiagnosticsParams:
    """Publish diagnostics notification parameters."""

    uri: str
    diagnostics: list[Diagnostic]
    version: int | None = None


@dataclass(slots=True)
class CompletionItem:
    """A completion item."""

    label: str
    kind: int | None = None
    detail: str | None = None
    documentation: str | None = None
    sort_text: str | None = None
    filter_text: str | None = None
    insert_text: str | None = None
    insert_text_format: int | None = None


@dataclass(slots=True)
class CompletionParams:
    """Completion request parameters."""

    text_document: dict[str, Any]
    position: dict[str, Any]
    context: dict[str, Any] | None = None


@dataclass(slots=True)
class Hover:
    """A hover result."""

    contents: dict[str, Any]
    range: dict[str, Any] | None = None


@dataclass(slots=True)
class HoverParams:
    """Hover request parameters."""

    text_document: dict[str, Any]
    position: dict[str, Any]


@dataclass(slots=True)
class Location:
    """A source code location."""

    uri: str
    range: dict[str, Any]


@dataclass(slots=True)
class DefinitionParams:
    """Definition request parameters."""

    text_document: dict[str, Any]
    position: dict[str, Any]


@dataclass(slots=True)
class ReferenceParams:
    """Reference request parameters."""

    text_document: dict[str, Any]
    position: dict[str, Any]
    context: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "textDocument": self.text_document,
            "position": self.position,
        }
        if self.context is not None:
            d["context"] = self.context
        return d


# ============================================================================
# Framing
# ============================================================================

_CONTENT_LENGTH = b"Content-Length: "


def frame_message(message: LSPMessage) -> bytes:
    """Encode an LSP message into the wire format.

    The wire format is HTTP-like headers followed by a blank line and the
    JSON-RPC body:

        Content-Length: N\r\n
        \r\n
        {...json...}
    """

    body = json.dumps(message.to_dict(), separators=(",", ":")).encode("utf-8")
    headers = b"Content-Length: %d\r\n\r\n" % len(body)
    return headers + body


def parse_message(data: bytes) -> LSPMessage | None:
    """Parse one LSP message from raw bytes.

    Returns None if the data does not contain a complete message.
    """

    try:
        header_end = data.index(b"\r\n\r\n")
    except ValueError:
        return None

    header_block = data[:header_end].decode("utf-8")
    body_start = header_end + 4

    content_length = 0
    for line in header_block.split("\r\n"):
        if line.lower().startswith("content-length:"):
            try:
                content_length = int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
            break

    if len(data) < body_start + content_length:
        return None

    body = data[body_start : body_start + content_length]
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None

    msg = LSPMessage()
    msg.jsonrpc = payload.get("jsonrpc", "2.0")
    msg.method = payload.get("method")
    msg.params = payload.get("params")
    msg.result = payload.get("result")
    msg.error = payload.get("error")
    msg.id = payload.get("id")
    return msg


__all__ += [
    "ERROR_INTERNAL_ERROR",
    "ERROR_INVALID_PARAMS",
    "ERROR_INVALID_REQUEST",
    "ERROR_METHOD_NOT_FOUND",
    "ERROR_PARSE_ERROR",
    "ERROR_SERVER_ERROR_END",
    "ERROR_SERVER_ERROR_START",
    "ERROR_SERVER_NOT_INITIALIZED",
    "ERROR_UNKNOWN_ERROR_CODE",
]
