"""Ordered history, storage limits and export for durable conversations.

Two questions here are about honesty rather than mechanics: does a reopened
conversation put things back in the order they happened, and does it admit what it
is not showing? A transcript that is quietly out of order, or that implies a turn
finished when it did not, is worse than no transcript at all.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.runtime.tools import ToolResult
from quantsmind_agent.schema import AssistantMessage, SystemMessage, ToolCall, UserMessage
from quantsmind_agent.services.conversations.session import Session
from quantsmind_agent.services.conversations.store import (
    SCHEMA_VERSION,
    ConversationStore,
    StorageLimits,
    render_json,
    render_markdown,
    snapshot_session,
    truncate,
)


def _kinds(session: Session) -> list[str]:
    """The ordered history as plain role/tool names, for readable assertions."""

    names: list[str] = []
    for entry in session.ordered_history():
        if entry.tool_result is not None:
            names.append(entry.tool_result.tool)
        elif entry.message is not None:
            names.append(entry.message.role.value)
    return names


def _contents(session: Session) -> list[str]:
    """Ordered history as readable labels, including assistant request vs answer."""

    labels: list[str] = []
    for entry in session.ordered_history():
        if entry.tool_result is not None:
            labels.append(f"tool:{entry.tool_result.tool}")
        elif entry.message is not None:
            labels.append(f"{entry.message.role.value}:{entry.message.content}")
    return labels


def _live_session() -> Session:
    """A session that actually used tools, with known relative times."""

    session = Session()
    session.add_message(UserMessage(content="read the module", created_at=1000.0))
    session.record_tool_result(
        ToolResult.success("read_file", "read mod.py", {"path": "mod.py"}, created_at=1010.0)
    )
    session.record_tool_result(
        ToolResult.failure(
            "edit_file",
            "nothing matched",
            error="old_text not found",
            created_at=1020.0,
        )
    )
    session.add_message(AssistantMessage(content="It returns one.", created_at=1030.0))
    return session


def _record(conversation_id: str = "conv-1", **extra: Any) -> dict[str, Any]:
    """A record for a live session, with any fields the test wants overridden."""

    snapshot_kwargs = {"finished": extra.pop("finished", True)}
    payload = snapshot_session(
        _live_session(),
        provider={"name": "openai", "model": "gpt", "base_url": "https://api.test/v1"},
        **snapshot_kwargs,
    )
    payload["id"] = conversation_id
    payload.update(extra)
    return payload


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversations")


class TestOrderedHistory:
    def test_tool_results_interleave_with_the_messages_around_them(self) -> None:
        assert _kinds(_live_session()) == ["user", "read_file", "edit_file", "assistant"]

    def test_the_ordering_follows_the_clock_not_the_storage_order(
        self, store: ConversationStore
    ) -> None:
        session = Session()
        # Tool calls recorded first on purpose: the merge is what must fix this.
        session.record_tool_result(ToolResult.success("edit_file", "second", created_at=1020.0))
        session.record_tool_result(ToolResult.success("read_file", "first", created_at=1010.0))
        session.add_message(UserMessage(content="read the module", created_at=1000.0))
        session.add_message(AssistantMessage(content="It returns one.", created_at=1030.0))
        payload = snapshot_session(session)
        payload["id"] = "conv-shuffled"
        store.save(payload)

        reopened = Session()
        assert reopened.restore_from(store.load("conv-shuffled") or {})

        assert _kinds(reopened) == ["user", "read_file", "edit_file", "assistant"]

    def test_each_entry_carries_the_time_it_happened(self) -> None:
        entries = _live_session().ordered_history()

        assert [entry.created_at for entry in entries] == [1000.0, 1010.0, 1020.0, 1030.0]

    def test_messages_win_a_tie_so_a_tool_call_never_precedes_its_ask(self) -> None:
        session = Session()
        session.add_message(UserMessage(content="go", created_at=500.0))
        session.record_tool_result(
            ToolResult.success("read_file", "same instant", created_at=500.0)
        )

        entries = session.ordered_history()

        assert entries[0].message is not None
        assert entries[1].is_tool

    def test_a_tool_result_precedes_the_final_answer_on_a_timestamp_tie(self) -> None:
        session = Session()
        session.add_message(UserMessage(content="read it", created_at=500.0))
        session.add_message(
            AssistantMessage(
                content="checking",
                tool_calls=[ToolCall(id="call-1", name="read_file")],
                created_at=500.0,
            )
        )
        session.record_tool_result(ToolResult.success("read_file", "read mod.py", created_at=500.0))
        session.add_message(AssistantMessage(content="It returns one.", created_at=500.0))

        assert _contents(session) == [
            "user:read it",
            "assistant:checking",
            "tool:read_file",
            "assistant:It returns one.",
        ]

    def test_a_system_message_precedes_a_user_message_on_a_timestamp_tie(self) -> None:
        session = Session()
        session.add_message(SystemMessage(content="be careful", created_at=500.0))
        session.add_message(UserMessage(content="go", created_at=500.0))

        assert _contents(session) == ["system:be careful", "user:go"]

    def test_a_restored_tied_history_keeps_the_semantic_order(
        self, store: ConversationStore
    ) -> None:
        session = Session()
        session.add_message(SystemMessage(content="be careful", created_at=500.0))
        session.add_message(UserMessage(content="read it", created_at=500.0))
        session.add_message(
            AssistantMessage(
                content="checking",
                tool_calls=[ToolCall(id="call-1", name="read_file")],
                created_at=500.0,
            )
        )
        session.record_tool_result(ToolResult.success("read_file", "read mod.py", created_at=500.0))
        session.add_message(AssistantMessage(content="It returns one.", created_at=500.0))
        store.save(snapshot_session(session))

        reopened = Session()
        assert reopened.restore_from(store.load(session.id) or {})

        assert _contents(reopened) == [
            "system:be careful",
            "user:read it",
            "assistant:checking",
            "tool:read_file",
            "assistant:It returns one.",
        ]

    def test_tool_results_survive_a_restore(self, store: ConversationStore) -> None:
        store.save(_record("conv-tools"))

        reopened = Session()
        assert reopened.restore_from(store.load("conv-tools") or {})

        assert [result.tool for result in reopened.tool_results] == ["read_file", "edit_file"]
        assert reopened.tool_results[1].error == "old_text not found"

    def test_turns_survive_a_restore_so_durations_stay_honest(
        self, store: ConversationStore
    ) -> None:
        session = _live_session()
        turn = session.begin_turn("read the module")
        turn.started_at = 1000.0
        turn.iterations = 2
        turn.final_text = "It returns one."
        turn.finished_at = 1030.0
        store.save(snapshot_session(session))

        reopened = Session()
        assert reopened.restore_from(store.load(session.id) or {})

        assert len(reopened.turns) == 1
        assert reopened.turns[0].iterations == 2
        assert reopened.turns[0].duration_ms == 30000.0

    def test_a_stored_turn_that_ends_before_it_started_reports_no_duration(
        self, store: ConversationStore
    ) -> None:
        session = _live_session()
        turn = session.begin_turn("do it")
        turn.started_at = 2000.0
        turn.finished_at = 1000.0
        store.save(snapshot_session(session))

        reopened = Session()
        assert reopened.restore_from(store.load(session.id) or {})

        assert reopened.turns[0].duration_ms == 0.0

    def test_message_ids_and_timestamps_are_preserved(self, store: ConversationStore) -> None:
        session = _live_session()
        original = [(m.id, m.created_at) for m in session.messages]
        store.save(snapshot_session(session))

        reopened = Session()
        assert reopened.restore_from(store.load(session.id) or {})

        # Without these the next autosave overwrites real history with "this just
        # happened", which is how a transcript quietly starts lying about its age.
        assert [(m.id, m.created_at) for m in reopened.messages] == original

    def test_saving_the_same_conversation_repeatedly_adds_nothing(
        self, store: ConversationStore
    ) -> None:
        session = _live_session()
        store.save(snapshot_session(session))

        reopened = Session()
        assert reopened.restore_from(store.load(session.id) or {})
        store.save(snapshot_session(reopened))
        store.save(snapshot_session(reopened))
        store.save(snapshot_session(reopened))

        reloaded = Session()
        reloaded.restore_from(store.load(session.id) or {})
        assert len(reloaded.messages) == 2
        assert len(reloaded.tool_results) == 2
        assert len(reloaded.turns) == 0


class TestInterruptedTurns:
    def test_a_cancelled_turn_is_named(self) -> None:
        session = _live_session()
        turn = session.begin_turn("do it")
        turn.cancelled = True

        assert session.last_turn_outcome() == "cancelled"

    def test_a_failed_turn_is_named(self) -> None:
        session = _live_session()
        turn = session.begin_turn("do it")
        turn.error = "provider unavailable"

        assert session.last_turn_outcome() == "failed"

    def test_a_turn_still_running_when_the_app_died_is_called_interrupted(self) -> None:
        session = _live_session()
        session.begin_turn("do it")

        assert session.last_turn_outcome() == "interrupted"

    def test_a_conversation_with_no_turns_has_nothing_to_distrust(self) -> None:
        assert _live_session().last_turn_outcome() == "ok"

    def test_an_unusable_stored_result_is_dropped_not_shown_blank(
        self, store: ConversationStore
    ) -> None:
        payload = _record("conv-junk")
        payload["tool_results"].append({"no_tool_name_here": True})
        store.save(payload)

        reopened = Session()
        assert reopened.restore_from(store.load("conv-junk") or {})

        assert len(reopened.tool_results) == 2


class TestStorageLimits:
    def test_a_short_conversation_is_stored_whole(self) -> None:
        payload = snapshot_session(_live_session())

        assert len(payload["messages"]) == 2
        assert payload["truncated"]["dropped_messages"] == 0

    def test_the_oldest_messages_go_and_the_newest_stay(self) -> None:
        session = Session()
        for index in range(10):
            session.add_message(UserMessage(content=f"question {index}"))

        payload = snapshot_session(session, limits=StorageLimits(max_messages=4))

        kept = [item["content"] for item in payload["messages"]]
        assert kept == ["question 0", "question 7", "question 8", "question 9"]
        assert payload["truncated"]["dropped_messages"] == 6

    def test_the_opening_request_survives_the_trim(self) -> None:
        # It is what the conversation is named after, and what a reopened transcript
        # needs in order to say what it was for.
        session = Session()
        session.add_message(UserMessage(content="the original question"))
        for index in range(10):
            session.add_message(AssistantMessage(content=f"answer {index}"))

        payload = snapshot_session(session, limits=StorageLimits(max_messages=3))

        assert payload["messages"][0]["content"] == "the original question"
        assert payload["title"] == "the original question"

    def test_a_long_value_is_shortened_with_a_note_saying_so(self) -> None:
        session = Session()
        session.add_message(UserMessage(content="x" * 5000))

        payload = snapshot_session(session, limits=StorageLimits(max_chars_per_item=200))

        stored = payload["messages"][0]["content"]
        assert len(stored) < 5000
        assert "not stored" in stored

    def test_long_tool_output_is_shortened(self) -> None:
        session = Session()
        session.record_tool_result(ToolResult.success("run_command", "ok", {"stdout": "y" * 4000}))

        payload = snapshot_session(session, limits=StorageLimits(max_chars_per_item=300))

        assert "not stored" in payload["tool_results"][0]["data"]["stdout"]

    def test_old_tool_results_are_dropped(self) -> None:
        session = Session()
        for index in range(6):
            session.record_tool_result(
                ToolResult.success("read_file", f"read {index}", created_at=float(index))
            )

        payload = snapshot_session(session, limits=StorageLimits(max_tool_results=2))

        assert [item["summary"] for item in payload["tool_results"]] == ["read 4", "read 5"]
        assert payload["truncated"]["dropped_tool_results"] == 4

    def test_keeping_no_tool_results_is_allowed(self) -> None:
        payload = snapshot_session(_live_session(), limits=StorageLimits(max_tool_results=0))

        assert payload["tool_results"] == []

    def test_truncate_leaves_short_values_alone(self) -> None:
        assert truncate("short", 100) == "short"

    def test_a_default_record_states_its_limits(self) -> None:
        payload = _record()

        assert payload["truncated"]["limits"]["max_messages"] > 0
        assert payload["schema_version"] == SCHEMA_VERSION


class TestExport:
    def test_markdown_carries_the_transcript_in_order(self) -> None:
        text = render_markdown(_record("conv-md"))

        assert text.index("read the module") < text.index("read_file")
        assert text.index("read_file") < text.index("It returns one.")

    def test_markdown_shows_a_failure_as_a_failure(self) -> None:
        text = render_markdown(_record("conv-md"))

        assert "FAILED edit_file" in text
        assert "old_text not found" in text

    def test_markdown_carries_no_tool_payloads(self) -> None:
        payload = _record("conv-md")
        payload["tool_results"][0]["data"] = {"note": "sensitive blob"}

        assert "sensitive blob" not in render_markdown(payload)

    def test_markdown_says_so_when_a_turn_did_not_finish(self) -> None:
        text = render_markdown(_record("conv-md", finished=False))

        assert "interrupted" in text.lower()

    def test_markdown_admits_a_truncated_history(self) -> None:
        text = render_markdown(
            _record(
                "conv-md",
                truncated={
                    "dropped_messages": 12,
                    "dropped_tool_results": 3,
                    "limits": StorageLimits().to_dict(),
                },
            )
        )

        assert "partial" in text.lower()
        assert "12" in text

    def test_markdown_states_what_did_not_travel_with_the_record(self) -> None:
        text = render_markdown(_record("conv-md"))

        assert "Not restored" in text
        assert "undo_history" in text

    def test_json_is_the_whole_record_and_reloads(self) -> None:
        text = render_json(_record("conv-json"))

        assert json.loads(text)["id"] == "conv-json"

    def test_export_writes_markdown_with_the_right_suffix(self, store: ConversationStore) -> None:
        store.save(_record("conv-out"))

        written = store.export("conv-out", store.root / "dossier", fmt="md")

        assert written.name == "dossier.md"
        assert "read the module" in written.read_text(encoding="utf-8")

    def test_export_writes_json_with_the_right_suffix(self, store: ConversationStore) -> None:
        store.save(_record("conv-out"))

        written = store.export("conv-out", store.root / "dossier", fmt="json")

        assert written.name == "dossier.json"
        assert json.loads(written.read_text(encoding="utf-8"))["id"] == "conv-out"

    def test_exporting_an_unknown_conversation_raises(self, store: ConversationStore) -> None:
        with pytest.raises(FileNotFoundError):
            store.export("nope", store.root / "dossier")

    def test_an_unknown_export_format_is_refused(self, store: ConversationStore) -> None:
        store.save(_record("conv-out"))

        with pytest.raises(ValueError):
            store.export("conv-out", store.root / "dossier", fmt="pdf")

    def test_export_creates_the_destination_directory(self, store: ConversationStore) -> None:
        store.save(_record("conv-out"))
        target = store.root / "nested" / "deeper" / "dossier.md"

        written = store.export("conv-out", target, fmt="md")

        assert written.is_file()


class TestOlderRecords:
    def test_a_v2_record_without_tool_timestamps_is_backfilled(
        self, store: ConversationStore
    ) -> None:
        payload = _record("conv-old")
        payload["schema_version"] = 2
        for item in payload["tool_results"]:
            item.pop("created_at")

        store.save(payload)
        loaded = store.load("conv-old")

        assert loaded is not None
        assert all("created_at" in item for item in loaded["tool_results"])

    def test_a_v1_record_still_opens(self, store: ConversationStore) -> None:
        payload = _record("conv-v1")
        payload["schema_version"] = 1
        payload.pop("title")
        payload.pop("updated_at")
        payload.pop("finished")
        store.save(payload)

        loaded = store.load("conv-v1")

        assert loaded is not None
        assert loaded["title"] == "read the module"
        assert loaded["schema_version"] == SCHEMA_VERSION
