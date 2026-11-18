"""Settings dialog: provider, model, credential and agent limits.

The dialog reads and writes :class:`~quantsmind_agent.app.settings.Configuration` and
stores credentials through :class:`~quantsmind_agent.app.settings.ConfigManager`, so a
saved API key is never rendered in plain text again. The key field echoes.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.app.settings import (
    PROVIDER_PRESETS,
    ConfigManager,
    Configuration,
    LogLevel,
    ProviderSettings,
)
from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.errors import ConfigurationError, QuantsMindError
from quantsmind_agent.gui.shell.style import COLORS
from quantsmind_agent.security.credential_resolution import (
    CredentialResolution,
    CredentialSource,
)
from quantsmind_agent.security.credential_resolution import (
    CredentialStatus as ResolutionStatus,
)
from quantsmind_agent.security.provider_identity import (
    EndpointPolicyError,
    canonicalize_endpoint,
    rebind_explicit_provider_id,
)
from quantsmind_agent.security.secrets import StorageStatus
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
        security: Any | None = None,
    ) -> None:
        super().__init__(parent)
        self._manager = config_manager
        self._available_models = available_models or []
        #: The secure provider settings controller (M3B-S1.2). Optional, so
        #: the dialog still opens where no vault is available.
        self._security = security
        self.setWindowTitle(f"{APP_NAME} \u2014 Settings")
        self.setMinimumWidth(540)
        self.setMaximumHeight(700)
        self.setModal(True)
        self._build()
        self.load_from_config()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)

        provider_page = self._provider_tab()
        provider_scroll = QScrollArea()
        provider_scroll.setWidgetResizable(True)
        provider_scroll.setWidget(provider_page)
        provider_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tabs.addTab(provider_scroll, "Provider")

        agent_page = self._agent_tab()
        agent_scroll = QScrollArea()
        agent_scroll.setWidgetResizable(True)
        agent_scroll.setWidget(agent_page)
        agent_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tabs.addTab(agent_scroll, "Agent")

        if self._security is not None:
            from quantsmind_agent.gui.security.center import SecurityPanel

            security_panel = SecurityPanel(self._security, parent=self)
            security_scroll = QScrollArea()
            security_scroll.setWidgetResizable(True)
            security_scroll.setWidget(security_panel)
            security_scroll.setFrameShape(QFrame.Shape.NoFrame)
            tabs.addTab(security_scroll, "Security")

        about_page = self._about_tab()
        about_scroll = QScrollArea()
        about_scroll.setWidgetResizable(True)
        about_scroll.setWidget(about_page)
        about_scroll.setFrameShape(QFrame.Shape.NoFrame)
        tabs.addTab(about_scroll, "About")
        outer.addWidget(tabs)

        self._hint = QLabel("")
        self._hint.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
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
        self._base_url.textChanged.connect(self._on_provider_edited)
        form.addRow("Base URL", self._base_url)

        self._identity = QLabel("")
        self._identity.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
        self._identity.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Provider identity", self._identity)

        self._api_key = QLineEdit()
        self._api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key.setPlaceholderText("Paste your API key (saved securely)")
        self._api_key.setText("")
        form.addRow("API key", self._api_key)

        key_row = QHBoxLayout()
        self._key_status = QLabel("No key stored")
        self._key_status.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
        clear_button = QPushButton("Clear stored key")
        clear_button.clicked.connect(self._on_clear_key)
        key_row.addWidget(self._key_status)
        key_row.addStretch(1)
        key_row.addWidget(clear_button)
        form.addRow("", key_row)

        self._env_var = QLineEdit()
        self._env_var.setPlaceholderText("QM_API_KEY")
        self._env_var.textChanged.connect(self._on_provider_edited)
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
        self._paths_label.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
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
        self._key_status.setText(self._credential_status_text())
        self._paths_label.setText(
            f"Config file: {self._manager.config_file}<br/>"
            f"Log file: {self._manager.paths.log_file}<br/>"
            "Credentials stay in your OS credential store and are never written to Git."
        )
        self._refresh_identity()
        self._refresh_hint()

    def _on_provider_edited(self) -> None:
        self._refresh_identity()
        self._refresh_hint()

    def _provider_settings_from_widgets(self) -> ProviderSettings | None:
        """The provider settings currently on screen, or ``None`` if invalid.

        The explicit provider binding is re-validated against the *edited*
        endpoint: changing the address must not carry the old provider id, or
        a key for one service would become a key for another.
        """

        provider = self._manager.config.provider
        try:
            endpoint = canonicalize_endpoint(self._base_url.text().strip())
            return ProviderSettings(
                name=provider.name,
                base_url=endpoint.endpoint_id,
                model=self._model.text().strip() or provider.model,
                timeout_seconds=provider.timeout_seconds,
                max_output_tokens=provider.max_output_tokens,
                temperature=provider.temperature,
                api_key_env=self._env_var.text().strip() or "QM_API_KEY",
                organization=provider.organization,
                capability_overrides=provider.capability_overrides,
                provider_id=rebind_explicit_provider_id(endpoint, provider.provider_id),
                trust_custom_endpoint=provider.trust_custom_endpoint,
                allow_remote_local_models=provider.allow_remote_local_models,
            )
        except (EndpointPolicyError, ValueError):
            return None

    def _refresh_identity(self) -> None:
        """Show which provider identity the *edited* configuration resolves to."""

        settings = self._provider_settings_from_widgets()
        if settings is None:
            self._identity.setStyleSheet(f"color:{COLORS['error']};font-size:11px;")
            self._identity.setText("invalid: the model address cannot be used")
            return
        identity = settings.identity()
        if identity.identity_mismatch:
            self._identity.setStyleSheet(f"color:{COLORS['error']};font-size:11px;")
            self._identity.setText(
                f"mismatch: {identity.requested_provider_id!r} does not own "
                f"{identity.endpoint_id}; it will be saved as {identity.provider_id}"
            )
            return
        self._identity.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
        self._identity.setText(
            f"{identity.provider_id} / {identity.profile} ({identity.trust.value})"
        )

    @staticmethod
    def _resolution_text(resolution: CredentialResolution) -> str:
        status = resolution.status
        if status is ResolutionStatus.RESOLVED:
            if resolution.source is CredentialSource.ENVIRONMENT:
                return "Credential: from environment"
            if resolution.storage_status is StorageStatus.PLAINTEXT:
                return "Credential: configured (stored in a plaintext file)"
            return "Credential: configured"
        if status is ResolutionStatus.DEGRADED:
            return "Credential: configured (storage degraded)"
        if status is ResolutionStatus.NOT_REQUIRED:
            return "Credential: not required"
        if status is ResolutionStatus.REJECTED:
            return {
                "provider_endpoint_mismatch": "Credential: provider identity mismatch",
                "credential_disabled": "Credential: disabled",
                "credential_revoked": "Credential: removed",
            }.get(resolution.reason_code, "Credential: unusable")
        if status is ResolutionStatus.UNAVAILABLE:
            return "Credential: store unavailable"
        return "Credential: not configured"

    def _credential_status_text(self) -> str:
        """A status line for the credential the stored configuration uses."""

        if self._security is None:
            return f"Stored key: {self._manager.masked_api_key()}"
        try:
            return self._resolution_text(self._manager.credential_resolution())
        except EndpointPolicyError:
            return "Credential: invalid configuration"

    def to_configuration(self) -> Configuration:
        """Build a validated :class:`Configuration` from the widgets."""

        config = self._manager.config.model_copy(deep=True)
        try:
            endpoint = canonicalize_endpoint(self._base_url.text().strip())
        except EndpointPolicyError as exc:
            raise ConfigurationError(
                f"invalid base URL: {exc}",
                user_message=f"The model address is not valid: {exc}",
            ) from exc
        config.provider.model = self._model.text().strip()
        config.provider.base_url = endpoint.endpoint_id
        config.provider.provider_id = rebind_explicit_provider_id(
            endpoint, config.provider.provider_id
        )
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
        if self._security is not None:
            try:
                identity = self._manager.provider_identity()
            except EndpointPolicyError:
                self._key_status.setText("The saved provider configuration is invalid")
                return
            removed = self._security.remove_credential_for(identity)
            self._key_status.setText(
                self._credential_status_text()
                if removed
                else "No credential was stored for this provider"
            )
            self._api_key.clear()
            return
        removed = self._manager.clear_api_key()
        self._key_status.setText("Stored key removed" if removed else "No key was stored")
        self._api_key.clear()

    def _on_save(self) -> None:
        try:
            config = self.to_configuration()
        except QuantsMindError as exc:
            QMessageBox.warning(self, "Invalid settings", exc.user_message)
            return

        key = self._api_key.text().strip()
        if key:
            try:
                if self._security is not None:
                    # Store under the identity of the configuration *being
                    # saved*, not the one that was loaded, so the key can never
                    # be written under a provider that is no longer selected.
                    self._security.store_credential_for(config.provider.identity(), key)
                else:
                    self._manager.set_api_key_for(config.provider, key)
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

        self._key_status.setText(self._credential_status_text())
        self.accept()

    def _refresh_hint(self) -> None:
        """Explain the *current* credential decision, from the one authority.

        The hint is computed from the values on screen, so it cannot promise a
        key the request path would refuse, or advise an environment variable
        that would not be read for this provider.
        """

        settings = self._provider_settings_from_widgets()
        if settings is None:
            self._hint.setText("The model address is not valid, so no credential would be used.")
            return
        try:
            resolution = self._manager.credential_resolution_for(settings)
        except EndpointPolicyError:
            self._hint.setText("The configured provider identity cannot be used for this address.")
            return

        status = resolution.status
        if status is ResolutionStatus.REJECTED:
            if resolution.reason_code == "provider_endpoint_mismatch":
                self._hint.setText(
                    "The selected provider does not belong to this model address. "
                    "Its API key will not be used; choose the provider again or "
                    "correct the address."
                )
            elif resolution.reason_code in {"credential_disabled", "credential_revoked"}:
                self._hint.setText(
                    "The stored credential is disabled or removed. Enter a new key to "
                    "use this provider."
                )
            else:
                self._hint.setText(
                    "The model endpoint was refused by the security policy, so no key "
                    "would be sent."
                )
            return
        if status is ResolutionStatus.UNAVAILABLE:
            self._hint.setText(
                "The OS credential store could not be read, so no key is available. "
                "Check the credential service and try again."
            )
            return
        if status is ResolutionStatus.DEGRADED:
            self._hint.setText(
                "A credential is configured, but it is not held in the OS credential "
                "store. Move it to secure storage when one is available; only this "
                "provider can use it either way."
            )
            return
        if status is ResolutionStatus.RESOLVED:
            if resolution.source is CredentialSource.ENVIRONMENT:
                self._hint.setText(
                    "A credential is provided by an environment variable for this "
                    "provider only. It is never written to disk."
                )
            elif resolution.storage_status is StorageStatus.PLAINTEXT:
                self._hint.setText(
                    "A credential is configured in a plaintext file. It is never shown "
                    "or logged, but it is not protected by the OS credential store."
                )
            else:
                self._hint.setText("A credential is configured. It is never shown or logged.")
            return
        if status is ResolutionStatus.NOT_REQUIRED:
            self._hint.setText("This endpoint is a local model server, so no API key is required.")
            return

        if self._security is not None:
            candidate = self._security.legacy_candidate()
            if candidate is not None and candidate.provider_id == settings.identity().provider_id:
                self._hint.setText(
                    "A credential was found under the shared legacy identity. Open the "
                    "Security tab to choose the provider it belongs to and confirm the "
                    "migration."
                )
                return
        if resolution.storage_status in (StorageStatus.PLAINTEXT, StorageStatus.DEGRADED):
            self._hint.setText(
                "No secure credential store is available on this system, so API keys "
                "cannot be stored. Existing credentials are left untouched."
            )
            return
        self._hint.setText(
            "No API key yet. Paste one above, or set the environment variable for this "
            "provider before starting the app."
        )
