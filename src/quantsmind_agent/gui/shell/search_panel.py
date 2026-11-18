"""Project text search.

Search runs on a worker thread with a cancellation flag the service checks per
file and per line. The panel never walks the project itself, never claims
semantic or symbol results it cannot produce, and reports truncation instead of
pretending the result list is complete.
"""

from __future__ import annotations

import contextlib
import threading

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import QuantsMindError
from quantsmind_agent.services.workspace.explorer import (
    ExplorerService,
    SearchMatch,
    SearchOutcome,
    SearchQuery,
)

__all__ = ["SearchPanel", "SearchWorker"]

logger = get_logger(__name__)

_PATH_ROLE = int(Qt.ItemDataRole.UserRole)
_LINE_ROLE = _PATH_ROLE + 1


class SearchWorker(QThread):
    """One bounded text search. Never touches a widget."""

    completed = Signal(object)  # SearchOutcome
    failed = Signal(str)

    def __init__(
        self,
        service: ExplorerService,
        query: SearchQuery,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._service = service
        self._query = query
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            outcome = self._service.search_text(self._query, cancel=self.cancel_event)
        except QuantsMindError as exc:
            self.failed.emit(exc.user_message or exc.message)
            return
        except Exception:  # a worker must report, not crash the app
            logger.exception("Text search failed")
            self.failed.emit("The project could not be searched.")
            return
        self.completed.emit(outcome)


class SearchPanel(QWidget):
    """Query, options, grouped results and cancellation."""

    open_requested = Signal(str, int)
    notice = Signal(str)
    search_finished = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._service: ExplorerService | None = None
        self._worker: SearchWorker | None = None
        self._generation = 0
        self._closed = False
        self._build()
        self._set_idle()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        heading = QLabel("Search")
        heading.setObjectName("SectionLabel")
        layout.addWidget(heading)

        self.query_edit = QLineEdit()
        self.query_edit.setPlaceholderText("Search file contents\u2026")
        self.query_edit.returnPressed.connect(self.search_now)
        layout.addWidget(self.query_edit)

        options = QHBoxLayout()
        options.setSpacing(6)
        self.case_box = QCheckBox("Aa")
        self.case_box.setToolTip("Match case")
        self.word_box = QCheckBox("Word")
        self.word_box.setToolTip("Match whole words only")
        self.regex_box = QCheckBox(".*")
        self.regex_box.setToolTip("Use a regular expression")
        for box in (self.case_box, self.word_box, self.regex_box):
            options.addWidget(box)
        options.addStretch(1)
        layout.addLayout(options)

        self.include_edit = QLineEdit()
        self.include_edit.setPlaceholderText("Files to include, e.g. *.py")
        layout.addWidget(self.include_edit)

        self.exclude_edit = QLineEdit()
        self.exclude_edit.setPlaceholderText("Files to exclude, e.g. *.min.js")
        layout.addWidget(self.exclude_edit)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.search_button = QPushButton("Search")
        self.search_button.setObjectName("PrimaryButton")
        self.search_button.clicked.connect(self.search_now)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel)
        buttons.addWidget(self.search_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.status_label = QLabel("Open a folder to search its contents.")
        self.status_label.setObjectName("StatusText")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.results = QTreeWidget()
        self.results.setHeaderHidden(True)
        self.results.setUniformRowHeights(True)
        self.results.itemActivated.connect(self._on_result_activated)
        layout.addWidget(self.results, 1)

    # -- workspace ---------------------------------------------------------
    def set_workspace(self, service: ExplorerService | None) -> None:
        self._cancel_worker()
        self._service = service
        self.results.clear()
        if service is None:
            self.query_edit.setEnabled(False)
            self.search_button.setEnabled(False)
            self.status_label.setText("Open a folder to search its contents.")
        else:
            self.query_edit.setEnabled(True)
            self.search_button.setEnabled(True)
            self.status_label.setText(
                "Search file contents. Ignored project folders are excluded automatically."
            )
        self._set_idle()

    @property
    def service(self) -> ExplorerService | None:
        return self._service

    # -- searching ---------------------------------------------------------
    def query(self) -> SearchQuery:
        return SearchQuery(
            text=self.query_edit.text().strip(),
            include=self.include_edit.text().strip(),
            exclude=self.exclude_edit.text().strip(),
            case_sensitive=self.case_box.isChecked(),
            whole_word=self.word_box.isChecked(),
            use_regex=self.regex_box.isChecked(),
        )

    def search_now(self) -> None:
        if self._service is None:
            self.notice.emit("Open a folder before searching.")
            return
        query = self.query()
        if not query.text:
            self.status_label.setText("Enter text to search for.")
            return
        self._cancel_worker()
        self._generation += 1
        generation = self._generation
        self.results.clear()
        self.status_label.setText(f"Searching for {query.text!r}\u2026")
        self.search_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        worker = SearchWorker(self._service, query, parent=None)
        worker.completed.connect(
            lambda outcome, token=generation: self._on_completed(token, outcome)
        )
        worker.failed.connect(lambda message, token=generation: self._on_failed(token, message))
        worker.finished.connect(lambda bound=worker: self._release(bound))
        self._worker = worker
        worker.start()

    def cancel(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        self.status_label.setText("Cancelling\u2026")
        self.cancel_button.setEnabled(False)

    @property
    def is_searching(self) -> bool:
        return self._worker is not None

    def _cancel_worker(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.cancel()
        for signal in (worker.completed, worker.failed, worker.finished):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect()
        worker.wait(2000)
        worker.deleteLater()

    def _release(self, worker: SearchWorker) -> None:
        if self._worker is worker:
            self._worker = None
        worker.deleteLater()

    def _on_completed(self, generation: int, outcome: object) -> None:
        if self._closed or generation != self._generation:
            return
        self._worker = None
        result = outcome if isinstance(outcome, SearchOutcome) else SearchOutcome()
        self._set_idle()
        self._populate(result.matches)
        if result.cancelled:
            self.status_label.setText("Search cancelled.")
        elif result.truncated:
            self.status_label.setText(
                f"{len(result.matches)} matches shown (limit reached). "
                "Narrow the query or the file filters for a complete view."
            )
        elif result.matches:
            self.status_label.setText(
                f"{len(result.matches)} match(es) in {result.files_scanned} file(s)."
            )
        else:
            self.status_label.setText(f"No matches in {result.files_scanned} file(s).")
        self.search_finished.emit(result)

    def _on_failed(self, generation: int, message: str) -> None:
        if self._closed or generation != self._generation:
            return
        self._worker = None
        self._set_idle()
        self.results.clear()
        self.status_label.setText(message)
        self.notice.emit(message)

    def _set_idle(self) -> None:
        self.search_button.setEnabled(self._service is not None)
        self.cancel_button.setVisible(False)
        self.cancel_button.setEnabled(True)

    # -- results -----------------------------------------------------------
    def _populate(self, matches: list[SearchMatch]) -> None:
        groups: dict[str, QTreeWidgetItem] = {}
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        for match in matches:
            parent = groups.get(match.path)
            if parent is None:
                parent = QTreeWidgetItem(self.results, [match.path])
                parent.setData(0, _PATH_ROLE, match.path)
                parent.setToolTip(0, match.path)
                parent.setIcon(0, icon)
                parent.setExpanded(True)
                groups[match.path] = parent
            child = QTreeWidgetItem(parent, [f"Ln {match.line}: {match.preview.strip()}"])
            child.setData(0, _PATH_ROLE, match.path)
            child.setData(0, _LINE_ROLE, match.line)
            child.setToolTip(0, match.preview)
        if matches:
            first = self.results.topLevelItem(0)
            if first is not None:
                self.results.setCurrentItem(first)

    def result_count(self) -> int:
        total = 0
        for index in range(self.results.topLevelItemCount()):
            item = self.results.topLevelItem(index)
            if item is not None:
                total += item.childCount()
        return total

    def _on_result_activated(self, item: QTreeWidgetItem, _column: int = 0) -> None:
        path = str(item.data(0, _PATH_ROLE) or "")
        if not path:
            return
        line = item.data(0, _LINE_ROLE)
        if line is None:
            item.setExpanded(not item.isExpanded())
            return
        self.open_requested.emit(path, int(line))

    # -- lifecycle ---------------------------------------------------------
    def shutdown(self) -> None:
        self._closed = True
        self._cancel_worker()
        self._generation += 1

    def closeEvent(self, event: QCloseEvent) -> None:
        self.shutdown()
        super().closeEvent(event)
