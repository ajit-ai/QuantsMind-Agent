"""Conversation persistence through the real application.

These drive two :class:`ApplicationContext` instances over the same paths, which
is what a restart actually is. The questions that matter are the ones a user would
ask: does my work come back, do the *wrong* things come back with it, and what
happens if the project has moved.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.app import ApplicationContext
from quantsmind_agent.app.settings import ConfigManager
from quantsmind_agent.schema import AssistantMessage, UserMessage
from quantsmind_agent.security.permissions import ApprovalOutcome
from quantsmind_agent.security.secrets import FileSecretStore
from tests.fakes import FakeModelProvider, text_response, tool_response

pytestmark = pytest.mark.integration


def _context(
    app_paths: Any,
    project: Path | None = None,
    provider: FakeModelProvider | None = None,
) -> ApplicationContext:
    """A running application, with the provider and approver already attached.

    Order matters: opening a project rebuilds the agent stack, so anything wired
    before that is thrown away. Tests that pass a project must therefore not
    assume an earlier ``set_approver`` survived.
    """

    manager = ConfigManager(app_paths, secret_store=FileSecretStore(app_paths.secrets_file))
    context = ApplicationContext.create(paths=app_paths, config_manager=manager)
    if project is not None:
        context.open_workspace(project, trusted=True)
    if provider is not None:
        context.agent._gateway.set_provider(provider)
    context.tools.permissions.set_approver(lambda request: ApprovalOutcome.ALLOW_ONCE)
    return context


def _edit(value: str) -> dict[str, Any]:
    return {
        "path": "mod.py",
        "operation": "replace",
        "old_text": "return 1",
        "new_text": value,
    }


def _user(text: str) -> UserMessage:
    return UserMessage(content=text)


def _assistant(text: str) -> AssistantMessage:
    return AssistantMessage(content=text)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "persisted_project"
    root.mkdir()
    (root / "mod.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (root / "README.md").write_text("# Project\n", encoding="utf-8")
    return root


class TestPersistenceAcrossARestart:
    def test_a_turn_is_saved_and_comes_back(self, app_paths: Any, project: Path) -> None:
        first = _context(
            app_paths, project, FakeModelProvider([text_response("The module returns one.")])
        )
        first.agent.run("What does mod.py do?")
        first.shutdown()

        # A restart is a second context over the same directories.
        second = _context(app_paths)

        assert second.resume_latest_conversation() is True
        transcript = [message.content for message in second.session.messages]
        assert any("What does mod.py do?" in text for text in transcript)
        assert any("returns one" in text for text in transcript)

    def test_the_workspace_is_reopened_for_a_resumed_conversation(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first.shutdown()

        second = _context(app_paths)
        second.resume_latest_conversation()

        assert second.workspace is not None
        assert second.workspace.root == project.resolve()

    def test_persistence_can_be_switched_off(self, app_paths: Any) -> None:
        context = _context(app_paths)
        context.config_manager.config.agent.persist_conversations = False
        context._build_store()

        assert context.conversations is None
        context.save_conversation()  # must not raise
        assert context.list_conversations() == []

    def test_a_new_conversation_saves_the_previous_one(self, app_paths: Any, project: Path) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("answer")]))
        context.agent.run("first question")
        first_id = context.session.id

        context.reset_conversation()

        assert [summary.id for summary in context.list_conversations()] == [first_id]
        assert context.session.messages == []

    def test_an_unfinished_turn_is_recorded_as_unfinished(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("answer")]))
        context.agent.run("hello")

        # A cancelled turn must not be recorded as a clean one.
        context._last_turn_finished = False
        context.shutdown()

        assert context.list_conversations()[0].finished is False

    def test_metadata_and_limits_are_recorded(self, app_paths: Any, project: Path) -> None:
        context = _context(app_paths, project)
        context.tools.execute("read_file", {"path": "README.md"})
        context.save_conversation()

        payload = context.conversations.load(context.session.id)  # type: ignore[union-attr]

        assert payload is not None
        assert payload["workspace"].endswith("persisted_project")
        assert payload["provider"]["name"]
        # The record states what did not travel with it, rather than leaving a
        # reader to assume everything did.
        assert set(payload["not_restored"]) == {
            "undo_history",
            "approval_grants",
            "pending_tool_calls",
        }


class TestNothingInappropriateComesBack:
    def test_undo_history_is_not_restored(self, app_paths: Any, project: Path) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.tools.execute("edit_file", _edit("return 2"))
        assert first.session.revisions.depth("mod.py") == 1
        first.shutdown()

        second = _context(app_paths)
        second.resume_latest_conversation()

        assert second.session.revisions.size == 0, "a restored pre-image outlives its session"
        assert not second.tools.execute("undo_file", {"path": "mod.py"}).ok

    def test_approval_grants_are_never_restored(self, app_paths: Any, project: Path) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first.shutdown()

        second = _context(app_paths)
        second.resume_latest_conversation()

        assert not second.tools.permissions.session_grants

    def test_a_pending_tool_call_is_not_replayed(self, app_paths: Any, project: Path) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        conversation_id = first.session.id
        first.shutdown()

        second = _context(app_paths)
        second.resume_latest_conversation()

        # A resumed conversation continues; it does not re-run anything.
        pending = [m for m in second.session.messages if getattr(m, "tool_calls", None)]
        assert pending == []
        assert second.session.id == conversation_id


class TestIsolationBetweenConversations:
    def test_switching_does_not_leak_history(self, app_paths: Any, project: Path) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("first answer")]))
        first.agent.run("the first question")
        first.reset_conversation()
        first.agent._gateway.set_provider(FakeModelProvider([text_response("second answer")]))
        first.agent.run("the second question")
        second_id = first.session.id

        first.reset_conversation()
        first.resume_latest_conversation()

        assert first.session.id == second_id, "the newest conversation wins"
        texts = [message.content for message in first.session.messages]
        assert any("second question" in text for text in texts)
        assert not any("first question" in text for text in texts)

    def test_switching_clears_the_undo_journal(self, app_paths: Any, project: Path) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        context.tools.execute("edit_file", _edit("return 2"))
        assert context.session.revisions.size == 1

        context.reset_conversation()
        context.resume_latest_conversation()

        assert context.session.revisions.size == 0

    def test_recorded_events_do_not_leak(self, app_paths: Any, project: Path) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        context.agent.run("hello")
        assert context.recorded_events

        context.reset_conversation()
        context.open_conversation(context.session.id)

        assert not any(e.type.value == "agent_completed" for e in context.recorded_events)


class TestMissingAndUnusableState:
    def test_an_unavailable_workspace_is_reported_not_substituted(
        self, app_paths: Any, tmp_path: Path
    ) -> None:
        project = tmp_path / "will_be_deleted"
        project.mkdir()
        (project / "mod.py").write_text("x = 1\n", encoding="utf-8")

        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first.shutdown()

        shutil.rmtree(project)

        second = _context(app_paths)
        assert second.resume_latest_conversation() is True
        assert second.workspace is None, "the wrong project must not be opened instead"
        assert str(project) in second.unavailable_workspace

    def test_a_corrupt_record_does_not_stop_the_application(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first.shutdown()
        assert first.conversations is not None
        broken = first.conversations.root / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")

        second = _context(app_paths)

        assert second.resume_latest_conversation() is True
        assert broken.with_suffix(".json.corrupt").exists()

    def test_opening_an_unknown_conversation_reports_failure(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(app_paths)
        context.open_workspace(project, trusted=True)

        assert context.open_conversation("does-not-exist") is False

    def test_deleting_and_renaming_go_through_the_application(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        context.agent.run("hello")
        conversation_id = context.session.id

        assert context.rename_conversation(conversation_id, "Parser work") is True
        assert context.list_conversations()[0].title == "Parser work"
        assert context.delete_conversation(conversation_id) is True
        assert context.list_conversations() == []


class TestResumingAtStartup:
    def test_startup_resumes_when_the_setting_is_on(self, app_paths: Any, project: Path) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("the earlier question")
        conversation_id = first.session.id
        first.shutdown()

        second = _context(app_paths)

        assert second.resume_on_startup() is True
        assert second.session.id == conversation_id
        assert second.resumed_conversation_id == conversation_id

    def test_startup_does_not_resume_when_the_setting_is_off(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first.shutdown()

        second = _context(app_paths)
        second.config_manager.config.agent.resume_last_conversation = False

        assert second.resume_on_startup() is False
        assert second.session.messages == []

    def test_a_named_conversation_can_be_resumed_instead_of_the_newest(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("one")]))
        context.agent.run("the first question")
        wanted = context.session.id
        context.reset_conversation()
        context.agent._gateway.set_provider(FakeModelProvider([text_response("two")]))
        context.agent.run("the second question")
        context.reset_conversation()

        assert context.resume_on_startup(conversation_id=wanted) is True
        assert context.session.id == wanted

    def test_a_named_conversation_does_not_override_the_switched_off_setting(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        conversation_id = first.session.id
        first.shutdown()

        second = _context(app_paths)
        second.config_manager.config.agent.resume_last_conversation = False

        # Turning resumption off means off, however it was asked for.
        assert second.resume_on_startup(conversation_id=conversation_id) is False

    def test_a_deleted_conversation_is_not_resumed_at_startup(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        context.agent.run("hello")
        conversation_id = context.session.id
        context.reset_conversation()
        context.delete_conversation(conversation_id)

        assert context.resume_on_startup() is False

    def test_an_unavailable_project_does_not_stop_the_resume(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first.shutdown()
        shutil.rmtree(project)

        second = _context(app_paths)

        assert second.resume_on_startup() is True
        assert second.workspace is None
        assert second.resumed_outcome == "ok"


class TestResumedHonesty:
    def test_a_resumed_conversation_reports_an_interrupted_last_turn(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first._last_turn_finished = False
        first.shutdown()

        second = _context(app_paths)
        second.resume_on_startup()

        assert second.resumed_outcome == "interrupted"

    def test_a_clean_turn_is_not_reported_as_interrupted(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first.shutdown()

        second = _context(app_paths)
        second.resume_on_startup()

        assert second.resumed_outcome == "ok"

    def test_a_resumed_conversation_admits_dropped_history(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(app_paths, project)
        context.config_manager.config.agent.max_stored_messages = 2
        context._build_store()
        for index in range(6):
            context.session.add_message(_user(f"question {index}"))
        context.save_conversation()
        context.reset_conversation()

        context.resume_on_startup()

        assert context.resumed_dropped_items > 0

    def test_timestamps_survive_a_restart_and_the_next_autosave(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        before = [(m.id, m.created_at) for m in first.session.messages]
        first.shutdown()

        second = _context(app_paths)
        second.resume_on_startup()
        assert [(m.id, m.created_at) for m in second.session.messages] == before

        # The autosave that follows the next turn must not rewrite the older
        # timestamps as "just now", which is how a transcript starts lying about its
        # own age without anyone noticing.
        second.agent._gateway.set_provider(FakeModelProvider([text_response("again")]))
        second.agent.run("another question")

        assert [(m.id, m.created_at) for m in second.session.messages][: len(before)] == before

    def test_tool_results_come_back_in_their_original_order(
        self, app_paths: Any, project: Path
    ) -> None:
        # Tools have to be driven through the agent: a bare ``tools.execute`` call is
        # a real result but never becomes part of the conversation.
        first = _context(
            app_paths,
            project,
            FakeModelProvider(
                [
                    tool_response([("read_file", {"path": "README.md"})]),
                    tool_response([("read_file", {"path": "mod.py"})]),
                    text_response("both files read"),
                ]
            ),
        )
        first.agent.run("read both files")
        first.save_conversation()
        first.shutdown()

        second = _context(app_paths)
        second.resume_on_startup()

        assert [result.tool for result in second.session.tool_results] == [
            "read_file",
            "read_file",
        ]
        assert [
            entry.tool_result.tool
            for entry in second.session.ordered_history()
            if entry.is_tool and entry.tool_result is not None
        ] == ["read_file", "read_file"]

    def test_a_resumed_conversation_leaves_no_approval_grants(
        self, app_paths: Any, project: Path
    ) -> None:
        first = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        first.agent.run("hello")
        first.shutdown()

        second = _context(app_paths)
        second.resume_on_startup()

        assert not second.tools.permissions.session_grants

    def test_switching_conversations_keeps_tool_results_apart(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(
            app_paths,
            project,
            FakeModelProvider(
                [
                    tool_response([("read_file", {"path": "README.md"})]),
                    text_response("read it"),
                ]
            ),
        )
        context.agent.run("read the readme")
        with_tools = context.session.id
        assert len(context.session.tool_results) == 1

        context.reset_conversation()
        assert context.session.tool_results == []
        assert context.session.ordered_history() == []

        context.open_conversation(with_tools)
        assert len(context.session.tool_results) == 1


class TestStorageLimitsThroughTheApplication:
    def test_the_configured_message_limit_bounds_the_record(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(app_paths, project)
        context.config_manager.config.agent.max_stored_messages = 3
        context._build_store()
        for index in range(8):
            context.session.add_message(_user(f"question {index}"))
        context.save_conversation()

        payload = context.conversations.load(context.session.id)  # type: ignore[union-attr]

        assert payload is not None
        assert len(payload["messages"]) == 3
        assert payload["truncated"]["dropped_messages"] == 5

    def test_long_tool_output_is_stored_within_the_limit(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(
            app_paths,
            project,
            FakeModelProvider(
                [
                    tool_response([("read_file", {"path": "big.txt"})]),
                    text_response("read it"),
                ]
            ),
        )
        context.config_manager.config.agent.max_stored_item_chars = 300
        context._build_store()
        big = project / "big.txt"
        big.write_text("z" * 20_000, encoding="utf-8")
        context.agent.run("read big.txt")

        payload = context.conversations.load(context.session.id)  # type: ignore[union-attr]

        assert payload is not None
        assert payload["tool_results"], "the tool call should still be recorded"
        # The bulk of a read lives in the payload, not the one-line summary, so that
        # is where the bound has to bite.
        stored = payload["tool_results"][0]["data"]["content"]
        assert "not stored" in stored
        assert len(stored) < 20_000

    def test_limits_are_applied_on_every_save_not_just_the_first(
        self, app_paths: Any, project: Path
    ) -> None:
        context = _context(app_paths, project)
        context.config_manager.config.agent.max_stored_messages = 3
        context._build_store()
        for index in range(6):
            context.session.add_message(_user(f"question {index}"))
            context.save_conversation()

        payload = context.conversations.load(context.session.id)  # type: ignore[union-attr]

        assert payload is not None
        assert len(payload["messages"]) == 3


class TestExportAndDeletion:
    def test_exporting_the_open_conversation_writes_markdown(
        self, app_paths: Any, project: Path, tmp_path: Path
    ) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("an answer")]))
        context.agent.run("a question worth exporting")

        written = context.export_conversation(tmp_path / "out.md")

        assert written is not None
        text = written.read_text(encoding="utf-8")
        assert "a question worth exporting" in text
        assert "an answer" in text

    def test_exporting_json_keeps_the_whole_record(
        self, app_paths: Any, project: Path, tmp_path: Path
    ) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("an answer")]))
        context.agent.run("hello")

        written = context.export_conversation(tmp_path / "out.json", fmt="json")

        assert written is not None
        assert json.loads(written.read_text(encoding="utf-8"))["id"] == context.session.id

    def test_exporting_a_conversation_that_is_not_stored_reports_failure(
        self, app_paths: Any, tmp_path: Path
    ) -> None:
        context = _context(app_paths)

        result = context.export_conversation(tmp_path / "out.md", conversation_id="nope")

        assert result is None

    def test_a_deleted_conversation_is_gone_from_every_path(
        self, app_paths: Any, project: Path, tmp_path: Path
    ) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        context.agent.run("hello")
        conversation_id = context.session.id

        assert context.delete_conversation(conversation_id) is True

        assert context.list_conversations() == []
        assert context.open_conversation(conversation_id) is False
        assert (
            context.export_conversation(tmp_path / "out.md", conversation_id=conversation_id)
            is None
        )
        assert context.resume_on_startup() is False

    def test_deleting_twice_is_harmless_and_says_so(self, app_paths: Any, project: Path) -> None:
        context = _context(app_paths, project, FakeModelProvider([text_response("done")]))
        context.agent.run("hello")
        conversation_id = context.session.id

        assert context.delete_conversation(conversation_id) is True
        assert context.delete_conversation(conversation_id) is False
