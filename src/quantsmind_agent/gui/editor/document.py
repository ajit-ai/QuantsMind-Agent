"""Qt-free text document model for the workbench editor.

A document owns what the editor shows and how it is written back: the decoded
text, the encoding and line ending used to encode it, the dirty bit, and the
large/binary/read-only classification. Keeping this layer free of Qt lets the
same rules be tested headlessly and lets a future ChangeSet preflight ask which
buffers are dirty without importing the GUI.

Nothing here displays anything; :class:`~quantsmind_agent.gui.editor.editor_widget`
and :mod:`~quantsmind_agent.gui.editor.tabs` do that.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.persistence.atomic import atomic_write_bytes

__all__ = [
    "BINARY_PROBE_BYTES",
    "LARGE_FILE_BYTES",
    "PREVIEW_LIMIT_BYTES",
    "DocumentConflictError",
    "DocumentError",
    "DocumentInfo",
    "DocumentRegistry",
    "NewlineStyle",
    "TextDocument",
    "TextEncoding",
    "UnsupportedFileError",
]

#: Above this size the editor turns syntax highlighting off and trims history.
LARGE_FILE_BYTES = 5 * 1024 * 1024
#: Above this size the editor opens a bounded, read-only preview.
PREVIEW_LIMIT_BYTES = 20 * 1024 * 1024
#: Bytes inspected for NUL when deciding whether a file is binary.
BINARY_PROBE_BYTES = 8192


class DocumentError(QuantsMindError):
    """A document could not be opened, changed or saved."""

    code = "QM_EDITOR_DOCUMENT"


class DocumentConflictError(DocumentError):
    """The request would discard unsaved text."""

    code = "QM_EDITOR_CONFLICT"


class UnsupportedFileError(DocumentError):
    """The file is not text this editor can honestly edit."""

    code = "QM_EDITOR_UNSUPPORTED"


class TextEncoding(StrEnum):
    UTF8 = "utf-8"
    UTF8_BOM = "utf-8-bom"
    UTF16_LE = "utf-16-le"
    UTF16_BE = "utf-16-be"

    @property
    def label(self) -> str:
        return {
            TextEncoding.UTF8: "UTF-8",
            TextEncoding.UTF8_BOM: "UTF-8 BOM",
            TextEncoding.UTF16_LE: "UTF-16 LE",
            TextEncoding.UTF16_BE: "UTF-16 BE",
        }[self]

    @property
    def codec(self) -> str:
        return {
            TextEncoding.UTF8: "utf-8",
            TextEncoding.UTF8_BOM: "utf-8",
            TextEncoding.UTF16_LE: "utf-16-le",
            TextEncoding.UTF16_BE: "utf-16-be",
        }[self]

    @property
    def bom(self) -> bytes:
        return {
            TextEncoding.UTF8: b"",
            TextEncoding.UTF8_BOM: b"\xef\xbb\xbf",
            TextEncoding.UTF16_LE: b"\xff\xfe",
            TextEncoding.UTF16_BE: b"\xfe\xff",
        }[self]


class NewlineStyle(StrEnum):
    LF = "lf"
    CRLF = "crlf"

    @property
    def label(self) -> str:
        return "CRLF" if self is NewlineStyle.CRLF else "LF"

    @property
    def sequence(self) -> str:
        return "\r\n" if self is NewlineStyle.CRLF else "\n"


@dataclass(frozen=True, slots=True)
class DocumentInfo:
    """A snapshot of one open document, safe to hand across layers."""

    path: str
    relative: str
    dirty: bool
    encoding: str
    newline: str
    language: str = ""

    @property
    def name(self) -> str:
        return Path(self.path).name


def _decode(raw: bytes, *, tolerant: bool = False) -> tuple[TextEncoding, str]:
    errors = "ignore" if tolerant else "strict"
    if raw.startswith(b"\xef\xbb\xbf"):
        return TextEncoding.UTF8_BOM, raw[3:].decode("utf-8", errors=errors)
    if raw.startswith(b"\xff\xfe") and not raw.startswith(b"\xff\xfe\x00\x00"):
        return TextEncoding.UTF16_LE, raw[2:].decode("utf-16-le", errors=errors)
    if raw.startswith(b"\xfe\xff"):
        return TextEncoding.UTF16_BE, raw[2:].decode("utf-16-be", errors=errors)
    return TextEncoding.UTF8, raw.decode("utf-8", errors=errors)


def _newline_style(text: str) -> NewlineStyle:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return NewlineStyle.CRLF if crlf > lf else NewlineStyle.LF


def _normalised(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text_bom(sample: bytes) -> bool:
    return (
        sample.startswith(b"\xef\xbb\xbf")
        or sample.startswith(b"\xff\xfe")
        or sample.startswith(b"\xfe\xff")
    )


def _looks_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            sample = handle.read(BINARY_PROBE_BYTES)
    except OSError:
        return True
    # UTF-16 text contains NUL bytes; a recognised BOM decides first.
    if _text_bom(sample):
        return False
    return b"\0" in sample


class TextDocument:
    """One file as the editor knows it."""

    def __init__(
        self,
        path: Path,
        *,
        relative: str = "",
        text: str = "",
        encoding: TextEncoding = TextEncoding.UTF8,
        newline: NewlineStyle = NewlineStyle.LF,
        exists: bool = True,
        readonly: bool = False,
        binary: bool = False,
        unsupported: bool = False,
        large: bool = False,
        preview_only: bool = False,
        truncated: bool = False,
        size_bytes: int = 0,
        mtime_ns: int = 0,
        digest: str = "",
        mode: int | None = None,
        reason: str = "",
    ) -> None:
        self.path = Path(path)
        self.relative = relative
        self.text = text
        self.encoding = encoding
        self.newline = newline
        self.exists = exists
        self.readonly = readonly
        self.binary = binary
        self.unsupported = unsupported
        self.large = large
        self.preview_only = preview_only
        self.truncated = truncated
        self.size_bytes = size_bytes
        self.mtime_ns = mtime_ns
        self.digest = digest
        self.mode = mode
        self.reason = reason
        self.dirty = False

    # -- construction ------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: Path | str,
        *,
        relative: str = "",
        force_editable: bool = False,
    ) -> TextDocument:
        """Read ``path`` and classify it.

        ``force_editable`` is the explicit "Open editable anyway" action for a
        file above the preview limit; it lifts the read-only preview but the
        large-file mode (and therefore reduced features) remains.
        """

        path = Path(path)
        try:
            info = path.stat()
        except OSError as exc:
            raise DocumentError(
                f"The file could not be opened: {path.name}",
                user_message=f"{path.name} could not be opened: {exc.strerror or exc}",
                cause=exc,
            ) from exc
        if not path.is_file():
            raise DocumentError(
                f"Not a regular file: {path}",
                user_message=f"{path.name} is not a regular file.",
            )

        large = info.st_size > LARGE_FILE_BYTES
        binary = _looks_binary(path)
        writable = os.access(path, os.W_OK)
        mode = stat.S_IMODE(info.st_mode)

        if binary:
            return cls(
                path,
                relative=relative,
                exists=True,
                readonly=True,
                binary=True,
                large=large,
                size_bytes=info.st_size,
                mtime_ns=info.st_mtime_ns,
                digest=_hash_file(path),
                mode=mode,
                reason="This looks like a binary file, so it is shown as a summary.",
            )

        preview_only = info.st_size > PREVIEW_LIMIT_BYTES and not force_editable
        try:
            if preview_only:
                with path.open("rb") as handle:
                    raw = handle.read(PREVIEW_LIMIT_BYTES)
                truncated = info.st_size > len(raw)
            else:
                raw = path.read_bytes()
                truncated = False
            encoding, decoded = _decode(raw, tolerant=truncated)
        except UnicodeDecodeError:
            return cls(
                path,
                relative=relative,
                exists=True,
                readonly=True,
                unsupported=True,
                large=large,
                size_bytes=info.st_size,
                mtime_ns=info.st_mtime_ns,
                digest=_hash_file(path),
                mode=mode,
                reason="This file is not UTF-8 or UTF-16 text, so it is shown as a summary.",
            )
        except OSError as exc:
            raise DocumentError(
                f"The file could not be read: {path.name}",
                user_message=f"{path.name} could not be read: {exc.strerror or exc}",
                cause=exc,
            ) from exc

        newline = _newline_style(decoded)
        text = _normalised(decoded)
        if preview_only:
            readonly = True
            reason = (
                "This file is larger than 20 MB, so it opened as a read-only preview. "
                "Use Open editable anyway to edit it."
            )
        elif not writable:
            readonly = True
            reason = "This file is not writable by the current process."
        else:
            readonly = False
            reason = ""
        return cls(
            path,
            relative=relative,
            text=text,
            encoding=encoding,
            newline=newline,
            exists=True,
            readonly=readonly,
            large=large,
            preview_only=preview_only,
            truncated=truncated,
            size_bytes=info.st_size,
            mtime_ns=info.st_mtime_ns,
            mode=mode,
            reason=reason,
        )

    @classmethod
    def new(cls, path: Path | str, *, relative: str = "", text: str = "") -> TextDocument:
        """An unsaved buffer for a file that does not exist yet."""

        return cls(Path(path), relative=relative, text=text, exists=False, mode=None)

    # -- content -----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.path.name

    @property
    def line_count(self) -> int:
        return self.text.count("\n") + 1

    @property
    def editable(self) -> bool:
        return not (self.readonly or self.binary or self.unsupported)

    @property
    def highlighting(self) -> bool:
        return not (self.large or self.binary or self.unsupported)

    def mark_dirty(self) -> None:
        self.dirty = True

    def set_text(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self.dirty = True

    def set_encoding(self, encoding: TextEncoding) -> None:
        if encoding is not self.encoding:
            self.encoding = encoding
            self.dirty = True

    def set_newline(self, newline: NewlineStyle) -> None:
        if newline is not self.newline:
            self.newline = newline
            self.dirty = True

    def info(self, *, language: str = "") -> DocumentInfo:
        return DocumentInfo(
            path=str(self.path),
            relative=self.relative,
            dirty=self.dirty,
            encoding=self.encoding.label,
            newline=self.newline.label,
            language=language,
        )

    # -- disk --------------------------------------------------------------
    def has_external_change(self) -> bool:
        try:
            info = self.path.stat()
        except OSError:
            return True
        return info.st_mtime_ns != self.mtime_ns or info.st_size != self.size_bytes

    def reload(self, *, force: bool = False) -> None:
        if self.dirty and not force:
            raise DocumentConflictError(
                "Refusing to reload a dirty buffer.",
                user_message="Save or discard your changes before reloading.",
            )
        fresh = TextDocument.load(self.path, relative=self.relative)
        self._adopt(fresh)

    def save(
        self,
        path: Path | str | None = None,
        *,
        relative: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Write the buffer atomically and return the path written."""

        if self.binary or self.unsupported:
            raise UnsupportedFileError(
                "A binary or unsupported file cannot be saved as text.",
                user_message="This file cannot be saved from the text editor.",
            )
        if self.preview_only and path is None:
            raise DocumentError(
                "The preview is read-only.",
                user_message=self.reason or "Use Open editable anyway first.",
            )
        target = Path(path) if path is not None else self.path
        if target != self.path and target.exists() and not overwrite:
            raise FileExistsError(str(target))

        data = self._encode()
        mode = self.mode if self.exists else None
        try:
            atomic_write_bytes(target, data, mode=mode)
        except OSError as exc:
            raise DocumentError(
                f"The file could not be saved: {target.name}",
                user_message=f"{target.name} could not be saved: {exc.strerror or exc}",
                cause=exc,
            ) from exc

        self.path = target
        if relative is not None:
            self.relative = relative
        self.exists = True
        self.dirty = False
        self.truncated = False
        self.preview_only = False
        self.readonly = False
        self.size_bytes = len(data)
        try:
            info = target.stat()
            self.mtime_ns = info.st_mtime_ns
            self.size_bytes = info.st_size
            self.mode = stat.S_IMODE(info.st_mode)
        except OSError:  # pragma: no cover - the write succeeded
            pass
        return target

    def _encode(self) -> bytes:
        body = self.text.replace("\n", self.newline.sequence)
        return self.encoding.bom + body.encode(self.encoding.codec)

    def _adopt(self, other: TextDocument) -> None:
        self.path = other.path
        self.text = other.text
        self.encoding = other.encoding
        self.newline = other.newline
        self.exists = other.exists
        self.readonly = other.readonly
        self.binary = other.binary
        self.unsupported = other.unsupported
        self.large = other.large
        self.preview_only = other.preview_only
        self.truncated = other.truncated
        self.size_bytes = other.size_bytes
        self.mtime_ns = other.mtime_ns
        self.digest = other.digest
        self.mode = other.mode
        self.reason = other.reason
        self.dirty = False

    def summary(self) -> str:
        """A human-readable description for binary and unsupported files."""

        kind = "Binary file" if self.binary else "Unsupported text file"
        lines = [
            f"{kind}: {self.name}",
            f"Size: {self.size_bytes:,} bytes",
        ]
        if self.digest:
            lines.append(f"SHA-256: {self.digest}")
        if self.reason:
            lines.append(self.reason)
        return "\n".join(lines)


class DocumentRegistry:
    """The set of open documents, keyed by canonical path.

    A single registry is shared by every editor pane, so the same file is never
    represented by two divergent buffers and dirty state has one answer.
    """

    def __init__(self) -> None:
        self._documents: dict[str, TextDocument] = {}

    @staticmethod
    def key(path: Path | str) -> str:
        return os.path.normcase(str(Path(path).resolve(strict=False)))

    def get(self, path: Path | str) -> TextDocument | None:
        return self._documents.get(self.key(path))

    def add(self, document: TextDocument) -> TextDocument:
        existing = self.get(document.path)
        if existing is not None:
            return existing
        self._documents[self.key(document.path)] = document
        return document

    def open(
        self,
        path: Path | str,
        *,
        relative: str = "",
        force_editable: bool = False,
    ) -> TextDocument:
        existing = self.get(path)
        if existing is not None:
            return existing
        return self.add(TextDocument.load(path, relative=relative, force_editable=force_editable))

    def create(self, path: Path | str, *, relative: str = "") -> TextDocument:
        existing = self.get(path)
        if existing is not None:
            return existing
        return self.add(TextDocument.new(path, relative=relative))

    def remove(self, path: Path | str) -> None:
        self._documents.pop(self.key(path), None)

    def documents(self) -> tuple[TextDocument, ...]:
        return tuple(self._documents.values())

    def dirty_documents(self) -> tuple[TextDocument, ...]:
        return tuple(document for document in self._documents.values() if document.dirty)

    def is_dirty(self, relative: str) -> bool:
        return any(
            document.relative == relative and document.dirty
            for document in self._documents.values()
        )

    def dirty_paths(self) -> frozenset[str]:
        return frozenset(
            document.relative for document in self._documents.values() if document.dirty
        )

    def clear(self) -> None:
        self._documents.clear()
