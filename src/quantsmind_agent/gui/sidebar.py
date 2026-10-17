"""Sidebar: project selection and model/provider summary.

The sidebar owns no provider logic. It reads and writes configuration through
:class:`~quantsmind_agent.config.ConfigManager` and forwards user intent to the
main window through signals.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.version import APP_NAME, __version__

__all__ = ["Sidebar"]

_ROLE_LABEL = "<span style='color:#6a7280;font-size:11px'>{}</span>"


class Sidebar(QWidget):
    """Left-hand panel: workspace, model, status."""

    open_workspace_requested = Signal()
    close_workspace_requested = Signal()
    settings_requested = Signal()
    new_conversation_requested = Signal()
    #: A stored conversation was chosen from the list, by id.
    conversation_selected = Signal(str)
    #: The delete button asked for the conversation currently listed, by id.
    conversation_delete_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(260)
        self.setMaximumWidth(420)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        #: Directory chosen by the last folder dialog (``""`` when cancelled).
        self.chosen_directory: str = ""
        self._build()

    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 12, 10, 12)
        outer.setSpacing(10)

        title = QLabel(f"{APP_NAME} <span style='color:#6a7280'>v{__version__}</span>")
        title.setStyleSheet("font-size:15px;font-weight:700;")
        outer.addWidget(title)

        outer.addWidget(self._workspace_box())
        outer.addWidget(self._model_box())
        outer.addWidget(self._session_box())

        self._status = QLabel("Ready")
        self._status.setObjectName("StatusText")
        self._status.setWordWrap(True)
        self._status.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        outer.addStretch(1)
        outer.addWidget(self._status)

        self._error = QLabel("")
        self._error.setObjectName("ErrorText")
        self._error.setWordWrap(True)
        self._error.setVisible(False)
        outer.addWidget(self._error)

    # -- workspace ---------------------------------------------------------
    def _workspace_box(self) -> QGroupBox:
        box = QGroupBox("Workspace")
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 8, 10, 10)
        grid.setVerticalSpacing(6)

        label = QLabel("Project folder")
        label.setObjectName("SectionLabel")
        grid.addWidget(label, 0, 0, 1, 2)

        self._workspace_value = QLabel("No project open")
        self._workspace_value.setWordWrap(True)
        self._workspace_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        grid.addWidget(self._workspace_value, 1, 0, 1, 2)

        self._open_button = QPushButton("Open project\u2026")
        self._open_button.setToolTip("Select the local folder the agent may inspect")
        self._open_button.clicked.connect(self._on_open_clicked)
        grid.addWidget(self._open_button, 2, 0, 1, 2)

        self._clear_button = QPushButton("Close project")
        self._clear_button.setEnabled(False)
        self._clear_button.clicked.connect(self.close_workspace_requested.emit)
        grid.addWidget(self._clear_button, 3, 0, 1, 2)
        return box

    def _model_box(self) -> QGroupBox:
        box = QGroupBox("Model")
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 8, 10, 10)
        grid.setVerticalSpacing(6)

        self._model_value = QLabel("\u2014")
        self._model_value.setWordWrap(True)
        grid.addWidget(self._model_value, 0, 0, 1, 2)

        self._endpoint_value = QLabel("")
        self._endpoint_value.setStyleSheet("color:#6a7280;font-size:11px;")
        self._endpoint_value.setWordWrap(True)
        grid.addWidget(self._endpoint_value, 1, 0, 1, 2)

        self._api_key_value = QLabel("")
        self._api_key_value.setStyleSheet("color:#6a7280;font-size:11px;")
        grid.addWidget(self._api_key_value, 2, 0, 1, 2)

        self._settings_button = QPushButton("Settings\u2026")
        self._settings_button.setToolTip("Provider, model, API key and agent limits")
        self._settings_button.clicked.connect(self.settings_requested.emit)
        grid.addWidget(self._settings_button, 3, 0, 1, 2)
        return box

    def _session_box(self) -> QGroupBox:
        box = QGroupBox("Session")
        grid = QGridLayout(box)
        grid.setContentsMargins(10, 8, 10, 10)
        grid.setVerticalSpacing(6)

        self._session_value = QLabel("0 messages")
        self._session_value.setStyleSheet("color:#6a7280;font-size:11px;")
        grid.addWidget(self._session_value, 0, 0, 1, 2)

        #: Stored conversations, newest first. The active one is marked rather
        #: than preselected, so choosing an entry always means "switch to it".
        self._conversation_combo = QComboBox()
        self._conversation_combo.setToolTip("Reopen a previous conversation")
        self._conversation_combo.currentIndexChanged.connect(self._on_conversation_chosen)
        grid.addWidget(self._conversation_combo, 1, 0, 1, 2)

        buttons = QHBoxLayout()
        new_button = QPushButton("New")
        new_button.clicked.connect(self.new_conversation_requested.emit)
        buttons.addWidget(new_button)
        delete_button = QPushButton("Delete")
        delete_button.setToolTip("Delete the listed conversation")
        delete_button.clicked.connect(self._on_delete_clicked)
        buttons.addWidget(delete_button)
        grid.addLayout(buttons, 2, 0, 1, 2)

        self._conversation_ids: list[str] = []
        return box

    def _on_conversation_chosen(self, index: int) -> None:
        if 0 <= index < len(self._conversation_ids):
            self.conversation_selected.emit(self._conversation_ids[index])

    def _on_delete_clicked(self) -> None:
        index = self._conversation_combo.currentIndex()
        if 0 <= index < len(self._conversation_ids):
            self.conversation_delete_requested.emit(self._conversation_ids[index])

    # -- conversations -----------------------------------------------------
    def set_conversations(self, entries: list[tuple[str, str]], *, active_id: str = "") -> None:
        """Show stored conversations as ``(id, label)`` pairs.

        The list is only rebuilt when the set of ids changes, so refreshing it
        after every turn does not make the user fight a moving dropdown.
        """

        ids = [entry[0] for entry in entries]
        if ids == self._conversation_ids:
            self._mark_active(active_id)
            return

        self._conversation_ids = ids
        blocked = self._conversation_combo.blockSignals(True)
        self._conversation_combo.clear()
        for conversation_id, label in entries:
            self._conversation_combo.addItem(label, conversation_id)
        self._conversation_combo.blockSignals(blocked)
        self._mark_active(active_id)

    def _mark_active(self, active_id: str) -> None:
        if active_id and active_id in self._conversation_ids:
            self._conversation_combo.setCurrentIndex(self._conversation_ids.index(active_id))

    def conversations_available(self) -> bool:
        return bool(self._conversation_ids)

    # -- public API --------------------------------------------------------
    def set_workspace(self, path: str | None) -> None:
        """Show the active project (or the empty state)."""

        if path:
            self._workspace_value.setText(path)
            self._workspace_value.setToolTip(path)
            self._clear_button.setEnabled(True)
        else:
            self._workspace_value.setText("No project open")
            self._workspace_value.setToolTip("")
            self._clear_button.setEnabled(False)

    def set_model(self, provider: str, model: str, base_url: str, masked_key: str) -> None:
        """Show the configured model, endpoint and masked credential."""

        self._model_value.setText(f"{model}")
        self._model_value.setToolTip(f"Provider: {provider}")
        self._endpoint_value.setText(f"{provider} \u00b7 {base_url}")
        self._api_key_value.setText(f"API key: {masked_key}")
        self._api_key_value.setToolTip("The key is stored in your OS credential store.")

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_error(self, text: str = "") -> None:
        self._error.setText(text)
        self._error.setVisible(bool(text))

    def set_session_counts(self, messages: int, turns: int, tools: int) -> None:
        self._session_value.setText(
            f"{messages} messages \u00b7 {turns} turns \u00b7 {tools} tool calls"
        )

    # -- internals ---------------------------------------------------------
    def _on_open_clicked(self) -> None:
        start_at = self._workspace_value.toolTip()
        dialog = QFileDialog(self)
        dialog.setWindowTitle("Open project folder")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        if start_at:
            dialog.setDirectory(start_at)
        if dialog.exec():
            chosen = dialog.selectedFiles()
            if chosen:
                self.open_workspace_requested.emit()
                self.chosen_directory = chosen[0]
        else:
            self.chosen_directory = ""

    def choose_directory(self, start_at: str = "") -> str:
        """Open the folder picker; returns the selection (or an empty string)."""

        dialog = QFileDialog(self)
        dialog.setWindowTitle("Open project folder")
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        if start_at:
            dialog.setDirectory(start_at)
        if dialog.exec():
            chosen = dialog.selectedFiles()
            if chosen:
                return chosen[0]
        return ""

    def set_enabled(self, enabled: bool) -> None:
        """Disable project switching while a turn is running."""

        self._open_button.setEnabled(enabled)
        self._clear_button.setEnabled(enabled and bool(self._workspace_value.toolTip()))
