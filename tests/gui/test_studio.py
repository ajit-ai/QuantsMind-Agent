"""Tests for the Engineering Studio destinations.

The window tests use the real application context and the real task store. The
widget tests drive literal snapshots, so the presentation can be checked without
starting a provider or a worker.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QLabel, QListWidget, QPushButton, QWidget

from quantsmind_agent.gui.studio.changesets import (
    ChangeCheck,
    ChangeFile,
    ChangeSetRecord,
    EngineeringChangeSets,
)
from quantsmind_agent.gui.studio.control_center import ControlCenter, ControlSnapshot
from quantsmind_agent.gui.studio.home import (
    EngineeringHome,
    HomeConversation,
    HomeSnapshot,
    HomeTask,
)
from quantsmind_agent.gui.studio.navigation import DESTINATIONS, Destination, StudioHeader
from quantsmind_agent.services.tasks.models import (
    CheckResult,
    CheckStatus,
    FileChangeRecord,
    Plan,
    PlannedStep,
    Task,
    TaskStatus,
)
from tests.gui.harness import GuiHarness

pytestmark = pytest.mark.gui


def _record_task(window: object, workspace: Path, task_id: str = "studio-task") -> Task:
    """Store a completed task with real change evidence."""

    task = Task(
        id=task_id,
        objective="Add a health check endpoint",
        workspace=str(workspace),
        status=TaskStatus.COMPLETED,
        plan=Plan(
            objective="Add a health check endpoint",
            steps=(
                PlannedStep(
                    id="step-1",
                    description="Write the health module",
                    tool="write_file",
                    expected_paths=("src/health.py",),
                ),
            ),
            scope_paths=("src/health.py",),
            acceptance_criteria=(),
        ),
        file_changes=[
            FileChangeRecord(
                path="src/health.py",
                operation="created",
                existed_before=False,
                exists_after=True,
                bytes_after=120,
            )
        ],
        checks=[
            CheckResult(
                check_id="pytest",
                command=("pytest",),
                status=CheckStatus.PASSED,
                exit_code=0,
            )
        ],
        updated_at=time.time(),
    )
    task_service = window._task_service  # type: ignore[attr-defined]
    task_service.store.save(task)
    return task


class TestWindowDestinations:
    def test_the_window_opens_on_engineering_home(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        assert window._destination == "home"
        assert window._destinations.currentWidget() is window.home_view
        assert window.header.current() == "home"
        assert [item.id for item in DESTINATIONS] == [
            "home",
            "ai",
            "development",
            "changes",
            "control",
        ]

    def test_the_header_switches_every_destination(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        for destination in DESTINATIONS:
            window.header._buttons[destination.id].click()
            assert window._destination == destination.id
            assert window._destinations.currentWidget() is window._destination_pages[destination.id]
            assert window.describe_structure()["destination"] == destination.id

    def test_development_activity_selects_the_studio_destination(
        self, gui_harness: GuiHarness
    ) -> None:
        window = gui_harness.window
        window._show_activity("search")
        assert window._destination == "development"
        assert window.header.current() == "development"
        assert window._side_stack.currentWidget() is window.search_panel
        assert window.activity_bar.current() == "search"

    def test_agent_views_switch_to_ai_engineering(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        window.show_destination("home")
        window.show_task_view()
        assert window._destination == "ai"
        assert window._views.currentIndex() == 1
        window.show_destination("home")
        window.show_chat_view()
        assert window._destination == "ai"
        assert window._views.currentIndex() == 0

    def test_fresh_install_home_is_honest(self, gui_harness: GuiHarness) -> None:
        window = gui_harness.window
        state = window.home_view.describe()
        assert state["workspace"] == ""
        assert state["active_tasks"] == 0
        assert state["review_count"] == 0
        assert state["conversations"] == 0
        assert state["empty_states"] == 2
        assert state["banner_visible"] is False
        assert window.home_view.project_stat.value_label.text() == "No project"
        assert window.home_view.tasks_stat.value_label.text() == "None"
        assert window.home_view.review_stat.value_label.text() == "0"

    def test_opening_a_project_updates_home_and_header(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        window = gui_harness.window
        state = window.home_view.describe()
        assert state["workspace"] == str(sample_project.resolve())
        assert state["trust"] == "trusted"
        assert state["conversations"] == 0
        assert window.header.current() == "development"
        assert window.header.workspace_chip.text() == sample_project.resolve().name
        assert window.header.trust_chip.text() == "Trusted"
        assert window.header.model_chip.text() == "gpt-4o-mini"

    def test_a_recorded_change_becomes_a_reviewable_record(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        window = gui_harness.window
        assert gui_harness.context.workspace is not None
        _record_task(window, gui_harness.context.workspace.root)

        window._refresh_studio_snapshots()

        state = window.home_view.describe()
        assert state["review_count"] == 1
        assert window.changesets_view.describe()["records"] == 1
        assert window.changesets_view.describe()["files"] == 1
        # The Apply button is present (permission-gated, disabled for a
        # completed task that was never approved).
        assert window.changesets_view.describe()["has_apply_button"] is True
        assert window.changesets_view._selected_id == "studio-task"
        buttons = [
            button.text() for button in window.changesets_view.detail_host.findChildren(QPushButton)
        ]
        assert "Apply ChangeSet" in buttons

    def test_opening_a_stored_change_inspects_it_without_running(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        window = gui_harness.window
        assert gui_harness.context.workspace is not None
        _record_task(window, gui_harness.context.workspace.root)
        window._refresh_studio_snapshots()

        window.changesets_view.task_open_requested.emit("studio-task")

        assert window._destination == "ai"
        assert window._views.currentIndex() == 1
        assert window._inspecting is not None
        assert window._inspecting.id == "studio-task"
        assert window.task_panel.worker_running is False

    def test_control_center_never_renders_the_credential_value(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        gui_harness.open_project(sample_project)
        window = gui_harness.window
        credential = "studio-control-center-test-key"
        gui_harness.context.config_manager.set_api_key(credential)
        window._refresh_studio_snapshots()

        rendered = " ".join(label.text() for label in window.control_center.findChildren(QLabel))
        assert credential not in rendered
        assert window.control_center.describe()["credential"] != credential


class TestHomeWidget:
    def test_empty_snapshot_explains_how_to_start(self, qapp: object) -> None:
        home = EngineeringHome()
        state = home.describe()
        assert state["empty_states"] == 2
        assert state["banner_visible"] is False
        assert home.project_stat.value_label.text() == "No project"
        home.deleteLater()

    def test_snapshot_paints_real_facts_and_routes_task_click(self, qapp: object) -> None:
        home = EngineeringHome()
        opened: list[str] = []
        home.task_open_requested.connect(opened.append)
        home.set_snapshot(
            HomeSnapshot(
                workspace="/work/quant",
                workspace_name="quant",
                trust="trusted",
                model="model-x",
                active_tasks=(
                    HomeTask(
                        id="task-1",
                        objective="Fix the parser",
                        status="running",
                        steps_done=1,
                        steps_total=3,
                        updated_at=time.time(),
                    ),
                ),
                conversations=(HomeConversation(id="c1", title="Parser work"),),
            )
        )
        assert home.project_stat.value_label.text() == "quant"
        assert home.tasks_stat.value_label.text() == "1"
        assert home.agent_stat.value_label.text() == "Ready"
        assert home.describe()["conversations"] == 1

        widget = home.active_host.findChild(QListWidget)
        assert widget is not None
        widget.itemClicked.emit(widget.item(0))
        assert opened == ["task-1"]
        home.deleteLater()

    def test_banner_offers_trust_or_provider_action(self, qapp: object) -> None:
        home = EngineeringHome()
        trusted: list[bool] = []
        settings: list[bool] = []
        home.trust_requested.connect(lambda: trusted.append(True))
        home.settings_requested.connect(lambda: settings.append(True))

        home.set_snapshot(HomeSnapshot(workspace="/work/q", trust="restricted"))
        assert home.banner.isHidden() is False
        home.banner_button.click()
        assert trusted == [True]

        home.set_snapshot(HomeSnapshot(workspace="/work/q", trust="trusted", readiness="No key"))
        home.banner_button.click()
        assert settings == [True]
        home.deleteLater()


class TestChangeSetWidget:
    def _record(self) -> ChangeSetRecord:
        return ChangeSetRecord(
            task_id="task-9",
            objective="Add the health endpoint",
            status="completed",
            workspace="/work/q",
            updated_at=time.time(),
            approval="approved",
            verification="pass",
            plan_summary="2 planned step(s)",
            expected_paths=("src/health.py",),
            files=(ChangeFile("src/health.py", "created", bytes_after=120),),
            checks=(ChangeCheck("pytest", "passed", exit_code=0),),
        )

    def test_records_render_evidence_and_offer_no_apply(self, qapp: object) -> None:
        view = EngineeringChangeSets()
        view.set_records((self._record(),))
        assert view.describe()["records"] == 1
        assert view.describe()["files"] == 1
        # The Apply button is present but disabled: no context wires the engine,
        # and the record's status is "completed", not "approved".
        assert view.describe()["has_apply_button"] is True
        assert view.describe()["apply_enabled"] is False
        assert view.describe()["rollback_enabled"] is False
        rendered = " ".join(label.text() for label in view.detail_host.findChildren(QLabel))
        assert "src/health.py" in rendered
        assert "pytest" in rendered

        opened: list[str] = []
        view.task_open_requested.connect(opened.append)
        button = next(
            item
            for item in view.detail_host.findChildren(QPushButton)
            if item.text() == "Open task record"
        )
        button.click()
        assert opened == ["task-9"]
        view.deleteLater()

    def test_a_planned_change_without_actual_files_is_explainable(self, qapp: object) -> None:
        view = EngineeringChangeSets()
        planned = ChangeSetRecord(
            task_id="task-10",
            objective="Waiting for approval",
            status="awaiting_approval",
            expected_paths=("src/app.py",),
        )
        view.set_records((planned,))
        assert view.describe()["records"] == 1
        assert view.describe()["files"] == 0
        view.deleteLater()

    def test_apply_button_enabled_only_for_approved_record(self, qapp: object) -> None:
        """Apply is enabled for an approved record when the engine is wired."""

        class FakeContext:
            changeset_applier = object()
            changeset_rollback = object()

        approved = ChangeSetRecord(
            task_id="task-11",
            objective="Approved change",
            status="approved",
            approval="approved",
            workspace="/work/q",
            files=(ChangeFile("src/app.py", "created", bytes_after=10),),
        )
        not_approved = ChangeSetRecord(
            task_id="task-12",
            objective="Not approved",
            status="completed",
            approval="no decision",
            workspace="/work/q",
            files=(ChangeFile("src/app.py", "modified", bytes_after=10),),
        )

        view = EngineeringChangeSets(context=FakeContext())
        view.set_records((approved, not_approved))
        view._on_selection(view.listing.item(0), None)
        assert view.describe()["apply_enabled"] is True
        view._on_selection(view.listing.item(1), None)
        assert view.describe()["apply_enabled"] is False
        view.deleteLater()

    def test_rollback_button_enabled_only_for_applied_record(self, qapp: object) -> None:
        """Rollback is enabled for an applied record when the engine is wired."""

        class FakeContext:
            changeset_applier = object()
            changeset_rollback = object()

        applied = ChangeSetRecord(
            task_id="task-13",
            objective="Applied change",
            status="applied",
            approval="approved",
            workspace="/work/q",
            files=(ChangeFile("src/app.py", "modified", bytes_after=10),),
        )
        not_applied = ChangeSetRecord(
            task_id="task-14",
            objective="Not applied",
            status="approved",
            approval="approved",
            workspace="/work/q",
            files=(ChangeFile("src/app.py", "created", bytes_after=10),),
        )

        view = EngineeringChangeSets(context=FakeContext())
        view.set_records((applied, not_applied))
        view._on_selection(view.listing.item(0), None)
        assert view.describe()["rollback_enabled"] is True
        view._on_selection(view.listing.item(1), None)
        assert view.describe()["rollback_enabled"] is False
        view.deleteLater()

    def test_apply_and_rollback_signals_emit_task_id(self, qapp: object) -> None:
        """Clicking Apply/Rollback emits the task id."""

        class FakeContext:
            changeset_applier = object()
            changeset_rollback = object()

        applied = ChangeSetRecord(
            task_id="task-15",
            objective="Applied change",
            status="applied",
            approval="approved",
            workspace="/work/q",
            files=(ChangeFile("src/app.py", "modified", bytes_after=10),),
        )
        view = EngineeringChangeSets(context=FakeContext())
        view.set_records((applied,))
        view._on_selection(view.listing.item(0), None)

        applied_signals: list[str] = []
        rolled_back_signals: list[str] = []
        view.apply_requested.connect(applied_signals.append)
        view.rollback_requested.connect(rolled_back_signals.append)

        apply_button = next(
            b for b in view.detail_host.findChildren(QPushButton) if b.text() == "Apply ChangeSet"
        )
        rollback_button = next(
            b
            for b in view.detail_host.findChildren(QPushButton)
            if b.text() == "Rollback ChangeSet"
        )
        # Apply is disabled for "applied" status (only "approved"/"proposed"/"conflicted").
        apply_button.click()
        assert applied_signals == []
        # Rollback is enabled for "applied".
        rollback_button.click()
        assert rolled_back_signals == ["task-15"]
        view.deleteLater()


class TestControlCenterWidget:
    def test_trust_actions_follow_the_actual_state(self, qapp: object) -> None:
        center = ControlCenter()
        actions: list[str] = []
        center.trust_requested.connect(lambda: actions.append("trust"))
        center.revoke_requested.connect(lambda: actions.append("revoke"))
        center.restrict_requested.connect(lambda: actions.append("restrict"))

        center.set_snapshot(ControlSnapshot(workspace="/work/q", trust="restricted"))
        _click(center, "Trust workspace")
        assert actions == ["trust"]

        actions.clear()
        center.set_snapshot(ControlSnapshot(workspace="/work/q", trust="trusted"))
        _click(center, "Revoke trust")
        _click(center, "Restrict workspace")
        assert actions == ["revoke", "restrict"]
        center.deleteLater()

    def test_policy_facts_are_displayed_without_credential_values(self, qapp: object) -> None:
        center = ControlCenter()
        center.set_snapshot(
            ControlSnapshot(
                provider_name="openai",
                provider_id="openai",
                model="model-x",
                base_url="https://api.example.test/v1",
                credential="(stored securely)",
                readiness="",
                max_iterations=8,
                max_context_files=6,
                max_file_bytes=256_000,
                command_timeout=120,
                auto_approve_reads=True,
                audit_events=3,
                audit_denials=1,
                config_dir="C:/config",
                data_dir="C:/data",
                log_dir="C:/logs",
                memory="120 MB (normal)",
            )
        )
        state = center.describe()
        assert state["ready"] is True
        assert state["audit_denials"] == 1
        rendered = " ".join(label.text() for label in center.findChildren(QLabel))
        assert "https://api.example.test/v1" in rendered
        assert "Iteration limit 8" in rendered
        assert "250 KB" in rendered
        center.deleteLater()

    def test_the_audit_store_is_described_without_claiming_protection(self, qapp: object) -> None:
        center = ControlCenter()
        center.set_snapshot(
            ControlSnapshot(
                workspace="/work/q",
                audit_path="C:/data/audit/audit.jsonl",
                audit_records=42,
                audit_bytes_used=512_000,
                audit_ceiling_bytes=15_000_000,
                audit_chain_ok=True,
                audit_chain_note="42 records across 1 segment(s), chain intact",
            )
        )
        rendered = " ".join(label.text() for label in center.findChildren(QLabel))
        assert "42 record(s)" in rendered
        assert "500 KiB of 14648 KiB" in rendered
        assert "audit.jsonl" in rendered
        assert "not proof against tampering" in rendered
        assert center.describe()["audit_chain_ok"] is True
        center.deleteLater()

    def test_a_broken_chain_is_shown_instead_of_the_reassuring_note(self, qapp: object) -> None:
        center = ControlCenter()
        center.set_snapshot(
            ControlSnapshot(
                workspace="/work/q",
                audit_path="C:/data/audit/audit.jsonl",
                audit_records=4,
                audit_chain_ok=False,
                audit_chain_note="chain broken at record #3: digest does not match",
            )
        )
        rendered = " ".join(label.text() for label in center.findChildren(QLabel))
        assert "The audit trail is incomplete." in rendered
        assert "digest does not match" in rendered
        assert "not proof against tampering" not in rendered
        center.deleteLater()

    def test_a_store_changed_while_closed_is_reported_at_startup(self, qapp: object) -> None:
        center = ControlCenter()
        center.set_snapshot(
            ControlSnapshot(
                workspace="/work/q",
                audit_path="C:/data/audit/audit.jsonl",
                audit_records=4,
                audit_chain_ok=True,
                audit_chain_note="4 records across 1 segment(s), chain intact",
                audit_recovery_note="2 record(s) are missing after the last one the anchor witnessed",
            )
        )
        rendered = " ".join(label.text() for label in center.findChildren(QLabel))
        assert "did not match its own witness when this session opened" in rendered
        assert "changed while the application was closed" in rendered
        center.deleteLater()

    def test_export_is_an_explicit_action_with_no_default_destination(self, qapp: object) -> None:
        center = ControlCenter()
        requested: list[str] = []
        center.audit_export_requested.connect(lambda: requested.append("export"))

        center.set_snapshot(ControlSnapshot(workspace="/work/q", audit_records=1))
        _click(center, "Export audit evidence")

        assert requested == ["export"]
        center.deleteLater()


class TestHeaderWidget:
    def test_select_and_current_track_the_destination(self, qapp: object) -> None:
        header = StudioHeader(
            (
                Destination("home", "Home", "overview", "Ctrl+1"),
                Destination("ai", "AI Engineering", "tasks", "Ctrl+2"),
            )
        )
        chosen: list[str] = []
        header.destination_selected.connect(chosen.append)
        header.select("ai")
        assert header.current() == "ai"
        header._buttons["home"].click()
        assert chosen == ["home"]
        assert header.current() == "home"
        header.deleteLater()


def _click(widget: QWidget, text: str) -> None:
    button = next(item for item in widget.findChildren(QPushButton) if item.text() == text)
    button.click()
