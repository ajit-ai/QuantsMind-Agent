"""Security panel for the provider settings (M3B-S1.2).

The widgets are a thin view over
:class:`~quantsmind_agent.gui.security.controller.SecurityController`. Every
rule -- which provider owns which credential, when a legacy credential may be
adopted, whether an endpoint is authorised -- lives in the controller, so the
same behaviour applies whether it is driven by a person or by a test.

Deliberate omissions
--------------------
* There is no "reveal secret" control, and no control that copies a
  credential to the clipboard.
* The credential input is never pre-filled with an existing value, and it is
  cleared when the panel closes as well as after a successful save.
* Nothing here reads a credential value into a label, a tooltip or a log.
  The status column shows a masked description at most.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.gui.security.controller import (
    CredentialStatus,
    EndpointState,
    SecurityController,
)
from quantsmind_agent.gui.shell.style import COLORS
from quantsmind_agent.security.provider_identity import EndpointPolicyError

__all__ = ["SecurityDialog", "SecurityPanel"]

logger = get_logger(__name__)

_STATUS_COLUMNS = (
    "Identity",
    "Display name",
    "Endpoint",
    "Authentication",
    "Credential",
    "Endpoint status",
)
_AUDIT_COLUMNS = ("Timestamp", "Category", "Provider", "Event", "Result")


class SecurityPanel(QWidget):
    """Provider identities, credentials, endpoint status and the audit trail."""

    workspace_trust_revoked = Signal()

    def __init__(
        self,
        controller: SecurityController,
        parent: QWidget | None = None,
        *,
        workspace: str = "",
        trusted: bool | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self._workspace = workspace
        self._trusted = trusted
        self._build()
        self.refresh()

    # -- construction ------------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        tabs = QTabWidget()
        tabs.addTab(self._providers_tab(), "Providers & credentials")
        tabs.addTab(self._audit_tab(), "Security audit")
        outer.addWidget(tabs)

    def _providers_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self._workspace_box = QGroupBox("Workspace trust")
        workspace_layout = QVBoxLayout(self._workspace_box)
        self._workspace_label = QLabel("")
        self._workspace_label.setWordWrap(True)
        workspace_layout.addWidget(self._workspace_label)
        workspace_row = QHBoxLayout()
        self._revoke_workspace_button = QPushButton("Restrict and revoke trust")
        self._revoke_workspace_button.setToolTip(
            "Stop the agent reading this folder and remove the stored trust decision"
        )
        self._revoke_workspace_button.clicked.connect(self._on_revoke_workspace_trust)
        workspace_row.addWidget(self._revoke_workspace_button)
        workspace_row.addStretch(1)
        workspace_layout.addLayout(workspace_row)
        layout.addWidget(self._workspace_box)
        self._refresh_workspace_trust()

        self._migration_box = QGroupBox("Legacy credential")
        migration_layout = QVBoxLayout(self._migration_box)
        self._migration_text = QLabel("")
        self._migration_text.setWordWrap(True)
        migration_layout.addWidget(self._migration_text)
        migration_row = QHBoxLayout()
        self._migration_provider = QComboBox()
        self._migration_provider.addItem("Choose the provider it belongs to\u2026", "")
        for option in self._controller.catalog:
            if option.requires_credential:
                self._migration_provider.addItem(option.display_name, option.provider_id)
        migration_row.addWidget(QLabel("Belongs to"))
        migration_row.addWidget(self._migration_provider)
        migration_row.addStretch(1)
        self._migration_button = QPushButton("Confirm migration")
        self._migration_button.clicked.connect(self._on_confirm_migration)
        migration_row.addWidget(self._migration_button)
        migration_layout.addLayout(migration_row)
        layout.addWidget(self._migration_box)

        self._provider_table = QTableWidget(0, len(_STATUS_COLUMNS))
        self._provider_table.setHorizontalHeaderLabels(list(_STATUS_COLUMNS))
        self._provider_table.verticalHeader().setVisible(False)
        self._provider_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._provider_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        header = self._provider_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self._provider_table, stretch=1)

        provider_row = QHBoxLayout()
        self._provider_choice = QComboBox()
        self._provider_choice.addItem("Choose a provider\u2026", "")
        for option in self._controller.catalog:
            self._provider_choice.addItem(option.display_name, option.provider_id)
        provider_row.addWidget(QLabel("Provider identity"))
        provider_row.addWidget(self._provider_choice)
        self._use_button = QPushButton("Use this provider")
        self._use_button.clicked.connect(self._on_use_provider)
        provider_row.addWidget(self._use_button)
        provider_row.addStretch(1)
        layout.addLayout(provider_row)

        self._credential_box = QGroupBox("Credential")
        credential_layout = QFormLayout(self._credential_box)
        self._credential_status = QLabel("Not configured")
        credential_layout.addRow("Status", self._credential_status)
        self._credential_masked = QLabel("")
        self._credential_masked.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
        credential_layout.addRow("Stored value", self._credential_masked)

        self._credential_input = QLineEdit()
        self._credential_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._credential_input.setPlaceholderText("Enter the API key for this provider")
        credential_layout.addRow("New API key", self._credential_input)

        buttons = QHBoxLayout()
        self._save_button = QPushButton("Save key")
        self._save_button.clicked.connect(self._on_save_credential)
        self._disable_button = QPushButton("Disable")
        self._disable_button.clicked.connect(self._on_disable_credential)
        self._enable_button = QPushButton("Re-enable")
        self._enable_button.clicked.connect(self._on_enable_credential)
        self._remove_button = QPushButton("Remove")
        self._remove_button.clicked.connect(self._on_remove_credential)
        for button in (
            self._save_button,
            self._disable_button,
            self._enable_button,
            self._remove_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        credential_layout.addRow("", buttons)
        layout.addWidget(self._credential_box)

        self._endpoint_label = QLabel("")
        self._endpoint_label.setWordWrap(True)
        layout.addWidget(self._endpoint_label)
        self._trust_row = QHBoxLayout()
        self._trust_button = QPushButton("Trust this custom endpoint")
        self._trust_button.clicked.connect(self._on_trust_endpoint)
        self._trust_row.addWidget(self._trust_button)
        self._trust_row.addStretch(1)
        layout.addLayout(self._trust_row)
        return page

    def _refresh_workspace_trust(self) -> None:
        if not self._workspace:
            self._workspace_label.setText("No folder is open.")
            self._revoke_workspace_button.setEnabled(False)
            return
        if self._trusted is None:
            state = "unknown"
        else:
            state = "trusted" if self._trusted else "restricted"
        self._workspace_label.setText(
            f"{self._workspace}\nCurrent state: {state}. "
            + (
                "The agent may read this folder and project indexing is enabled."
                if self._trusted
                else "Browsing and editing stay available; the agent and indexing are off."
            )
        )
        self._revoke_workspace_button.setEnabled(bool(self._trusted))

    def set_workspace_trust(self, workspace: str, trusted: bool | None) -> None:
        self._workspace = workspace
        self._trusted = trusted
        self._refresh_workspace_trust()

    def _on_revoke_workspace_trust(self) -> None:
        if not self._workspace:
            return
        answer = QMessageBox.question(
            self,
            "Revoke workspace trust?",
            "The agent will stop reading this folder, project indexing is removed, "
            "and the stored trust decision is deleted. Browsing and editing stay "
            "available. You can trust the folder again later.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self.workspace_trust_revoked.emit()

    def _audit_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        filters = QHBoxLayout()
        self._filter_provider = QComboBox()
        self._filter_provider.addItem("All providers", "")
        self._filter_provider.currentIndexChanged.connect(self._refresh_audit)
        self._filter_event = QComboBox()
        self._filter_event.addItem("All events", "")
        self._filter_event.currentIndexChanged.connect(self._refresh_audit)
        self._filter_result = QComboBox()
        self._filter_result.addItem("All results", "")
        self._filter_result.currentIndexChanged.connect(self._refresh_audit)
        filters.addWidget(QLabel("Provider"))
        filters.addWidget(self._filter_provider)
        filters.addWidget(QLabel("Event"))
        filters.addWidget(self._filter_event)
        filters.addWidget(QLabel("Result"))
        filters.addWidget(self._filter_result)
        filters.addStretch(1)
        layout.addLayout(filters)

        self._audit_table = QTableWidget(0, len(_AUDIT_COLUMNS))
        self._audit_table.setHorizontalHeaderLabels(list(_AUDIT_COLUMNS))
        self._audit_table.verticalHeader().setVisible(False)
        self._audit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._audit_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        layout.addWidget(self._audit_table, stretch=1)

        archive_row = QHBoxLayout()
        self._include_archive = QCheckBox("Include records from earlier sessions")
        self._include_archive.setToolTip(
            "Read the durable audit store and show the newest 500 records it holds, "
            "including those written before this run. Reading never modifies the store."
        )
        self._include_archive.toggled.connect(self._refresh_audit)
        archive_row.addWidget(self._include_archive)
        archive_row.addStretch(1)
        layout.addLayout(archive_row)

        self._audit_note = QLabel()
        self._audit_note.setStyleSheet(f"color:{COLORS['muted']};font-size:11px;")
        self._audit_note.setWordWrap(True)
        layout.addWidget(self._audit_note)
        return page

    # -- data -------------------------------------------------------------
    def refresh(self) -> None:
        """Re-read every value from the controller and redraw."""

        self._refresh_providers()
        self._refresh_credential()
        self._refresh_endpoint()
        self._refresh_migration()
        self._refresh_audit()

    def _selected_provider_id(self) -> str:
        return str(self._provider_choice.currentData() or "")

    def _refresh_providers(self) -> None:
        try:
            current_id = self._controller.current_identity().provider_id
        except EndpointPolicyError:
            current_id = ""
        self._provider_table.setRowCount(0)
        for option in self._controller.catalog:
            row = self._provider_table.rowCount()
            self._provider_table.insertRow(row)
            status = self._controller.credential_status(option.provider_id)
            endpoint = (
                self._controller.endpoint_status() if option.provider_id == current_id else None
            )
            values = (
                option.provider_id,
                option.display_name,
                option.base_url,
                option.authentication,
                status.label,
                endpoint.label if endpoint is not None else "\u2014",
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._provider_table.setItem(row, column, item)

    def _refresh_credential(self) -> None:
        provider_id = self._selected_provider_id()
        if not provider_id:
            self._credential_status.setText("Choose a provider identity")
            self._credential_masked.setText("")
            return
        status = self._controller.credential_status(provider_id)
        self._credential_status.setText(status.label)
        self._credential_masked.setText(self._controller.masked_credential(provider_id))
        # An environment credential is not stored here, so it cannot be
        # disabled, re-enabled or removed from this panel.
        self._disable_button.setEnabled(
            status
            in (
                CredentialStatus.CONFIGURED,
                CredentialStatus.ACTIVE,
                CredentialStatus.DEGRADED,
            )
        )
        self._enable_button.setEnabled(status is CredentialStatus.DISABLED)
        self._remove_button.setEnabled(
            status
            in (
                CredentialStatus.CONFIGURED,
                CredentialStatus.ACTIVE,
                CredentialStatus.DEGRADED,
                CredentialStatus.DISABLED,
            )
        )

    def _refresh_endpoint(self) -> None:
        status = self._controller.endpoint_status()
        self._endpoint_label.setText(
            f"Endpoint: {status.endpoint_id or '(not set)'} \u2014 {status.label}"
            + (f" ({status.reason_code})" if status.reason_code else "")
        )
        self._trust_button.setVisible(status.state is EndpointState.CUSTOM_REQUIRES_AUTHORIZATION)

    def _refresh_migration(self) -> None:
        preview = self._controller.migration_preview()
        if not preview:
            self._migration_box.setVisible(False)
            return
        self._migration_box.setVisible(True)
        self._migration_text.setText(
            "A credential was found under the shared legacy identity "
            f"<b>{preview['legacy_slot']}</b>, which every provider used to share. "
            "It has not been used, moved or deleted. Choose the provider it belongs "
            "to and confirm; the original is kept until the new location has been "
            "verified. Nothing is sent to the provider during this operation."
        )
        self._migration_button.setEnabled(bool(self._migration_provider.currentData()))

    def _refresh_audit(self) -> None:
        include_archive = self._include_archive.isChecked()
        rows_all = self._controller.audit_rows(include_persisted=include_archive)
        events = sorted({str(record.event) for record in rows_all})
        for name, combo in (("event", self._filter_event), ("result", self._filter_result)):
            current = str(combo.currentData() or "")
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("All" if name == "event" else "All results", "")
            for value in (
                events
                if name == "event"
                else sorted({record.decision for record in rows_all if record.decision})
            ):
                combo.addItem(value, value)
            index = combo.findData(current)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)
        providers = sorted({record.provider_id for record in rows_all if record.provider_id})
        current_provider = str(self._filter_provider.currentData() or "")
        self._filter_provider.blockSignals(True)
        self._filter_provider.clear()
        self._filter_provider.addItem("All providers", "")
        for provider_id in providers:
            self._filter_provider.addItem(provider_id, provider_id)
        index = self._filter_provider.findData(current_provider)
        self._filter_provider.setCurrentIndex(index if index >= 0 else 0)
        self._filter_provider.blockSignals(False)

        rows = self._controller.audit_rows(
            provider_id=str(self._filter_provider.currentData() or ""),
            event=str(self._filter_event.currentData() or ""),
            result=str(self._filter_result.currentData() or ""),
            include_persisted=include_archive,
        )
        self._audit_table.setRowCount(0)
        for record in rows:
            row = self._audit_table.rowCount()
            self._audit_table.insertRow(row)
            values = (
                record.timestamp,
                str(record.event).split(".")[0],
                record.provider_id,
                str(record.event),
                record.decision or record.reason_code,
            )
            for column, value in enumerate(values):
                self._audit_table.setItem(row, column, QTableWidgetItem(str(value)))

        self._include_archive.setEnabled(not self._controller.audit_is_session_only())
        self._audit_note.setText(self._audit_note_text())

    def _audit_note_text(self) -> str:
        """Where these records live, and what the chain does not claim."""

        if self._controller.audit_is_session_only():
            return (
                "No durable store is attached in this run: these records live in "
                "memory and are gone when the application closes."
            )
        return (
            f"{self._controller.audit_store_note()}\n"
            "Chaining makes a changed or removed record detectable. It is not proof "
            "against tampering: anyone who can rewrite the store can rewrite its "
            "anchor as well, and records older than the retained window are gone."
        )

    # -- handlers ---------------------------------------------------------
    def _on_use_provider(self) -> None:
        provider_id = self._selected_provider_id()
        option = self._controller.option_for(provider_id) if provider_id else None
        if option is None:
            return
        self._controller.apply_provider(option)
        self._controller.record_provider_selected(option)
        self.refresh()

    def _on_save_credential(self) -> None:
        provider_id = self._selected_provider_id()
        if not provider_id:
            QMessageBox.warning(self, "No provider", "Choose a provider identity first.")
            return
        value = self._credential_input.text()
        try:
            self._controller.store_credential(provider_id, value)
        except Exception as exc:  # the controller raises domain errors
            message = getattr(exc, "user_message", None) or str(exc)
            QMessageBox.warning(self, "Could not store the key", message)
            return
        finally:
            # The transient input is cleared whether the write succeeded or not.
            self._credential_input.clear()
        self.refresh()

    def _on_disable_credential(self) -> None:
        provider_id = self._selected_provider_id()
        if provider_id:
            self._controller.disable_credential(provider_id)
            self.refresh()

    def _on_enable_credential(self) -> None:
        provider_id = self._selected_provider_id()
        if provider_id:
            self._controller.enable_credential(provider_id)
            self.refresh()

    def _on_remove_credential(self) -> None:
        provider_id = self._selected_provider_id()
        if not provider_id:
            return
        answer = QMessageBox.question(
            self,
            "Remove the credential?",
            "The stored key for this provider will be deleted. The key itself is "
            "never shown. This does not revoke the key at the provider.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._controller.remove_credential(provider_id)
        self.refresh()

    def _on_confirm_migration(self) -> None:
        provider_id = str(self._migration_provider.currentData() or "")
        if not provider_id:
            QMessageBox.warning(
                self,
                "Choose a provider",
                "Choose which provider the legacy credential belongs to.",
            )
            return
        answer = QMessageBox.question(
            self,
            "Adopt the legacy credential?",
            f"The credential stored under the shared legacy identity will be moved to "
            f"the {provider_id} identity and verified there. The original is removed "
            "only after the new copy has been read back. No request is sent to the "
            "provider.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        result = self._controller.confirm_migration(provider_id)
        if result.migrated:
            QMessageBox.information(
                self,
                "Credential adopted",
                "The credential is now stored under the provider identity and has been "
                "read back successfully.",
            )
        else:
            QMessageBox.warning(
                self,
                "The credential was not adopted",
                f"The migration did not complete ({result.status}). The original "
                "credential has not been changed.",
            )
        self.refresh()

    def _on_trust_endpoint(self) -> None:
        status = self._controller.endpoint_status()
        answer = QMessageBox.question(
            self,
            "Trust this custom endpoint?",
            f"The endpoint {status.endpoint_id or '(not set)'} is not a known service. "
            "The API key configured for this provider identity will be sent to it. "
            "Trust it only if you trust the operator of that address.",
        )
        if answer is not QMessageBox.StandardButton.Yes:
            return
        self._controller.trust_custom_endpoint(True)
        self.refresh()

    # -- lifecycle --------------------------------------------------------
    def closeEvent(self, event: Any) -> None:
        # A key typed but not saved must not survive the panel.
        self._credential_input.clear()
        super().closeEvent(event)


class SecurityDialog(QDialog):
    """A modal wrapper around :class:`SecurityPanel`."""

    workspace_trust_revoked = Signal()

    def __init__(
        self,
        controller: SecurityController,
        parent: QWidget | None = None,
        *,
        workspace: str = "",
        trusted: bool | None = None,
    ) -> None:
        super().__init__(parent)
        from quantsmind_agent.version import APP_NAME

        self.setWindowTitle(f"{APP_NAME} \u2014 Security")
        self.setMinimumWidth(760)
        self.setModal(True)
        layout = QVBoxLayout(self)
        self.panel = SecurityPanel(controller, parent=self, workspace=workspace, trusted=trusted)
        self.panel.workspace_trust_revoked.connect(self.workspace_trust_revoked.emit)
        layout.addWidget(self.panel)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def reject(self) -> None:
        self.panel._credential_input.clear()
        super().reject()
