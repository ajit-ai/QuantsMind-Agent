"""Phase 0.10 editor document-model tests.

The model is deliberately Qt-free, so encoding, line endings, file
classification and save/reload rules are tested without a GUI.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from quantsmind_agent.gui.editor import document as document_module
from quantsmind_agent.gui.editor.document import (
    DocumentConflictError,
    DocumentError,
    DocumentRegistry,
    NewlineStyle,
    TextDocument,
    TextEncoding,
    UnsupportedFileError,
)


class TestDecoding:
    def test_plain_utf8(self, tmp_path: Path) -> None:
        target = tmp_path / "notes.txt"
        target.write_bytes(b"alpha\nbeta\n")
        document = TextDocument.load(target)
        assert document.encoding is TextEncoding.UTF8
        assert document.newline is NewlineStyle.LF
        assert document.text == "alpha\nbeta\n"
        assert document.dirty is False
        assert document.editable is True
        assert document.highlighting is True
        assert document.size_bytes == len("alpha\nbeta\n")

    def test_crlf_is_detected_and_preserved(self, tmp_path: Path) -> None:
        target = tmp_path / "windows.txt"
        target.write_bytes(b"one\r\ntwo\r\n")
        document = TextDocument.load(target)
        assert document.newline is NewlineStyle.CRLF
        assert document.text == "one\ntwo\n"
        document.text = "one\ntwo\nthree\n"
        document.save()
        assert target.read_bytes() == b"one\r\ntwo\r\nthree\r\n"

    def test_utf8_bom_roundtrip(self, tmp_path: Path) -> None:
        target = tmp_path / "bom.txt"
        target.write_bytes(b"\xef\xbb\xbfhello\n")
        document = TextDocument.load(target)
        assert document.encoding is TextEncoding.UTF8_BOM
        assert document.text == "hello\n"
        document.text = "hello again\n"
        document.save()
        assert target.read_bytes() == b"\xef\xbb\xbfhello again\n"

    @pytest.mark.parametrize(
        ("encoding", "bom"),
        [(TextEncoding.UTF16_LE, b"\xff\xfe"), (TextEncoding.UTF16_BE, b"\xfe\xff")],
    )
    def test_utf16_roundtrip(self, tmp_path: Path, encoding: TextEncoding, bom: bytes) -> None:
        target = tmp_path / f"{encoding.value}.txt"
        target.write_bytes(bom + "line one\nline two\n".encode(encoding.codec))
        document = TextDocument.load(target)
        assert document.encoding is encoding
        assert document.text == "line one\nline two\n"
        assert document.editable is True
        document.text = "changed\n"
        document.save()
        assert target.read_bytes() == bom + "changed\n".encode(encoding.codec)

    def test_invalid_utf8_is_unsupported_not_corrupted(self, tmp_path: Path) -> None:
        target = tmp_path / "legacy.txt"
        target.write_bytes(b"\x80\x81\x82")
        document = TextDocument.load(target)
        assert document.unsupported is True
        assert document.editable is False
        assert document.highlighting is False
        assert "Unsupported text file" in document.summary()
        with pytest.raises(UnsupportedFileError):
            document.save()

    def test_binary_file_is_summarised_and_read_only(self, tmp_path: Path) -> None:
        target = tmp_path / "blob.dat"
        target.write_bytes(b"\x00\x01\x02payload")
        document = TextDocument.load(target)
        assert document.binary is True
        assert document.readonly is True
        assert document.editable is False
        assert document.highlighting is False
        assert document.digest
        summary = document.summary()
        assert "Binary file" in summary
        assert "SHA-256" in summary
        with pytest.raises(UnsupportedFileError):
            document.save()

    def test_missing_file_raises_document_error(self, tmp_path: Path) -> None:
        with pytest.raises(DocumentError) as info:
            TextDocument.load(tmp_path / "missing.txt")
        assert "could not be opened" in info.value.user_message

    def test_directory_is_not_a_document(self, tmp_path: Path) -> None:
        folder = tmp_path / "folder"
        folder.mkdir()
        with pytest.raises(DocumentError):
            TextDocument.load(folder)


class TestSizeClassification:
    def test_large_file_disables_highlighting(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_module, "LARGE_FILE_BYTES", 10)
        target = tmp_path / "large.txt"
        target.write_text("x" * 50, encoding="utf-8")
        document = TextDocument.load(target)
        assert document.large is True
        assert document.highlighting is False
        assert document.editable is True

    def test_oversized_file_is_preview_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_module, "PREVIEW_LIMIT_BYTES", 10)
        target = tmp_path / "huge.txt"
        target.write_text("y" * 50, encoding="utf-8")
        document = TextDocument.load(target)
        assert document.preview_only is True
        assert document.truncated is True
        assert document.readonly is True
        assert document.editable is False
        assert "read-only preview" in document.reason
        with pytest.raises(DocumentError):
            document.save()

    def test_force_editable_lifts_the_preview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_module, "LARGE_FILE_BYTES", 20)
        monkeypatch.setattr(document_module, "PREVIEW_LIMIT_BYTES", 10)
        target = tmp_path / "huge.txt"
        target.write_text("y" * 50, encoding="utf-8")
        document = TextDocument.load(target, force_editable=True)
        assert document.preview_only is False
        assert document.editable is True
        assert document.large is True

    def test_small_file_is_not_preview(self, tmp_path: Path) -> None:
        target = tmp_path / "small.txt"
        target.write_text("small\n", encoding="utf-8")
        document = TextDocument.load(target)
        assert document.preview_only is False
        assert document.truncated is False


class TestSaving:
    def test_new_buffer_is_written_on_save(self, tmp_path: Path) -> None:
        target = tmp_path / "new.txt"
        document = TextDocument.new(target)
        assert document.exists is False
        document.set_text("written\n")
        dirty_after_edit = document.dirty
        assert dirty_after_edit is True
        assert document.save() == target
        assert target.read_text(encoding="utf-8") == "written\n"
        assert document.dirty is False
        assert document.mtime_ns > 0
        # ``exists`` is checked last: mypy keeps the constructor's literal
        # ``False`` for this attribute after a save, so a later assertion would
        # be reported as unreachable even though it holds at runtime.
        assert document.exists is True

    def test_save_is_atomic_and_leaves_no_temp_file(self, tmp_path: Path) -> None:
        target = tmp_path / "atomic.txt"
        document = TextDocument.new(target)
        document.set_text("payload\n")
        document.save()
        leftovers = [item.name for item in tmp_path.iterdir() if item.name != "atomic.txt"]
        assert leftovers == []

    def test_save_as_refuses_to_overwrite_without_confirmation(self, tmp_path: Path) -> None:
        source = tmp_path / "source.txt"
        source.write_text("source\n", encoding="utf-8")
        target = tmp_path / "target.txt"
        target.write_text("target\n", encoding="utf-8")
        document = TextDocument.load(source)
        document.set_text("changed\n")
        with pytest.raises(FileExistsError):
            document.save(target)
        assert target.read_text(encoding="utf-8") == "target\n"
        document.save(target, overwrite=True)
        assert target.read_text(encoding="utf-8") == "changed\n"

    def test_encoding_and_newline_changes_are_saved(self, tmp_path: Path) -> None:
        target = tmp_path / "encoded.txt"
        target.write_text("alpha\n", encoding="utf-8")
        document = TextDocument.load(target)
        document.set_encoding(TextEncoding.UTF8_BOM)
        document.set_newline(NewlineStyle.CRLF)
        assert document.dirty is True
        document.save()
        assert target.read_bytes() == b"\xef\xbb\xbfalpha\r\n"

    @pytest.mark.skipif(os.name == "nt" and not hasattr(os, "chmod"), reason="no chmod")
    def test_read_only_file_is_not_editable(self, tmp_path: Path) -> None:
        target = tmp_path / "readonly.txt"
        target.write_text("locked\n", encoding="utf-8")
        target.chmod(0o444)
        try:
            document = TextDocument.load(target)
            assert document.readonly is True
            assert document.editable is False
            assert "not writable" in document.reason
        finally:
            target.chmod(0o644)


class TestReloadAndChangeDetection:
    def test_dirty_buffer_refuses_to_reload(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.txt"
        target.write_text("original\n", encoding="utf-8")
        document = TextDocument.load(target)
        document.set_text("edited\n")
        with pytest.raises(DocumentConflictError):
            document.reload()
        assert document.text == "edited\n"

    def test_forced_reload_adopts_disk_content(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.txt"
        target.write_text("original\n", encoding="utf-8")
        document = TextDocument.load(target)
        document.set_text("edited\n")
        target.write_text("from disk\n", encoding="utf-8")
        document.reload(force=True)
        assert document.text == "from disk\n"
        assert document.dirty is False

    def test_external_change_is_detected(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.txt"
        target.write_text("original\n", encoding="utf-8")
        document = TextDocument.load(target)
        assert document.has_external_change() is False
        target.write_text("a longer replacement\n", encoding="utf-8")
        assert document.has_external_change() is True

    def test_deleted_file_counts_as_external_change(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.txt"
        target.write_text("original\n", encoding="utf-8")
        document = TextDocument.load(target)
        target.unlink()
        assert document.has_external_change() is True

    def test_set_text_only_marks_dirty_on_change(self, tmp_path: Path) -> None:
        target = tmp_path / "doc.txt"
        target.write_text("same\n", encoding="utf-8")
        document = TextDocument.load(target)
        document.set_text("same\n")
        assert document.dirty is False
        document.set_text("different\n")
        assert document.dirty is True


class TestDocumentRegistry:
    def test_same_path_is_the_same_document(self, tmp_path: Path) -> None:
        target = tmp_path / "one.txt"
        target.write_text("one\n", encoding="utf-8")
        registry = DocumentRegistry()
        first = registry.open(target, relative="one.txt")
        second = registry.open(target, relative="one.txt")
        assert first is second
        assert len(registry.documents()) == 1

    def test_create_reuses_an_open_document(self, tmp_path: Path) -> None:
        target = tmp_path / "new.txt"
        registry = DocumentRegistry()
        created = registry.create(target, relative="new.txt")
        assert registry.open(target, relative="new.txt") is created
        assert created.exists is False

    def test_dirty_tracking(self, tmp_path: Path) -> None:
        first = tmp_path / "first.txt"
        second = tmp_path / "second.txt"
        first.write_text("first\n", encoding="utf-8")
        second.write_text("second\n", encoding="utf-8")
        registry = DocumentRegistry()
        registry.open(first, relative="first.txt")
        second_document = registry.open(second, relative="second.txt")
        assert registry.dirty_paths() == frozenset()
        second_document.set_text("changed\n")
        assert registry.is_dirty("second.txt") is True
        assert registry.dirty_paths() == frozenset({"second.txt"})
        assert [item.name for item in registry.dirty_documents()] == ["second.txt"]

    def test_remove_and_clear(self, tmp_path: Path) -> None:
        target = tmp_path / "one.txt"
        target.write_text("one\n", encoding="utf-8")
        registry = DocumentRegistry()
        registry.open(target)
        registry.remove(target)
        assert registry.get(target) is None
        registry.open(target)
        registry.clear()
        assert registry.documents() == ()

    def test_key_is_case_insensitive_on_windows(self, tmp_path: Path) -> None:
        target = tmp_path / "Case.txt"
        target.write_text("x\n", encoding="utf-8")
        registry = DocumentRegistry()
        document = registry.open(target)
        upper = str(target).upper()
        if os.name == "nt":
            assert registry.get(upper) is document
        else:
            assert registry.get(upper) is None

    def test_info_reports_display_state(self, tmp_path: Path) -> None:
        target = tmp_path / "info.py"
        target.write_text("x = 1\r\n", encoding="utf-8")
        document = TextDocument.load(target)
        info = document.info(language="Python")
        assert info.path == str(target)
        assert info.encoding == "UTF-8"
        assert info.newline == "CRLF"
        assert info.language == "Python"
        assert info.name == "info.py"
        assert info.dirty is False
