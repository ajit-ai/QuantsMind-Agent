"""Headless GUI tests for the secure provider settings (M3B-S1.2).

These instantiate the real widgets on Qt's ``offscreen`` platform, so they
run without a visible desktop. They cover the parts of the behaviour that
only exist in the interface: what is displayed, what is cleared, and what the
user can and cannot do.

The rules themselves are qualified in
``tests/security/test_m3b_s12_credential_migration.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is required for GUI tests")

from PySide6.QtWidgets import QLineEdit

from quantsmind_agent.auth_gateway import AuthGateway
from quantsmind_agent.config import ConfigManager
from quantsmind_agent.credential_vault import CredentialVault
from quantsmind_agent.gui.security_controller import SecurityController
from quantsmind_agent.gui.security_panel import SecurityDialog, SecurityPanel
from quantsmind_agent.paths import AppPaths
from quantsmind_agent.security_audit import SecurityAuditLog
from tests.security.test_m3b_s11_credential_security import FakeSecretStore

pytestmark = pytest.mark.gui

OPENROUTER_KEY = "sk-canary-openrouter-0000000000"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def _controller(tmp_path: Path, store: FakeSecretStore | None = None) -> SecurityController:
    paths = AppPaths(
        config_dir=tmp_path / "config",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
        data_dir=tmp_path / "data",
    ).ensure()
    backend = store or FakeSecretStore()
    manager = ConfigManager(paths, secret_store=backend)
    (paths.config_dir / "config.toml").write_text(
        f'[provider]\nbase_url = "{OPENROUTER_BASE}"\n', encoding="utf-8"
    )
    manager.load()
    audit = SecurityAuditLog()
    vault = CredentialVault(backend, metadata_path=None, audit=audit, require_secure=True)
    gateway = AuthGateway(
        vault=vault,
        audit=audit,
        check_dns=False,
        trust_custom_endpoints=lambda: manager.config.provider.trust_custom_endpoint,
    )
    return SecurityController(
        manager,
        vault=vault,
        gateway=gateway,
        audit=audit,
        secure_storage=True,
    )


def _select(panel: SecurityPanel, provider_id: str) -> None:
    index = panel._provider_choice.findData(provider_id)
    panel._provider_choice.setCurrentIndex(index)


def _cell(panel: SecurityPanel, row: int, column: int) -> str:
    item = panel._provider_table.item(row, column)
    assert item is not None
    return item.text()


def _row_of(panel: SecurityPanel, provider_id: str) -> int:
    for row in range(panel._provider_table.rowCount()):
        if _cell(panel, row, 0) == provider_id:
            return row
    raise AssertionError(f"no row for {provider_id}")


def _audit_cell(panel: SecurityPanel, row: int, column: int) -> str:
    item = panel._audit_table.item(row, column)
    assert item is not None
    return item.text()


class TestProviderTable:
    def test_the_table_lists_every_provider_identity(self, qapp: object, tmp_path: Path) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        assert panel._provider_table.rowCount() == len(panel._controller.catalog)
        identities = [_cell(panel, row, 0) for row in range(panel._provider_table.rowCount())]
        assert "openrouter" in identities
        assert "ollama" in identities

    def test_the_table_distinguishes_identity_from_display_name(
        self, qapp: object, tmp_path: Path
    ) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        row = _row_of(panel, "openrouter")
        assert _cell(panel, row, 0) == "openrouter"
        assert _cell(panel, row, 1) == "OpenRouter"
        assert _cell(panel, row, 3) != ""

    def test_a_local_provider_is_shown_as_needing_no_credential(
        self, qapp: object, tmp_path: Path
    ) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        row = _row_of(panel, "ollama")
        assert "None" in _cell(panel, row, 3)

    def test_the_credential_status_column_reflects_the_vault(
        self, qapp: object, tmp_path: Path
    ) -> None:
        controller = _controller(tmp_path)
        panel = SecurityPanel(controller)
        row = _row_of(panel, "openrouter")
        assert _cell(panel, row, 4) == "Not configured"
        controller.store_credential("openrouter", OPENROUTER_KEY)
        panel.refresh()
        row = _row_of(panel, "openrouter")
        assert _cell(panel, row, 4) == "Active"

    def test_no_widget_ever_shows_a_credential_value(self, qapp: object, tmp_path: Path) -> None:
        controller = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        panel = SecurityPanel(controller)
        rendered = " ".join(
            [
                panel._credential_status.text(),
                panel._credential_masked.text(),
                panel._migration_text.text(),
                panel._endpoint_label.text(),
            ]
            + [
                _cell(panel, row, column)
                for row in range(panel._provider_table.rowCount())
                for column in range(panel._provider_table.columnCount())
            ]
        )
        assert OPENROUTER_KEY not in rendered


class TestCredentialEntry:
    def test_the_input_is_masked(self, qapp: object, tmp_path: Path) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        assert panel._credential_input.echoMode() is QLineEdit.EchoMode.Password

    def test_the_input_is_never_prefilled(self, qapp: object, tmp_path: Path) -> None:
        controller = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        panel = SecurityPanel(controller)
        _select(panel, "openrouter")
        assert panel._credential_input.text() == ""

    def test_the_input_is_cleared_after_a_save(self, qapp: object, tmp_path: Path) -> None:
        controller = _controller(tmp_path)
        panel = SecurityPanel(controller)
        _select(panel, "openrouter")
        panel._credential_input.setText(OPENROUTER_KEY)
        panel._on_save_credential()
        assert panel._credential_input.text() == ""
        assert controller.credential_status("openrouter").value == "active"

    def test_the_input_is_cleared_when_the_panel_closes(self, qapp: object, tmp_path: Path) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        panel._credential_input.setText(OPENROUTER_KEY)
        panel.close()
        assert panel._credential_input.text() == ""

    def test_there_is_no_reveal_control(self, qapp: object, tmp_path: Path) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        rendered = " ".join(
            [
                panel._save_button.text(),
                panel._disable_button.text(),
                panel._enable_button.text(),
                panel._remove_button.text(),
            ]
        ).lower()
        assert "reveal" not in rendered
        assert "show" not in rendered
        assert "copy" not in rendered

    def test_the_status_line_shows_a_masked_description(self, qapp: object, tmp_path: Path) -> None:
        controller = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        panel = SecurityPanel(controller)
        _select(panel, "openrouter")
        panel.refresh()
        assert "******" in panel._credential_masked.text()
        assert OPENROUTER_KEY not in panel._credential_masked.text()


class TestMigrationBanner:
    def test_the_banner_is_hidden_when_there_is_nothing_to_migrate(
        self, qapp: object, tmp_path: Path
    ) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        assert panel._migration_box.isVisibleTo(panel) is False

    def test_the_banner_explains_the_legacy_identity(self, qapp: object, tmp_path: Path) -> None:
        store = FakeSecretStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        panel = SecurityPanel(_controller(tmp_path, store))
        assert panel._migration_box.isVisibleTo(panel) is True
        text = panel._migration_text.text()
        assert "provider:openai_compatible:api_key" in text
        assert "not been used, moved or deleted" in text
        assert OPENROUTER_KEY not in text

    def test_the_confirm_button_needs_a_provider_choice(self, qapp: object, tmp_path: Path) -> None:
        store = FakeSecretStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        panel = SecurityPanel(_controller(tmp_path, store))
        assert panel._migration_button.isEnabled() is False
        index = panel._migration_provider.findData("openrouter")
        panel._migration_provider.setCurrentIndex(index)
        panel.refresh()
        assert panel._migration_button.isEnabled() is True

    def test_the_banner_never_mentions_the_key(self, qapp: object, tmp_path: Path) -> None:
        store = FakeSecretStore()
        store.set("provider:openai_compatible:api_key", OPENROUTER_KEY)
        panel = SecurityPanel(_controller(tmp_path, store))
        assert OPENROUTER_KEY not in panel._migration_text.text()


class TestEndpointStatus:
    def test_an_approved_endpoint_is_shown_as_approved(self, qapp: object, tmp_path: Path) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        assert "Approved cloud endpoint" in panel._endpoint_label.text()

    def test_a_blocked_endpoint_is_shown_as_blocked(self, qapp: object, tmp_path: Path) -> None:
        controller = _controller(tmp_path)
        controller._manager.config.provider.base_url = "https://192.168.1.20:8443/v1"
        controller._manager.config.provider.provider_id = None
        panel = SecurityPanel(controller)
        assert "Blocked" in panel._endpoint_label.text()

    def test_the_trust_control_only_appears_for_a_custom_endpoint(
        self, qapp: object, tmp_path: Path
    ) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        assert panel._trust_button.isVisibleTo(panel) is False


class TestAuditViewer:
    def test_the_audit_table_is_empty_at_the_start(self, qapp: object, tmp_path: Path) -> None:
        panel = SecurityPanel(_controller(tmp_path))
        assert panel._audit_table.rowCount() == 0

    def test_events_are_rendered(self, qapp: object, tmp_path: Path) -> None:
        controller = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        panel = SecurityPanel(controller)
        assert panel._audit_table.rowCount() >= 1
        rendered = " ".join(
            _audit_cell(panel, row, column)
            for row in range(panel._audit_table.rowCount())
            for column in range(panel._audit_table.columnCount())
        )
        assert "credential.created" in rendered
        assert OPENROUTER_KEY not in rendered

    def test_the_audit_viewer_is_labelled_session_only(self, qapp: object, tmp_path: Path) -> None:
        from PySide6.QtWidgets import QLabel

        panel = SecurityPanel(_controller(tmp_path))
        text = " ".join(
            label.text()
            for label in panel.findChildren(QLabel)
            if "session" in label.text().lower()
        )
        assert "Session-only" in text

    def test_the_filter_by_provider_narrows_the_table(self, qapp: object, tmp_path: Path) -> None:
        controller = _controller(tmp_path)
        controller.store_credential("openrouter", OPENROUTER_KEY)
        controller.store_credential("groq", "sk-canary-groq-2222222222")
        panel = SecurityPanel(controller)
        total = panel._audit_table.rowCount()
        index = panel._filter_provider.findData("groq")
        panel._filter_provider.setCurrentIndex(index)
        panel.refresh()
        assert panel._audit_table.rowCount() < total


class TestDialogBehaviour:
    def test_the_dialog_clears_the_input_when_it_is_cancelled(
        self, qapp: object, tmp_path: Path
    ) -> None:
        dialog = SecurityDialog(_controller(tmp_path))
        dialog.panel._credential_input.setText(OPENROUTER_KEY)
        dialog.reject()
        assert dialog.panel._credential_input.text() == ""

    def test_the_dialog_can_be_built_and_closed(self, qapp: object, tmp_path: Path) -> None:
        dialog = SecurityDialog(_controller(tmp_path))
        assert dialog.panel._provider_table.rowCount() > 0
        dialog.close()
