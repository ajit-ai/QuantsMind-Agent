"""Unit tests for durable conversation storage.

The store is the part that has to be unbreakable: it is the only thing standing
between a crash and a lost transcript. So the cases that matter are the hostile
ones -- a half-written file, a record from the future, a corrupt record, a
credential typed into a message -- not the happy path.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from quantsmind_agent.schema import AssistantMessage, UserMessage
from quantsmind_agent.services.conversations.session import Session
from quantsmind_agent.services.conversations.store import (
    SCHEMA_VERSION,
    ConversationStore,
    derive_title,
    scrub,
    snapshot_session,
)

# Assembled rather than written out: a literal credential-shaped value in this
# file would trip the repository secret scan, the same signal a real key gives.
_CREDENTIAL_SHAPED = "sk-" + "abcdefghijklmnop1234"


def _session(messages: list[str]) -> Session:
    session = Session()
    for text in messages:
        session.add_message(UserMessage(content=text))
        session.add_message(AssistantMessage(content=f"answer to {text}"))
    return session


def _record(conversation_id: str = "conv-1", **extra: Any) -> dict[str, Any]:
    payload = snapshot_session(
        _session(["hello there"]),
        provider={"name": "openai", "model": "gpt", "base_url": "https://api.test/v1"},
    )
    payload["id"] = conversation_id
    payload.update(extra)
    return payload


@pytest.fixture
def store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversations")


class TestRoundTrip:
    def test_a_record_survives_a_reload(self, store: ConversationStore) -> None:
        store.save(_record())

        loaded = store.load("conv-1")

        assert loaded is not None
        assert loaded["title"] == "hello there"
        assert loaded["schema_version"] == SCHEMA_VERSION
        assert loaded["provider"]["model"] == "gpt"

    def test_loading_an_unknown_conversation_returns_none(self, store: ConversationStore) -> None:
        assert store.load("nope") is None

    def test_saving_requires_an_id(self, store: ConversationStore) -> None:
        with pytest.raises(ValueError):
            store.save({"title": "no id"})

    def test_the_file_name_cannot_escape_the_root(self, store: ConversationStore) -> None:
        store.save(_record("../escape"))

        written = list(store.root.glob("*.json"))
        assert len(written) == 1
        assert written[0].parent == store.root


class TestListing:
    def test_listing_is_newest_first(self, store: ConversationStore) -> None:
        store.save(_record("old", updated_at=time.time() - 600))
        store.save(_record("new", updated_at=time.time()))

        assert [summary.id for summary in store.list()] == ["new", "old"]

    def test_a_summary_carries_what_a_list_needs(self, store: ConversationStore) -> None:
        store.save(_record())

        summary = store.latest()

        assert summary is not None
        assert summary.message_count == 2
        assert summary.finished is True
        assert summary.preview == "hello there"
        assert "hello there" in summary.label()

    def test_listing_an_empty_store_is_empty(self, store: ConversationStore) -> None:
        assert store.list() == []
        assert store.latest() is None

    def test_listing_a_missing_directory_is_empty(self, tmp_path: Path) -> None:
        assert ConversationStore(tmp_path / "absent").list() == []


class TestAtomicity:
    def test_a_save_leaves_no_temporary_file(self, store: ConversationStore) -> None:
        store.save(_record())

        assert [path.name for path in store.root.iterdir()] == ["conv-1.json"]

    def test_a_failed_save_keeps_the_previous_record(
        self, store: ConversationStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.save(_record())
        good = store.load("conv-1")

        def explode(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(os, "replace", explode)
        with pytest.raises(OSError):
            store.save(_record("conv-2"))
        monkeypatch.undo()

        assert store.load("conv-1") == good
        assert not list(store.root.glob("*.tmp"))


class TestCorruptionAndMigration:
    def test_a_corrupt_record_is_quarantined_not_deleted(self, store: ConversationStore) -> None:
        store.save(_record())
        (store.root / "conv-1.json").write_text("{not json", encoding="utf-8")

        assert store.load("conv-1") is None
        assert len(list(store.root.glob("*.corrupt"))) == 1, "the evidence survives for recovery"

    def test_one_corrupt_record_does_not_hide_the_others(self, store: ConversationStore) -> None:
        store.save(_record("good"))
        store.save(_record("bad"))
        (store.root / "bad.json").write_text("<<<", encoding="utf-8")

        assert [summary.id for summary in store.list()] == ["good"]

    def test_a_record_that_is_not_an_object_is_ignored(self, store: ConversationStore) -> None:
        store.save(_record())
        (store.root / "conv-1.json").write_text("[1, 2, 3]", encoding="utf-8")

        assert store.load("conv-1") is None

    def test_a_v1_record_is_migrated(self, store: ConversationStore) -> None:
        legacy = {
            "id": "old-format",
            "created_at": time.time(),
            "messages": [{"role": "user", "content": "the first question"}],
        }
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / "old-format.json").write_text(json.dumps(legacy), encoding="utf-8")

        loaded = store.load("old-format")

        assert loaded is not None
        assert loaded["schema_version"] == SCHEMA_VERSION
        assert loaded["title"] == "the first question"
        assert loaded["updated_at"] == pytest.approx(legacy["created_at"])
        assert loaded["finished"] is True

    def test_a_record_from_the_future_is_refused(self, store: ConversationStore) -> None:
        future = {"id": "from-the-future", "schema_version": SCHEMA_VERSION + 1, "messages": []}
        store.root.mkdir(parents=True, exist_ok=True)
        (store.root / "from-the-future.json").write_text(json.dumps(future), encoding="utf-8")

        assert store.load("from-the-future") is None


class TestLifecycle:
    def test_renaming_changes_the_title(self, store: ConversationStore) -> None:
        store.save(_record())

        assert store.rename("conv-1", "  My work  ") is True

        reloaded = store.load("conv-1")
        assert reloaded is not None
        assert reloaded["title"] == "My work"

    def test_renaming_an_unknown_conversation_reports_failure(
        self, store: ConversationStore
    ) -> None:
        assert store.rename("nope", "title") is False

    def test_deleting_removes_the_record(self, store: ConversationStore) -> None:
        store.save(_record())

        assert store.delete("conv-1") is True
        assert store.load("conv-1") is None

    def test_deleting_twice_reports_failure(self, store: ConversationStore) -> None:
        store.save(_record())
        store.delete("conv-1")

        assert store.delete("conv-1") is False

    def test_the_oldest_records_are_pruned_beyond_the_cap(self, tmp_path: Path) -> None:
        store = ConversationStore(tmp_path / "conversations", max_conversations=2)
        now = time.time()
        store.save(_record("oldest", updated_at=now - 300))
        store.save(_record("middle", updated_at=now - 200))
        store.save(_record("newest", updated_at=now - 100))

        assert {summary.id for summary in store.list()} == {"middle", "newest"}


class TestTitlesAndSecrets:
    def test_the_title_comes_from_the_first_question(self) -> None:
        assert derive_title([{"role": "user", "content": "fix the parser"}]) == "fix the parser"

    def test_a_long_question_is_truncated(self) -> None:
        title = derive_title([{"role": "user", "content": "x" * 200}])

        assert len(title) <= 60
        assert title.endswith("\u2026")

    def test_a_conversation_with_no_question_gets_a_default(self) -> None:
        assert derive_title([{"role": "assistant", "content": "hi"}]) == "Conversation"

    @pytest.mark.parametrize(
        "text",
        [
            f"my key is {_CREDENTIAL_SHAPED}",
            "token ghp_abcdefghijklmnop1234",
            "AIzaSyABCDEFGHIJKLMNOPQRSTUV",
            "api_key=supersecretvalue",
            "Authorization: Bearer abcdefghijklmnop",
        ],
    )
    def test_credential_shaped_text_is_masked(self, text: str) -> None:
        assert "[redacted]" in scrub(text)

    def test_ordinary_text_is_left_alone(self) -> None:
        text = "the parser needs a keyword argument for the separator"

        assert scrub(text) == text

    def test_a_credential_typed_into_a_message_never_reaches_disk(
        self, store: ConversationStore
    ) -> None:
        session = Session()
        session.add_message(UserMessage(content=f"use {_CREDENTIAL_SHAPED} please"))

        store.save(snapshot_session(session))

        raw = (store.root / f"{session.id}.json").read_text(encoding="utf-8")
        assert _CREDENTIAL_SHAPED not in raw
        assert "[redacted]" in raw

    def test_the_record_states_what_was_not_kept(self, store: ConversationStore) -> None:
        store.save(_record())

        payload = store.load("conv-1")

        assert payload is not None
        assert set(payload["not_restored"]) == {
            "undo_history",
            "approval_grants",
            "pending_tool_calls",
        }
