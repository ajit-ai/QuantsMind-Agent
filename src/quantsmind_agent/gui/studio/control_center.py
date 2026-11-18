"""Control Center: providers, trust, execution policy and security status.

Every fact shown here is read from the configuration, the credential service and
the security audit that the rest of the application already uses. The page owns
no policy: its buttons open the existing settings and security dialogs, and its
workspace actions route to the window's trust path. No credential value is ever
placed in a label; the only credential text is the masked description the
credential service returns.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from quantsmind_agent.gui.studio.cards import Badge, Card, EmptyState, clear_layout, section_label

__all__ = ["ControlCenter", "ControlSnapshot"]


@dataclass(frozen=True, slots=True)
class ControlSnapshot:
    """Everything the Control Center paints, prepared by the window."""

    provider_name: str = ""
    provider_id: str = ""
    model: str = ""
    base_url: str = ""
    credential: str = "No credential resolved"
    readiness: str = ""
    secure_storage: bool = False
    workspace: str = ""
    trust: str = "none"
    max_iterations: int = 0
    max_context_files: int = 0
    max_file_bytes: int = 0
    command_timeout: float = 0.0
    auto_approve_reads: bool = False
    auto_approve_writes: bool = False
    audit_events: int = 0
    audit_denials: int = 0
    config_dir: str = ""
    data_dir: str = ""
    log_dir: str = ""
    memory: str = ""
    #: Resource governance (WP0). The governor's own numbers, not a restatement
    #: of the memory figure: what it decided, what it degraded and what it refused.
    resource_pressure: str = "normal"
    resource_decisions: int = 0
    resource_refusals: int = 0
    resource_degraded: str = ""
    resource_last_refusal: str = ""
    #: The persistent audit store (WP3), described by the sink itself.
    audit_path: str = ""
    audit_records: int = 0
    audit_bytes_used: int = 0
    audit_ceiling_bytes: int = 0
    audit_chain_ok: bool = True
    audit_chain_note: str = ""
    audit_degraded: bool = False
    audit_last_error: str = ""
    #: The store did not match its witness when this session opened it.
    audit_recovery_note: str = ""

    @property
    def has_workspace(self) -> bool:
        return bool(self.workspace)

    @property
    def ready(self) -> bool:
        return not self.readiness


class ControlCenter(QWidget):
    """The operations console for providers, trust, policy and audit."""

    settings_requested = Signal()
    security_requested = Signal()
    trust_requested = Signal()
    restrict_requested = Signal()
    revoke_requested = Signal()
    open_workspace_requested = Signal()
    refresh_requested = Signal()
    #: An operator asked for a copy of the audit records. Export is never automatic.
    audit_export_requested = Signal()
    #: v1.4 production-readiness actions.
    support_pack_requested = Signal()
    backup_requested = Signal()
    restore_requested = Signal()
    performance_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("StudioPage")
        self._snapshot = ControlSnapshot()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        page.setObjectName("StudioPage")
        self._layout = QVBoxLayout(page)
        self._layout.setContentsMargins(30, 24, 30, 30)
        self._layout.setSpacing(14)
        scroll.setWidget(page)
        outer.addWidget(scroll)

        heading = QHBoxLayout()
        column = QVBoxLayout()
        column.setSpacing(3)
        title = QLabel("Control Center")
        title.setObjectName("PageTitle")
        column.addWidget(title)
        subtitle = QLabel(
            "Provider identity, credentials, workspace trust, execution limits and "
            "the security audit trail."
        )
        subtitle.setObjectName("PageSubtitle")
        subtitle.setWordWrap(True)
        column.addWidget(subtitle)
        heading.addLayout(column, 1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_requested.emit)
        heading.addWidget(refresh)
        self._layout.addLayout(heading)

        top = QHBoxLayout()
        top.setSpacing(14)
        self.provider_card = Card("Provider and model")
        self.provider_host = QWidget()
        self.provider_layout = QVBoxLayout(self.provider_host)
        self.provider_layout.setContentsMargins(0, 0, 0, 0)
        self.provider_layout.setSpacing(6)
        self.provider_card.add(self.provider_host)
        top.addWidget(self.provider_card, 3)

        self.workspace_card = Card("Workspace access")
        self.workspace_host = QWidget()
        self.workspace_layout = QVBoxLayout(self.workspace_host)
        self.workspace_layout.setContentsMargins(0, 0, 0, 0)
        self.workspace_layout.setSpacing(6)
        self.workspace_card.add(self.workspace_host)
        top.addWidget(self.workspace_card, 2)
        self._layout.addLayout(top)

        middle = QHBoxLayout()
        middle.setSpacing(14)
        self.policy_card = Card("Execution policy")
        self.policy_host = QWidget()
        self.policy_layout = QVBoxLayout(self.policy_host)
        self.policy_layout.setContentsMargins(0, 0, 0, 0)
        self.policy_layout.setSpacing(5)
        self.policy_card.add(self.policy_host)
        middle.addWidget(self.policy_card, 1)

        self.security_card = Card("Security and audit")
        self.security_host = QWidget()
        self.security_layout = QVBoxLayout(self.security_host)
        self.security_layout.setContentsMargins(0, 0, 0, 0)
        self.security_layout.setSpacing(5)
        self.security_card.add(self.security_host)
        middle.addWidget(self.security_card, 1)
        self._layout.addLayout(middle)

        self.storage_card = Card("Storage and resources")
        self.storage_host = QWidget()
        self.storage_layout = QVBoxLayout(self.storage_host)
        self.storage_layout.setContentsMargins(0, 0, 0, 0)
        self.storage_layout.setSpacing(5)
        self.storage_card.add(self.storage_host)
        self._layout.addWidget(self.storage_card)

        boot = Card("Production readiness (v1.4)")
        boot_host = QWidget()
        boot_layout = QVBoxLayout(boot_host)
        boot_layout.setContentsMargins(0, 0, 0, 0)
        boot_layout.setSpacing(5)
        for label, signal in (
            ("Download support pack…", self.support_pack_requested),
            ("Backup workspace state…", self.backup_requested),
            ("Restore workspace state…", self.restore_requested),
            ("Performance governor…", self.performance_requested),
        ):
            button = QPushButton(label)
            button.clicked.connect(signal.emit)
            boot_layout.addWidget(button)
        self.ops_status = QLabel("Support pack, backup and performance are explicit actions.")
        self.ops_status.setObjectName("StatusText")
        boot_layout.addWidget(self.ops_status)
        boot.add(boot_host)
        self._layout.addWidget(boot)

        self._layout.addStretch(1)
        self._paint(ControlSnapshot())

    # -- data --------------------------------------------------------------
    @property
    def snapshot(self) -> ControlSnapshot:
        return self._snapshot

    def set_snapshot(self, snapshot: ControlSnapshot) -> None:
        self._snapshot = snapshot
        self._paint(snapshot)

    def _paint(self, snapshot: ControlSnapshot) -> None:
        self._paint_provider(snapshot)
        self._paint_workspace(snapshot)
        self._paint_policy(snapshot)
        self._paint_security(snapshot)
        self._paint_storage(snapshot)

    # -- cards -------------------------------------------------------------
    def _paint_provider(self, snapshot: ControlSnapshot) -> None:
        clear_layout(self.provider_layout)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(
            Badge(
                "ready" if snapshot.ready else "not ready",
                tone="success" if snapshot.ready else "warning",
            )
        )
        if snapshot.provider_id:
            row.addWidget(Badge(snapshot.provider_id, tone="accent"))
        row.addStretch(1)
        self.provider_layout.addLayout(row)

        self.provider_layout.addWidget(self._pair("Provider", snapshot.provider_name or "not set"))
        self.provider_layout.addWidget(self._pair("Model", snapshot.model or "not set"))
        self.provider_layout.addWidget(
            self._pair("Endpoint", snapshot.base_url or "not set", mono=True)
        )
        self.provider_layout.addWidget(self._pair("Credential", snapshot.credential))
        if snapshot.readiness:
            reason = QLabel(f"Not ready: {snapshot.readiness}")
            reason.setObjectName("ErrorText")
            reason.setWordWrap(True)
            self.provider_layout.addWidget(reason)
        else:
            note = QLabel("The agent is ready to send requests with this configuration.")
            note.setObjectName("StatusText")
            note.setWordWrap(True)
            self.provider_layout.addWidget(note)
        self.provider_layout.addWidget(section_label("Secrets"))
        note = QLabel(
            "API keys are stored by the operating system credential store and are "
            "never shown here, written to the log or written to project files."
        )
        note.setObjectName("StatusText")
        note.setWordWrap(True)
        self.provider_layout.addWidget(note)

        buttons = QHBoxLayout()
        settings = QPushButton("Provider settings")
        settings.clicked.connect(self.settings_requested.emit)
        buttons.addWidget(settings)
        security = QPushButton("Security and credentials")
        security.clicked.connect(self.security_requested.emit)
        buttons.addWidget(security)
        buttons.addStretch(1)
        self.provider_layout.addLayout(buttons)

    def _paint_workspace(self, snapshot: ControlSnapshot) -> None:
        clear_layout(self.workspace_layout)
        if not snapshot.has_workspace:
            self.workspace_layout.addWidget(
                EmptyState(
                    "No project is open.",
                    "Open a folder to decide whether the agent may read and index it.",
                )
            )
            button = QPushButton("Open project")
            button.setObjectName("PrimaryButton")
            button.clicked.connect(self.open_workspace_requested.emit)
            self.workspace_layout.addWidget(button)
            return

        row = QHBoxLayout()
        row.setSpacing(6)
        tone = {"trusted": "success", "restricted": "warning"}.get(snapshot.trust, "")
        row.addWidget(Badge(snapshot.trust or "undecided", tone=tone))
        row.addStretch(1)
        self.workspace_layout.addLayout(row)
        self.workspace_layout.addWidget(self._pair("Root", snapshot.workspace, mono=True))
        explanation = {
            "trusted": "The agent may read this project's files and index it. Writes, "
            "deletions and commands still ask for approval.",
            "restricted": "Browsing, editing and saving stay available. The agent, its "
            "commands and project indexing are disabled.",
        }.get(
            snapshot.trust,
            "No trust decision exists for this root. The safe default is restricted.",
        )
        note = QLabel(explanation)
        note.setObjectName("CardBody")
        note.setWordWrap(True)
        self.workspace_layout.addWidget(note)

        buttons = QHBoxLayout()
        if snapshot.trust == "trusted":
            revoke = QPushButton("Revoke trust")
            revoke.setObjectName("DangerButton")
            revoke.clicked.connect(self.revoke_requested.emit)
            buttons.addWidget(revoke)
            restrict = QPushButton("Restrict workspace")
            restrict.clicked.connect(self.restrict_requested.emit)
            buttons.addWidget(restrict)
        else:
            trust = QPushButton("Trust workspace")
            trust.setObjectName("PrimaryButton")
            trust.clicked.connect(self.trust_requested.emit)
            buttons.addWidget(trust)
        buttons.addStretch(1)
        self.workspace_layout.addLayout(buttons)

    def _paint_policy(self, snapshot: ControlSnapshot) -> None:
        clear_layout(self.policy_layout)
        approvals = (
            "Writes are auto-approved; deletions and commands always ask."
            if snapshot.auto_approve_writes
            else "Writes, deletions and commands ask before they run."
        )
        self.policy_layout.addWidget(self._pair("Iteration limit", str(snapshot.max_iterations)))
        self.policy_layout.addWidget(self._pair("Context files", str(snapshot.max_context_files)))
        self.policy_layout.addWidget(
            self._pair("Max file size", f"{snapshot.max_file_bytes // 1024} KB")
        )
        self.policy_layout.addWidget(
            self._pair("Command timeout", f"{snapshot.command_timeout:.0f} s")
        )
        self.policy_layout.addWidget(self._pair("Approvals", approvals))
        reads = "allowed automatically" if snapshot.auto_approve_reads else "ask before reading"
        self.policy_layout.addWidget(self._pair("Reads", reads))
        note = QLabel(
            "These values are enforced by the agent core and the permission broker. "
            "This page displays them; it cannot widen them."
        )
        note.setObjectName("StatusText")
        note.setWordWrap(True)
        self.policy_layout.addWidget(note)

    def _paint_security(self, snapshot: ControlSnapshot) -> None:
        clear_layout(self.security_layout)
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(
            Badge(
                f"{snapshot.audit_events} event(s)",
                tone="accent" if snapshot.audit_events else "",
            )
        )
        row.addWidget(
            Badge(
                f"{snapshot.audit_denials} denial(s)",
                tone="error" if snapshot.audit_denials else "",
            )
        )
        row.addStretch(1)
        self.security_layout.addLayout(row)
        note = QLabel(
            "The audit records identifiers, decisions and reasons \u2014 never credential "
            "values. Records are held for this session and written to a bounded, chained "
            "store on disk."
        )
        note.setObjectName("CardBody")
        note.setWordWrap(True)
        self.security_layout.addWidget(note)
        store = (
            f"{snapshot.audit_records} record(s), "
            f"{snapshot.audit_bytes_used // 1024} KiB of "
            f"{max(1, snapshot.audit_ceiling_bytes // 1024)} KiB \u2014 {snapshot.audit_chain_note}"
            if snapshot.audit_path
            else "The audit store is not configured."
        )
        self.security_layout.addWidget(
            self._pair(
                "Audit store",
                store,
                tone="CardBody"
                if snapshot.audit_chain_ok and not snapshot.audit_recovery_note
                else "ErrorText",
            )
        )
        if snapshot.audit_path:
            self.security_layout.addWidget(self._pair("Location", snapshot.audit_path, mono=True))
        incomplete = snapshot.audit_degraded or not snapshot.audit_chain_ok
        if incomplete:
            # Saying what the chain does and does not prove, next to the number
            # that would otherwise imply more than it means.
            banner = QLabel(
                "The audit trail is incomplete. "
                + (snapshot.audit_last_error or snapshot.audit_chain_note)
            )
            banner.setObjectName("WarningBanner")
            banner.setWordWrap(True)
            self.security_layout.addWidget(banner)
        if snapshot.audit_recovery_note:
            witness = QLabel(
                f"The store did not match its own witness when this session opened: "
                f"{snapshot.audit_recovery_note}. The records were changed while the "
                "application was closed."
            )
            witness.setObjectName("WarningBanner")
            witness.setWordWrap(True)
            self.security_layout.addWidget(witness)
        if not incomplete and not snapshot.audit_recovery_note:
            honesty = QLabel(
                "Chaining makes a changed or removed record detectable. It is not proof "
                "against tampering: anyone who can rewrite the store can rewrite its anchor too."
            )
            honesty.setObjectName("CardBody")
            honesty.setWordWrap(True)
            self.security_layout.addWidget(honesty)
        secure = QLabel(
            "Operating-system credential storage is available."
            if snapshot.secure_storage
            else "The operating-system credential store is unavailable. Credentials "
            "cannot be saved securely in this session."
        )
        secure.setObjectName("StatusText" if snapshot.secure_storage else "ErrorText")
        secure.setWordWrap(True)
        self.security_layout.addWidget(secure)
        open_security = QPushButton("Open security and credentials")
        open_security.clicked.connect(self.security_requested.emit)
        self.security_layout.addWidget(open_security)
        export = QPushButton("Export audit evidence")
        export.setToolTip(
            "Write a readable copy of the records still on disk, with the chain report "
            "attached. You choose where it goes; nothing is exported automatically."
        )
        export.clicked.connect(self.audit_export_requested.emit)
        self.security_layout.addWidget(export)

    def _paint_storage(self, snapshot: ControlSnapshot) -> None:
        clear_layout(self.storage_layout)
        self.storage_layout.addWidget(self._pair("Configuration", snapshot.config_dir, mono=True))
        self.storage_layout.addWidget(self._pair("Session data", snapshot.data_dir, mono=True))
        self.storage_layout.addWidget(self._pair("Logs", snapshot.log_dir, mono=True))
        self.storage_layout.addWidget(self._pair("Memory", snapshot.memory))
        tone = "CardBody" if snapshot.resource_pressure in ("normal", "elevated") else "ErrorText"
        pressure = self._pair("Resource pressure", snapshot.resource_pressure)
        self.storage_layout.addWidget(pressure)
        self.storage_layout.addWidget(
            self._pair(
                "Governance decisions",
                f"{snapshot.resource_decisions} made, {snapshot.resource_refusals} refused",
            )
        )
        if snapshot.resource_degraded:
            self.storage_layout.addWidget(
                self._pair("Running degraded", snapshot.resource_degraded)
            )
        if snapshot.resource_last_refusal:
            refusal = QLabel(snapshot.resource_last_refusal)
            refusal.setObjectName("WarningBanner")
            refusal.setWordWrap(True)
            self.storage_layout.addWidget(refusal)
        note = QLabel(
            "Tasks, conversations, open tabs and workspace trust are stored under these "
            "directories. Credential values never are.\n"
            "Memory pressure degrades optional indexing and background analysis only. "
            "Permissions, approvals, trust, audit integrity and transaction recovery "
            "are never degraded, and heavy work is refused rather than run unsafely."
        )
        note.setObjectName(tone)
        note.setWordWrap(True)
        self.storage_layout.addWidget(note)

    @staticmethod
    def _pair(label: str, value: str, *, mono: bool = False, tone: str = "") -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        key = QLabel(label)
        key.setObjectName("StatusText")
        key.setMinimumWidth(104)
        layout.addWidget(key)
        val = QLabel(value)
        val.setObjectName(tone or ("MonoText" if mono else "CardBody"))
        val.setWordWrap(True)
        val.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(val, 1)
        return row

    def describe(self) -> dict[str, object]:
        return {
            "provider_id": self._snapshot.provider_id,
            "model": self._snapshot.model,
            "credential": self._snapshot.credential,
            "ready": self._snapshot.ready,
            "trust": self._snapshot.trust,
            "audit_events": self._snapshot.audit_events,
            "audit_denials": self._snapshot.audit_denials,
            "audit_records": self._snapshot.audit_records,
            "audit_chain_ok": self._snapshot.audit_chain_ok,
            "audit_degraded": self._snapshot.audit_degraded,
            "secure_storage": self._snapshot.secure_storage,
        }
