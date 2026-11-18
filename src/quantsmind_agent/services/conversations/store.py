"""Durable conversations: create, list, reopen, rename, delete.

v0.2 kept a conversation in memory and threw it away with the process. That was
a deliberate v0.1 decision (see ADR 0005) and it stopped being the right one as
soon as the agent could change files: losing the transcript meant losing the
record of *what was changed*, which is the one thing a user needs when they come
back to check.

Storage is one JSON file per conversation under the application data directory.
That choice is deliberate rather than default: no new dependency, nothing to
migrate on upgrade, no database file to corrupt, and it matches how the rest of
the application already writes files -- a sibling temporary file plus
:func:`os.replace`, so a crash mid-write leaves the previous record intact
rather than a half-written one.

What is deliberately *not* persisted
------------------------------------
* **Undo history.** The journal holds raw file bytes captured in memory, and a
  restored pre-image would outlive the session that produced it: the files may
  have changed since, and the user has re-approved nothing. Undo stays
  session-scoped and a resumed conversation starts with an empty one.
* **Approval session grants.** "Allow for Session" meant *this* session.
  Restoring it would silently re-grant a permission the user never gave again.
* **Pending tool calls and approvals.** A turn interrupted by a crash is recorded
  as unfinished and never replayed.
* **Credentials.** API keys live in the OS credential store. Provider records
  keep endpoint and model name -- neither is secret -- and message text is
  scrubbed of credential-shaped strings before it is written.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quantsmind_agent.diagnostics.logging import get_logger
from quantsmind_agent.schema import MessageRole
from quantsmind_agent.services.conversations.session import Session

__all__ = [
    "SCHEMA_VERSION",
    "ConversationStore",
    "ConversationSummary",
    "StorageLimits",
    "derive_title",
    "render_json",
    "render_markdown",
    "scrub",
    "snapshot_session",
]

logger = get_logger(__name__)

#: Bumped when the on-disk shape changes; see :func:`ConversationStore._migrate`.
SCHEMA_VERSION = 3

#: Default cap on stored conversations, oldest pruned first.
DEFAULT_MAX_CONVERSATIONS = 50

#: Default cap on messages kept per conversation.
DEFAULT_MAX_MESSAGES = 400

#: Default cap on stored tool results per conversation.
DEFAULT_MAX_TOOL_RESULTS = 400

#: Default cap on the length of any single stored text value.
DEFAULT_MAX_ITEM_CHARS = 8000

_TITLE_LIMIT = 60

_TRUNCATION_NOTE = "\n\n[... {dropped} more characters not stored ...]"

#: Strings that look like credentials are masked before anything is written.
#: Deliberately narrow: a scrubber that eats ordinary text makes the transcript
#: untrustworthy in a different way.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|authorization)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
)

_REDACTED = "[redacted]"


def scrub(text: str) -> str:
    """Mask credential-shaped substrings in text bound for disk."""

    cleaned = text
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub(_REDACTED, cleaned)
    return cleaned


def _scrub_deep(value: Any) -> Any:
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {key: _scrub_deep(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_deep(item) for item in value]
    return value


def truncate(text: str, limit: int) -> str:
    """Shorten one stored text value, saying that it was shortened."""

    if limit <= 0 or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return text[:limit].rstrip() + _TRUNCATION_NOTE.format(dropped=dropped)


def _truncate_deep(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return truncate(value, limit)
    if isinstance(value, dict):
        return {key: _truncate_deep(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate_deep(item, limit) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class StorageLimits:
    """How much of a conversation is kept on disk.

    Every value is a bound, not a target: a conversation that stays inside them is
    stored whole, and one that outgrows them loses its oldest history and its
    longest values rather than growing without limit. What was dropped is recorded
    in the record itself, so a reopened conversation can admit it rather than
    quietly pretending to be complete.
    """

    max_messages: int = DEFAULT_MAX_MESSAGES
    max_tool_results: int = DEFAULT_MAX_TOOL_RESULTS
    max_chars_per_item: int = DEFAULT_MAX_ITEM_CHARS

    def to_dict(self) -> dict[str, int]:
        return {
            "max_messages": self.max_messages,
            "max_tool_results": self.max_tool_results,
            "max_chars_per_item": self.max_chars_per_item,
        }


def _trim_messages(messages: list[Any], limit: int) -> tuple[list[Any], int]:
    """Keep the newest messages, but never the opening request.

    A negative ``limit`` means no limit. ``0`` is not a sensible message count --
    a conversation with no opening request cannot be titled or resumed -- so it is
    raised to the first request plus nothing.

    The first user message is what a conversation is named after and what tells a
    reopened transcript what it was for, so it survives the trim even when the rest
    of the oldest history does not.
    """

    if limit < 0 or len(messages) <= limit:
        return messages, 0
    if limit == 0:
        return [], len(messages)
    dropped = len(messages) - limit
    for index, item in enumerate(messages):
        if isinstance(item, dict) and item.get("role") == MessageRole.USER.value:
            head = [item]
            tail = messages[index + 1 :]
            return head + tail[-(limit - 1) :], dropped
    return messages[-limit:], dropped


def _trim_items(items: list[Any], limit: int) -> tuple[list[Any], int]:
    """Keep the newest items. ``0`` keeps none, which is a supported setting."""

    if limit < 0 or len(items) <= limit:
        return items, 0
    if limit == 0:
        return [], len(items)
    return items[-limit:], len(items) - limit


def derive_title(messages: list[dict[str, Any]], fallback: str = "Conversation") -> str:
    """Name a conversation after what the user first asked for."""

    for message in messages:
        if message.get("role") != MessageRole.USER.value:
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        first = content.splitlines()[0].strip()
        if len(first) > _TITLE_LIMIT:
            return first[: _TITLE_LIMIT - 1].rstrip() + "\u2026"
        return first or fallback
    return fallback


@dataclass(frozen=True, slots=True)
class ConversationSummary:
    """What the conversation list shows, without loading a whole transcript."""

    id: str
    title: str
    created_at: float
    updated_at: float
    message_count: int
    turn_count: int
    workspace: str
    model: str
    preview: str
    finished: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "turn_count": self.turn_count,
            "workspace": self.workspace,
            "model": self.model,
            "preview": self.preview,
            "finished": self.finished,
        }

    def label(self) -> str:
        """One line for a combo box: what it is, and when it was last touched."""

        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.updated_at))
        return f"{self.title}  \u00b7  {when}"


def snapshot_session(
    session: Session,
    *,
    title: str = "",
    provider: dict[str, Any] | None = None,
    finished: bool = True,
    limits: StorageLimits | None = None,
) -> dict[str, Any]:
    """Build the durable record for ``session``.

    ``finished`` marks whether the last turn ended cleanly. A conversation
    interrupted mid-turn is stored as unfinished so a resume can say so instead of
    pretending the tool call ran.

    ``limits`` bounds the result. The visible transcript survives a long
    conversation by losing its oldest turns rather than its newest, and any single
    over-long value is shortened with a note in place of the missing text.
    """

    bounds = limits or StorageLimits()
    payload = session.to_dict()
    messages = _truncate_deep(_scrub_deep(payload.get("messages", [])), bounds.max_chars_per_item)
    turns = _truncate_deep(_scrub_deep(payload.get("turns", [])), bounds.max_chars_per_item)
    tool_results = _truncate_deep(
        _scrub_deep(payload.get("tool_results", [])), bounds.max_chars_per_item
    )
    messages, dropped_messages = _trim_messages(messages, bounds.max_messages)
    tool_results, dropped_results = _trim_items(tool_results, bounds.max_tool_results)
    now = time.time()
    return {
        "schema_version": SCHEMA_VERSION,
        "id": session.id,
        "title": title or derive_title(messages),
        "created_at": payload.get("created_at", now),
        "updated_at": now,
        "finished": finished,
        "workspace": payload.get("workspace"),
        "provider": {
            "name": str((provider or {}).get("name") or ""),
            "model": str((provider or {}).get("model") or ""),
            "base_url": str((provider or {}).get("base_url") or ""),
        },
        "messages": messages,
        "turns": turns,
        "tool_results": tool_results,
        # What the limits cost, recorded rather than implied: a transcript that has
        # lost its oldest turns says so instead of looking like the whole thing.
        "truncated": {
            "dropped_messages": dropped_messages,
            "dropped_tool_results": dropped_results,
            "limits": bounds.to_dict(),
        },
        # Stated rather than implied, so a reader of the record knows exactly what
        # did not travel with it.
        "not_restored": {
            "undo_history": "session-scoped; a resumed conversation starts with none",
            "approval_grants": "never persisted; the user re-approves after a restart",
            "pending_tool_calls": "an interrupted turn is recorded as unfinished, never replayed",
        },
    }


class ConversationStore:
    """One JSON file per conversation, under ``root``.

    Reading is defensive by design: a corrupt record is quarantined rather than
    deleted, so a user can still recover it by hand, and a record from a newer
    version is skipped rather than misread. Neither is allowed to stop the
    application from starting.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_conversations: int = DEFAULT_MAX_CONVERSATIONS,
    ) -> None:
        self._root = Path(root)
        self._max = max(1, int(max_conversations))

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, conversation_id: str) -> Path:
        return self._root / f"{_safe_name(conversation_id)}.json"

    # -- writing -----------------------------------------------------------
    def save(self, payload: dict[str, Any]) -> Path:
        """Write a record atomically and prune the oldest beyond the cap."""

        conversation_id = str(payload.get("id") or "")
        if not conversation_id:
            raise ValueError("a conversation record needs an id")
        self._root.mkdir(parents=True, exist_ok=True)
        target = self.path_for(conversation_id)
        temporary = target.with_name(f".{target.name}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, indent=2, default=str, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(temporary, target)
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("Could not save conversation %s: %s", conversation_id, exc)
            temporary.unlink(missing_ok=True)
            raise
        self.prune()
        return target

    def rename(self, conversation_id: str, title: str) -> bool:
        """Give a conversation a new title. Returns ``False`` if it is unknown."""

        payload = self._read(conversation_id)
        if payload is None:
            return False
        payload["title"] = title.strip() or payload.get("title") or "Conversation"
        payload["updated_at"] = time.time()
        self.save(payload)
        return True

    def delete(self, conversation_id: str) -> bool:
        """Remove a record. Returns ``False`` if there was nothing to remove.

        Deletion is immediate and total: the file is gone, so the conversation is
        no longer listed, no longer restorable and no longer resumable. There is no
        soft-delete state to fall back on -- a user who asked for it to be deleted
        should not be able to find it again by reopening the application.
        """

        target = self.path_for(conversation_id)
        if not target.is_file():
            return False
        try:
            target.unlink()
        except OSError as exc:  # pragma: no cover - disk failure
            logger.warning("Could not delete conversation %s: %s", conversation_id, exc)
            return False
        return True

    def prune(self) -> int:
        """Delete the oldest records beyond the cap. Returns how many went."""

        summaries = self.list()
        excess = len(summaries) - self._max
        if excess <= 0:
            return 0
        removed = 0
        for summary in summaries[self._max :]:
            if self.delete(summary.id):
                removed += 1
        return removed

    # -- reading -----------------------------------------------------------
    def load(self, conversation_id: str) -> dict[str, Any] | None:
        """Read one record, or ``None`` when it is missing or unusable."""

        return self._read(conversation_id)

    def list(self) -> list[ConversationSummary]:
        """Every readable record, newest first.

        One unreadable file does not hide the others: it is quarantined and the
        list carries on.
        """

        summaries: list[ConversationSummary] = []
        if not self._root.is_dir():
            return summaries
        for path in sorted(self._root.glob("*.json")):
            payload = self._read(path.stem)
            if payload is None:
                continue
            summaries.append(_summarise(payload))
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return summaries

    def latest(self) -> ConversationSummary | None:
        """The most recently touched conversation, if any."""

        summaries = self.list()
        return summaries[0] if summaries else None

    def export(self, conversation_id: str, target: Path, *, fmt: str = "md") -> Path:
        """Write a conversation out as Markdown or JSON.

        Export is how a conversation outlives the application: the record format
        carries internals a person does not want to read, so Markdown is for
        reading and sharing and JSON is for re-importing elsewhere. Either way the
        content is exactly what was stored, with the same redaction applied.
        """

        payload = self.load(conversation_id)
        if payload is None:
            raise FileNotFoundError(f"no stored conversation with id {conversation_id!r}")
        if fmt == "json":
            text = render_json(payload)
            target = target.with_suffix(".json")
        elif fmt in ("md", "markdown"):
            text = render_markdown(payload)
            target = target.with_suffix(".md")
        else:
            raise ValueError(f"unsupported export format: {fmt!r} (use 'md' or 'json')")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        logger.info("Exported conversation %s to %s", conversation_id, target)
        return target

    def _read(self, conversation_id: str) -> dict[str, Any] | None:
        path = self.path_for(conversation_id)
        if not path.is_file():
            return None
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:  # pragma: no cover - unreadable file
            logger.warning("Could not read conversation %s: %s", conversation_id, exc)
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            # Keep the evidence: a quarantined record can still be recovered by
            # hand, whereas a deleted one is gone for good.
            quarantine = path.with_suffix(".json.corrupt")
            try:
                os.replace(path, quarantine)
            except OSError:  # pragma: no cover - disk failure
                quarantine = path
            logger.warning(
                "Conversation %s is corrupt (%s); moved to %s",
                conversation_id,
                exc,
                quarantine.name,
            )
            return None
        if not isinstance(payload, dict):
            logger.warning("Conversation %s is not a record; ignoring it", conversation_id)
            return None
        return self._migrate(payload)

    @staticmethod
    def _migrate(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Upgrade an older record, or refuse one from a newer version.

        A record written by a future version is refused rather than guessed at:
        its fields may mean something else, and silently misreading them would be
        worse than losing the conversation.
        """

        version = payload.get("schema_version")
        if not isinstance(version, int):
            version = 1
        if version > SCHEMA_VERSION:
            logger.warning("Skipping a conversation written by a newer version (%s)", version)
            return None
        if version < 2:
            # v1 had no title, no updated_at and no finished flag.
            messages = payload.get("messages", [])
            payload.setdefault("title", derive_title(messages))
            payload.setdefault("updated_at", payload.get("created_at") or time.time())
            payload.setdefault("finished", True)
            payload["schema_version"] = 2
        if version < 3:
            # v2 stored tool results with no timestamp, which makes it impossible
            # to say where a tool call sat among the assistant messages. Records
            # written before tool results carried a time are back-filled from the
            # conversation's own creation point, so they stay readable and in order
            # rather than being rejected; the back-fill is marked so the difference
            # is not hidden.
            back_filled = False
            for item in payload.get("tool_results", []) or []:
                if isinstance(item, dict) and "created_at" not in item:
                    item["created_at"] = float(payload.get("created_at") or time.time())
                    back_filled = True
            payload.setdefault(
                "truncated",
                {
                    "dropped_messages": 0,
                    "dropped_tool_results": 0,
                    "limits": StorageLimits().to_dict(),
                },
            )
            if back_filled:
                logger.info("Back-filled missing tool timestamps in an older record")
            payload["schema_version"] = SCHEMA_VERSION
        return payload


def _stamp(epoch: Any) -> str:
    if not isinstance(epoch, (int, float)) or epoch <= 0:
        return "unknown time"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


def _tool_line(result: dict[str, Any]) -> str:
    """One readable line per tool call, with no payloads."""

    tool = str(result.get("tool") or "tool")
    ok = bool(result.get("ok", False))
    text = str(result.get("summary") or "")
    if not ok:
        text = str(result.get("error") or text or "failed")
    duration = result.get("duration_ms")
    suffix = (
        f" ({float(duration):.0f} ms)" if isinstance(duration, (int, float)) and duration else ""
    )
    return f"{'OK' if ok else 'FAILED'} {tool}: {text}{suffix}"


def _ordered_lines(payload: dict[str, Any]) -> list[tuple[str, str, float]]:
    """The conversation as ``(kind, text, when)`` in the order it happened.

    The same merge the reopened transcript uses: messages and tool results sorted
    by when they were produced, messages first when the clock cannot separate them.
    """

    entries: list[tuple[float, int, int, str, str]] = []
    for index, message in enumerate(payload.get("messages", []) or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "")
        content = str(message.get("content") or "")
        when = message.get("created_at")
        when = float(when) if isinstance(when, (int, float)) else 0.0
        if role == MessageRole.USER.value:
            entries.append((when, 0, index, "user", content))
        elif role == MessageRole.ASSISTANT.value and content.strip():
            entries.append((when, 0, index, "assistant", content))
    for index, result in enumerate(payload.get("tool_results", []) or []):
        if not isinstance(result, dict):
            continue
        when = result.get("created_at")
        when = float(when) if isinstance(when, (int, float)) else 0.0
        entries.append((when, 1, index, "tool", _tool_line(result)))
    entries.sort(key=lambda item: (item[0], item[1], item[2]))
    return [(kind, text, stamp) for stamp, _, _, kind, text in entries]


def render_markdown(payload: dict[str, Any]) -> str:
    """A conversation as a readable Markdown document.

    This is the format to read, diff and paste into an issue. It carries the
    transcript and the tool outcomes, states plainly that the last turn was
    interrupted when it was, and does not carry tool payloads or internal metadata.
    """

    provider = payload.get("provider", {})
    provider = provider if isinstance(provider, dict) else {}
    title = str(payload.get("title") or "Conversation")
    finished = bool(payload.get("finished", True))
    lines = [f"# {title}", ""]
    lines.append(f"- **Conversation**: `{payload.get('id', '')}`")
    lines.append(f"- **Started**: {_stamp(payload.get('created_at'))}")
    lines.append(f"- **Last activity**: {_stamp(payload.get('updated_at'))}")
    if payload.get("workspace"):
        lines.append(f"- **Project**: `{payload['workspace']}`")
    if provider.get("model"):
        provider_name = provider.get("name") or "provider"
        lines.append(f"- **Model**: {provider_name} / {provider['model']}")
    lines.append("")
    if not finished:
        lines += [
            "> The last turn in this conversation was **interrupted** and did not "
            "finish. Nothing from it was replayed, and no answer below should be "
            "read as its result.",
            "",
        ]
    truncated = payload.get("truncated", {})
    if isinstance(truncated, dict) and (
        truncated.get("dropped_messages") or truncated.get("dropped_tool_results")
    ):
        lines += [
            f"> This record was stored under size limits and is **partial**: "
            f"{truncated.get('dropped_messages', 0)} older messages and "
            f"{truncated.get('dropped_tool_results', 0)} older tool results were "
            "dropped, and long values were shortened in place.",
            "",
        ]
    lines += ["## Transcript", ""]
    for kind, text, when in _ordered_lines(payload):
        label = {"user": "You", "assistant": "Assistant", "tool": "Tool"}.get(kind, kind)
        lines += [f"### {label} · {_stamp(when)}", "", text.strip() or "_(no text)_", ""]
    not_restored = payload.get("not_restored")
    if isinstance(not_restored, dict):
        lines += ["## Not restored", ""]
        lines += [f"- **{key}**: {value}" for key, value in not_restored.items()]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_json(payload: dict[str, Any]) -> str:
    """A conversation as JSON: the whole record, for tooling and re-import."""

    return json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n"


def _safe_name(conversation_id: str) -> str:
    """A file-safe stem for an id, so a stored path can never escape the root."""

    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", str(conversation_id))[:64]
    return cleaned or "conversation"


def _summarise(payload: dict[str, Any]) -> ConversationSummary:
    messages = payload.get("messages", [])
    provider = payload.get("provider", {})
    return ConversationSummary(
        id=str(payload.get("id") or ""),
        title=str(payload.get("title") or "Conversation"),
        created_at=float(payload.get("created_at") or 0.0),
        updated_at=float(payload.get("updated_at") or 0.0),
        message_count=len(messages) if isinstance(messages, list) else 0,
        turn_count=len(payload.get("turns", [])) if isinstance(payload.get("turns"), list) else 0,
        workspace=str(payload.get("workspace") or ""),
        model=str(provider.get("model") or "") if isinstance(provider, dict) else "",
        preview=_preview(messages),
        finished=bool(payload.get("finished", True)),
    )


def _preview(messages: Any) -> str:
    """The opening line of the conversation, for a list without loading it all."""

    if not isinstance(messages, list):
        return ""
    for message in messages:
        if isinstance(message, dict) and message.get("role") == MessageRole.USER.value:
            content = str(message.get("content") or "").strip()
            if content:
                return content.splitlines()[0].strip()[:_TITLE_LIMIT]
    return ""
