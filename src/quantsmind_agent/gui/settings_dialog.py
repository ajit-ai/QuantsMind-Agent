"""Settings dialog: provider, model, credential and agent limits.

The dialog reads and writes :class:`~quantsmind_agent.config.Configuration` and
stores credentials through :class:`~quantsmind_agent.config.ConfigManager`, so a
saved API key is never rendered in plain text again. The key field echoes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.config import PROVIDER_PRESETS, ConfigManager, Configuration, LogLevel
from quantsmind_agent.logging_setup import get_logger
from quantsmind_agent.version import APP_NAME, __version__

__all__ = ["SettingsDialog"]

logger = get_logger(__name__)


class SettingsDialog(QDialog):
    """Modal editor for provider/model configuration."""

    def __init__(
        self,
        config_manager: ConfigManager,
        *,
        parent: QWidget | None = None,
        available_models: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = config_manager
        self._available_models = available_models or []
        self.setWindowTitle(f"{APP_NAME} \u2014 Settings")
        self.setMinimumWidth(540)
        self.setModal(True)
        self._build()
        self.load_from_config()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        tabs = QTabWidget()
        tabs.addTab(self._provider_tab(), "Provider")
        tabs.addTab(self._agent_tab(), "Agent")
        tabs.addTab(self._about_tab(), "About")
        outer.addWidget(tabs)

        self._hint = QLabel("")
        self._hint.setStyleSheet("color:#6a7280;font-size:11px;")
        self._hint.setWordWrap(True)
        outer.addWidget(self._hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _provider_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        preset_box = QGroupBox("Preset endpoints")
        preset_layout = QFormLayout(preset_box)
        self._preset = QComboBox()
        self._preset.addItem("Choose a preset\u2026")
        for name in PROVIDER_PRESETS:
            self._preset.addItem(name)
        self._preset.currentIndexChanged.connect(self._on_preset_selected)
        preset_layout.addRow("Provider preset", self._preset)

        form = QFormLayout()
        self._model = QLineEdit()
        self._model.setPlaceholderText("gpt-4o-mini")
        form.addRow("Model", self._model)

        self._model_picker: QComboBox | None = None
        if self._available_models:
            picker = QComboBox()
            picker.setEditable(True)
            picker.addItems(self._available_models)
            picker.currentTextChanged.connect(self._model.setText)
            self._model.textChanged.connect(picker.setCurrentText)
            self._model_picker = picker
            form.addRow("Available on endpoint", picker)

        self._base_url = QLineEdit()
        self._base_url.setPlaceholderText("https://api.openai.com/v1")
        form.addRow("Base URL", self._base_url)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("Paste your API key (saved securely)")
        self._api_key.setText("")
        form.addRow("API key", self._api_key)

        key_row = QHBoxLayout()
        self._key_status = QLabel("No key stored")
        self._key_status.setStyleSheet("color:#6a7280;font-size:11px;")
        clear_button = QPushButton("Clear stored key")
        clear_button.clicked.connect(self._on_clear_key)
        key_row.addWidget(self._key_status)
        key_row.addStretch(1)
        key_row.addWidget(clear_button)
        form.addRow("", key_row)

        self._env_var = QLineEdit()
        self._env_var.setPlaceholderText("QM_API_KEY")
        form.addRow("Key from environment variable", self._env_var)

        self._timeout = QSpinBox()
        self._timeout.setRange(5, 3600)
        self._timeout.setSuffix(" s")
        form.addRow("Request timeout", self._timeout)

        self._max_tokens = QSpinBox()
        self._max_tokens.setRange(256, 200_000)
        self._max_tokens.setSingleStep(256)
        form.addRow("Max output tokens", self._max_tokens)

        self._temperature = QSpinBox()
        self._temperature.setRange(0, 20)
        self._temperature.setSuffix(" (x0.1)")
        form.addRow("Temperature", self._temperature)

        layout.addWidget(preset_box)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _agent_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        form = QFormLayout()
        self._max_iterations = QSpinBox()
        self._max_iterations.setRange(1, 50)
        form.addRow("Max agent iterations", self._max_iterations)

        self._max_file_kb = QSpinBox()
        self._max_file_kb.setRange(1, 20_000)
        self._max_file_kb.setSuffix(" KB")
        form.addRow("Read tool file cap", self._max_file_kb)

        self._history_turns = QSpinBox()
        self._history_turns.setRange(0, 200)
        form.addRow("Conversation turns kept", self._history_turns)

        self._auto_approve = QCheckBox("Allow workspace reads without prompting")
        self._auto_approve.setToolTip(
            "Workspace reads never leave your machine; writes, commands, git and "
            "network access are not available in v0.1."
        )
        form.addRow("", self._auto_approve)

        self._debug = QCheckBox("Developer mode (verbose logs)")
        form.addRow("", self._debug)

        layout.addLayout(form)

        prompt_box = QGroupBox("Extra agent instructions")
        prompt_layout = QVBoxLayout(prompt_box)
        self._system_prompt = QLineEdit()
        self._system_prompt.setPlaceholderText(
            "e.g. Always answer in Spanish. Prefer TypeScript examples."
        )
        prompt_layout.addWidget(self._system_prompt)
        layout.addWidget(prompt_box)
        layout.addStretch(1)
        return page

    def _about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(QLabel(f"<b>{APP_NAME} {__version__}</b>"))
        layout.addWidget(
            QLabel("An AI coding-agent platform inspired by modern coding-agent workflows.")
        )
        self._paths_label = QLabel("")
        self._paths_label.setStyleSheet("color:#6a7280;font-size:11px;")
        self._paths_label.setWordWrap(True)
        self._paths_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._paths_label)
        layout.addStretch(1)
        return page

    # -- data flow ---------------------------------------------------------
    def load_from_config(self) -> None:
        """Populate the widgets from the saved configuration."""

        config = self._manager.config
        provider = config.provider
        self._model.setText(provider.model)
        self._base_url.setText(provider.base_url)
        self._env_var.setText(provider.api_key_env)
        self._timeout.setValue(round(provider.timeout_seconds))
        self._max_tokens.setValue(int(provider.max_output_tokens))
        self._temperature.setValue(round(provider.temperature * 10))
        self._max_iterations.setValue(config.agent.max_iterations)
        self._max_file_kb.setValue(max(1, config.agent.max_file_bytes // 1024))
        self._history_turns.setValue(config.agent.history_turns)
        self._auto_approve.setChecked(config.agent.auto_approve_reads)
        self._system_prompt.setText(config.agent.system_prompt)
        self._debug.setChecked(config.logging.level is LogLevel.DEBUG)
        self._key_status.setText(f"Stored key: {self._manager.masked_api_key()}")
        self._paths_label.setText(
            f"Config file: {self._manager.config_file}<br/>"
            f"Log file: {self._manager.paths.log_file}<br/>"
            "Credentials stay in your OS credential store and are never written to Git."
        )
        self._refresh_hint()

    def to_configuration(self) -> Configuration:
        """Build a validated :class:`Configuration` from the widgets."""

        config = self._manager.config.model_copy(deep=True)
        config.provider.model = self._model.text().strip()
        config.provider.base_url = self._base_url.text().strip()
        config.provider.api_key_env = self._env_var.text().strip() or "QM_API_KEY"
        config.provider.timeout_seconds = float(self._timeout.value())
        config.provider.max_output_tokens = int(self._max_tokens.value())
        config.provider.temperature = self._temperature.value() / 10.0
        config.agent.max_iterations = int(self._max_iterations.value())
        config.agent.max_file_bytes = int(self._max_file_kb.value()) * 1024
        config.agent.history_turns = int(self._history_turns.value())
        config.agent.auto_approve_reads = self._auto_approve.isChecked()
        config.agent.system_prompt = self._system_prompt.text()
        config.logging.level = LogLevel.DEBUG if self._debug.isChecked() else LogLevel.INFO
        # Re-validate so the dialog surfaces field errors instead of saving junk.
        return Configuration.from_dict(config.to_dict())

    # -- handlers ----------------------------------------------------------
    def _on_preset_selected(self, index: int) -> None:
        if index <= 0:
            return
        preset = PROVIDER_PRESETS.get(self._preset.itemText(index))
        if not preset:
            return
        self._base_url.setText(preset["base_url"])
        self._model.setText(preset["model"])
        self._refresh_hint()

    def _on_clear_key(self) -> None:
        removed = self._manager.clear_api_key()
        self._key_status.setText("Stored key removed" if removed else "No key was stored")
        self._api_key.clear()

    def _on_save(self) -> None:
        from quantsmind_agent.errors import QuantsMindError

        try:
            config = self.to_configuration()
        except QuantsMindError as exc:
            QMessageBox.warning(self, "Invalid settings", exc.user_message)
            return

        key = self._api_key.text().strip()
        if key:
            try:
                self._manager.set_api_key(key)
            except QuantsMindError as exc:
                QMessageBox.warning(self, "Could not store the key", exc.user_message)
                return
            finally:
                self._api_key.clear()

        try:
            self._manager.save(config)
        except OSError as exc:
            logger.warning("Failed to save configuration: %s", exc)
            QMessageBox.warning(
                self,
                "Could not save settings",
                f"Writing the configuration file failed:\n{exc}",
            )
            return

        self._key_status.setText(f"Stored key: {self._manager.masked_api_key()}")
        self.accept()

    def _refresh_hint(self) -> None:
        config = self._manager.config
        if not config.provider.requires_api_key():
            self._hint.setText(
                "This endpoint looks like a local server, so no API key is required."
            )
        elif self._manager.has_api_key():
            self._hint.setText("A credential is configured. It is never shown or logged.")
        else:
            self._hint.setText(
                "No API key yet. Paste one above, or set the environment variable "
                f"{config.provider.api_key_env} before starting the app."
            )
