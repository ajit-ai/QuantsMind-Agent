"""Quick open: find a workspace file by name.

The walk runs on a worker thread because a workspace can be large and the GUI
thread must stay responsive while the user keeps typing. Each keystroke cancels
the previous walk cooperatively; results carry the query they belong to, so a
late answer can never replace a newer one.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterable
from typing import cast

from PySide6.QtCore import QThread, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.services.workspace.explorer import ExplorerService, QuickOpenResult

__all__ = ["QuickOpenDialog", "QuickOpenWorker"]

logger = get_logger(__name__)

_DEBOUNCE_MS = 120


class QuickOpenWorker(QThread):
    """One bounded filename walk. Never touches a widget."""

    completed = Signal(str, object)  # query, list[QuickOpenResult]
    failed = Signal(str, str)  # query, user-facing message

    def __init__(
        self,
        service: ExplorerService,
        query: str,
        recent: Iterable[str],
        *,
        limit: int = 100,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._query = query
        self._recent = tuple(recent)
        self._limit = limit
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            results = self._service.quick_open(
                self._query,
                limit=self._limit,
                cancel=self.cancel_event,
                recent=self._recent,
            )
        except Exception as exc:  # a worker must report, not crash the app
            logger.info("Quick open failed: %s", exc)
            self.failed.emit(self._query, "The project could not be searched for files.")
            return
        if not self.cancel_event.is_set():
            self.completed.emit(self._query, results)


class QuickOpenDialog(QDialog):
    """Type a name, choose a file. Emits the workspace-relative path."""

    file_selected = Signal(str)
    results_changed = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service: ExplorerService | None = None
        self._recent: tuple[str, ...] = ()
        self._workers: set[QuickOpenWorker] = set()
        self._current: str = ""
        self._closed = False
        self.setWindowTitle("Quick open")
        self.setModal(True)
        self.resize(700, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)
        self.input = QLineEdit()
        self.input.setPlaceholderText("Type part of a file name\u2026")
        self.input.textChanged.connect(self._on_text_changed)
        self.input.returnPressed.connect(self.open_current)
        layout.addWidget(self.input)

        self.results = QListWidget()
        self.results.itemActivated.connect(lambda _item: self.open_current())
        self.results.itemClicked.connect(lambda _item: self.open_current())
        layout.addWidget(self.results, 1)

        self.status = QLabel("")
        self.status.setObjectName("StatusText")
        layout.addWidget(self.status)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(_DEBOUNCE_MS)
        self._debounce.timeout.connect(lambda: self._start(self.input.text()))
        self.finished.connect(lambda _code: self.shutdown())

    # -- workspace ---------------------------------------------------------
    def set_workspace(
        self,
        service: ExplorerService | None,
        recent: Iterable[str] = (),
    ) -> None:
        self._service = service
        self._recent = tuple(recent)
        if service is None:
            self.status.setText("Open a folder before using quick open.")
            self.results.clear()
            return
        self._start("")

    def shutdown(self) -> None:
        """Cancel work and detach, without blocking on a live widget."""

        self._closed = True
        self._debounce.stop()
        for worker in list(self._workers):
            worker.cancel()
        for worker in list(self._workers):
            # Disconnect first so a queued completion cannot reach a widget
            # that is already being torn down.
            for signal in (worker.completed, worker.failed, worker.finished):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect()
            worker.wait(2000)
            worker.deleteLater()
        self._workers.clear()

    # -- searching ---------------------------------------------------------
    def _on_text_changed(self, _text: str) -> None:
        self._debounce.start()

    def search_now(self, text: str) -> None:
        """Start a search immediately, bypassing the debounce (used by tests)."""

        if self.input.text() != text:
            blocked = self.input.blockSignals(True)
            self.input.setText(text)
            self.input.blockSignals(blocked)
        self._debounce.stop()
        self._start(text)

    def _start(self, query: str) -> None:
        if self._service is None:
            self.status.setText("Open a folder before using quick open.")
            return
        for worker in list(self._workers):
            worker.cancel()
        self._current = query
        worker = QuickOpenWorker(self._service, query, self._recent, parent=None)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(lambda bound=worker: self._release(bound))
        self._workers.add(worker)
        self.status.setText("Searching\u2026")
        worker.start()

    def _release(self, worker: QuickOpenWorker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    def _on_completed(self, query: str, results: object) -> None:
        if self._closed or query != self._current:
            return
        rows: list[QuickOpenResult] = list(results) if isinstance(results, list) else []
        self._populate(rows)
        self.results_changed.emit(rows)

    def _on_failed(self, query: str, message: str) -> None:
        if self._closed or query != self._current:
            return
        self.results.clear()
        self.status.setText(message)
        self.results_changed.emit([])

    def _populate(self, rows: list[QuickOpenResult]) -> None:
        self.results.clear()
        for row in rows:
            item = QListWidgetItem(row.path)
            item.setToolTip(row.path)
            item.setData(_PATH_ROLE, row.path)
            self.results.addItem(item)
        if rows:
            self.results.setCurrentRow(0)
            self.status.setText(f"{len(rows)} file(s). Enter to open, or type a different name.")
        else:
            self.status.setText("No matching file in this project.")

    # -- selection ---------------------------------------------------------
    def current_relative(self) -> str:
        item = cast("QListWidgetItem | None", self.results.currentItem())
        if item is None:
            return ""
        return str(item.data(_PATH_ROLE) or "")

    def result_paths(self) -> list[str]:
        return [
            str(self.results.item(index).data(_PATH_ROLE) or "")
            for index in range(self.results.count())
        ]

    def open_current(self) -> None:
        relative = self.current_relative()
        if not relative:
            return
        self.file_selected.emit(relative)
        self.accept()

    # -- lifecycle ---------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)


#: Item data role holding the workspace-relative path.
_PATH_ROLE = 32  # Qt.ItemDataRole.UserRole
