"""The conversation list in the sidebar, driven through the real window.

A durable conversation is only useful if a person can get back to it, so these
exercise the sidebar wiring rather than the store: choosing an entry reopens the
conversation, New saves what was open, and Delete removes it.
"""

from __future__ import annotations

from pathlib import Path

from quantsmind_agent.permissions import ApprovalOutcome
from tests.fakes import FakeModelProvider, text_response
from tests.gui.harness import GuiHarness


def _context_ready(gui_harness: GuiHarness) -> None:
    gui_harness.context.tools.permissions.set_approver(lambda request: ApprovalOutcome.ALLOW_ONCE)


class TestConversationList:
    def test_a_new_conversation_appears_in_the_list(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question worth keeping")

        assert gui_harness.window.sidebar.conversations_available()

    def test_starting_a_new_one_keeps_the_old(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("first question")
        first_id = gui_harness.context.session.id

        gui_harness.window._on_new_conversation()

        summaries = gui_harness.context.list_conversations()
        assert first_id in {summary.id for summary in summaries}
        assert gui_harness.context.session.id != first_id

    def test_choosing_a_stored_conversation_reopens_it(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("the first answer")]))
        gui_harness.ask("the first question")
        first_id = gui_harness.context.session.id
        gui_harness.window._on_new_conversation()

        gui_harness.window._on_conversation_selected(first_id)

        assert gui_harness.context.session.id == first_id
        assert "the first question" in gui_harness.transcript

    def test_deleting_removes_it_from_the_list(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_conversation_delete_requested(conversation_id)

        assert conversation_id not in {
            summary.id for summary in gui_harness.context.list_conversations()
        }
        assert "deleted" in gui_harness.transcript.lower()

    def test_deleting_an_unknown_conversation_is_reported_not_crashed(
        self, gui_harness: GuiHarness
    ) -> None:
        gui_harness.window._on_conversation_delete_requested("nope")

        assert "nothing to delete" in gui_harness.transcript.lower()

    def test_a_missing_workspace_after_resuming_is_explained(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id
        gui_harness.window._on_new_conversation()

        import shutil

        shutil.rmtree(sample_project)
        gui_harness.window._on_conversation_selected(conversation_id)

        assert "not available" in gui_harness.transcript.lower()

    def test_choosing_the_open_conversation_does_nothing(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_conversation_selected(conversation_id)

        assert gui_harness.context.session.id == conversation_id
        assert "Reopened" not in gui_harness.transcript

    def test_renaming_shows_the_new_name_in_the_list(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_conversation_rename_requested(conversation_id, "Refactor plan")

        assert gui_harness.context.conversation_title == "Refactor plan"
        summary = next(
            item for item in gui_harness.context.list_conversations() if item.id == conversation_id
        )
        assert summary.title == "Refactor plan"
        # The listed label carries the timestamp too, so match on the name.
        assert any(
            "Refactor plan" in label for label in gui_harness.window.sidebar.conversation_labels()
        )

    def test_renaming_the_open_conversation_survives_the_next_save(
        self, gui_harness: GuiHarness, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("one"), text_response("two")]))
        gui_harness.ask("first question")
        conversation_id = gui_harness.context.session.id
        gui_harness.window._on_conversation_rename_requested(conversation_id, "Keep me")

        gui_harness.ask("second question")

        store = gui_harness.context.conversations
        assert store is not None
        payload = store.load(conversation_id)
        assert payload is not None
        assert payload["title"] == "Keep me"

    def test_a_blank_rename_is_refused(self, gui_harness: GuiHarness, sample_project: Path) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("done")]))
        gui_harness.ask("a question")
        conversation_id = gui_harness.context.session.id

        gui_harness.window._on_conversation_rename_requested(conversation_id, "   ")

        assert gui_harness.context.conversation_title != ""
        assert "was not applied" in gui_harness.transcript

    def test_renaming_an_unknown_conversation_is_reported_not_crashed(
        self, gui_harness: GuiHarness
    ) -> None:
        gui_harness.window._on_conversation_rename_requested("nope", "Title")

        assert "could not be renamed" in gui_harness.transcript.lower()

    def test_a_stale_unavailable_project_is_not_reported_against_the_next_conversation(
        self, gui_harness: GuiHarness, tmp_path: Path, sample_project: Path
    ) -> None:
        _context_ready(gui_harness)
        gui_harness.open_project(sample_project)
        gui_harness.use_provider(FakeModelProvider([text_response("one"), text_response("two")]))
        gui_harness.ask("first question")
        lost_id = gui_harness.context.session.id
        gui_harness.window._on_new_conversation()

        second_project = tmp_path / "second"
        second_project.mkdir()
        (second_project / "mod.py").write_text("x = 1\n", encoding="utf-8")
        gui_harness.open_project(second_project)
        gui_harness.ask("second question")
        healthy_id = gui_harness.context.session.id
        gui_harness.window._on_new_conversation()

        import shutil

        shutil.rmtree(sample_project)
        gui_harness.window._on_conversation_selected(lost_id)
        assert gui_harness.context.unavailable_workspace == str(sample_project)

        gui_harness.window._on_conversation_selected(healthy_id)

        assert gui_harness.context.unavailable_workspace == ""
        assert "not available" not in gui_harness.transcript.rsplit("Reopened", 1)[-1]
