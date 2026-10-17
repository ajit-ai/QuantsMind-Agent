"""Durable storage for tasks, with the same guarantees as conversation storage.

One JSON file per task under a ``tasks`` directory. Written atomically through a
sibling temporary file and :func:`os.replace`, so a crash mid-write leaves the
previous record intact rather than a half-written one. Corrupt records are
quarantined, never deleted: a user who loses a two-hour task to a disk error can
still recover the file by hand, and neither corruption nor a record from a newer
schema is allowed to stop the application starting.

Why this is a separate store rather than fields on a conversation
-----------------------------------------------------------------
A conversation is what was *said*. A task is what was *attempted*: which plan was
approved, what ran, what changed on disk, which checks passed. Those have
different lifetimes and different blast radii. Folding one into the other would
mean either deleting a task's evidence when the user tidies their history, or
resuming a task's approvals because someone reopened a chat -- which is exactly
the failure mode recovery has to avoid.

What is deliberately *not* persisted
------------------------------------
* **Reusable authorisation.** An approval is stored as a fact about one plan
  digest in one workspace, and is re-validated on every load. Nothing in a task
  record can be replayed as a permission.
* **File contents.** Evidence is path, operation, existence and hashes. A task
  record that embedded pre-images would duplicate the workspace and quietly rot
  as soon as the user edited anything.
* **Anything credential-shaped.** Task text passes through the same redaction the
  conversation store uses before anything is written.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from quantsmind_agent.conversations import _scrub_deep
from quantsmind_agent.tasks import SCHEMA_VERSION, PlanRejected, Task, TaskStatus

__all__ = ["DEFAULT_MAX_TASKS", "TaskStore", "TaskSummary"]

logger = logging.getLogger(__name__)

#: How many task records to keep. Tasks are heavier than conversations -- they
#: carry execution evidence -- so the ceiling is lower.
DEFAULT_MAX_TASKS = 25

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe_name(task_id: str) -> str:
    """A file name derived from an id, with nothing path-like surviving."""

    cleaned = _SAFE_ID.sub("-", str(task_id or "")).strip("-") or "task"
    return cleaned[:96]


#: Aliases declared outside the class body because ``TaskStore.list`` shadows the
#: builtin inside it, and an annotation written in the class body would then
#: refer to the method rather than to ``list``.
SummaryList = list["TaskSummary"]
PathList = list[Path]


class TaskSummary:
    """The list-level view of a task: enough to show a row, nothing more."""

    __slots__ = (
        "id",
        "objective",
        "status",
        "steps_done",
        "steps_total",
        "updated_at",
        "workspace",
    )

    def __init__(self, payload: dict[str, Any]) -> None:
        plan = payload.get("plan") or {}
        steps = plan.get("steps") or []
        statuses = payload.get("step_status") or {}
        done = sum(
            1
            for step in steps
            if str(statuses.get(str(step.get("id", "")), "")) in {"succeeded", "skipped"}
        )
        self.id = str(payload.get("id", ""))
        self.objective = str(payload.get("objective", ""))
        status = str(payload.get("status", TaskStatus.DRAFT))
        self.status = status if status in {item.value for item in TaskStatus} else TaskStatus.DRAFT
        self.updated_at = float(payload.get("updated_at", 0.0) or 0.0)
        self.workspace = str(payload.get("workspace", ""))
        self.steps_done = done
        self.steps_total = len(steps)

    @property
    def label(self) -> str:
        text = self.objective.strip() or self.id
        return text if len(text) <= 60 else text[:57] + "..."


class TaskStore:
    """One JSON file per task, under ``root``."""

    def __init__(self, root: Path, *, max_tasks: int = DEFAULT_MAX_TASKS) -> None:
        self._root = Path(root)
        self._max = max(1, int(max_tasks))

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, task_id: str) -> Path:
        return self._root / f"{_safe_name(task_id)}.json"

    def save(self, task: Task) -> Path:
        """Persist a task atomically, redacting anything credential-shaped."""

        self._root.mkdir(parents=True, exist_ok=True)
        payload = _json_safe(task.to_dict())
        # Task text can quote a model, a tool output or a file. Redact before it
        # reaches disk, not on the way out, so a credential is never written in
        # the first place.
        payload = _scrub_deep(payload)
        payload["saved_at"] = time.time()
        target = self.path_for(task.id)
        temporary = target.with_suffix(f".json.tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        os.replace(temporary, target)
        return target

    def load(self, task_id: str) -> Task | None:
        """Read a task, or ``None`` when it is missing, corrupt or unreadable."""

        return self._read(task_id)

    def exists(self, task_id: str) -> bool:
        return self.path_for(task_id).is_file()

    def delete(self, task_id: str) -> bool:
        """Delete a task record. Quarantined files are left alone."""

        target = self.path_for(task_id)
        if target.is_file():
            target.unlink()
            return True
        return False

    def list(self) -> SummaryList:
        """Summaries of every readable task, newest first."""

        summaries: list[TaskSummary] = []
        if not self._root.is_dir():
            return summaries
        for path in sorted(self._root.glob("*.json")):
            payload = _read_json(path)
            if isinstance(payload, dict) and payload.get("id"):
                try:
                    summaries.append(TaskSummary(payload))
                except (TypeError, ValueError):  # pragma: no cover - defensive
                    continue
        summaries.sort(key=lambda item: item.updated_at, reverse=True)
        return summaries

    def prune(self) -> int:
        """Delete the oldest finished tasks beyond the ceiling.

        Only terminal tasks are eligible. An unfinished task is somebody's
        in-progress work, and pruning it because it is old would be destroying
        something rather than tidying up.
        """

        summaries = self.list()
        finished = [
            item
            for item in summaries
            if item.status in (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value)
        ]
        excess = len(finished) - self._max
        removed = 0
        for summary in finished[max(0, excess) :]:
            if self.delete(summary.id):
                removed += 1
        return removed

    def verify_integrity(self) -> PathList:
        """Quarantine every record that cannot be read, and return those paths.

        Separate from :meth:`list` on purpose: listing is a read, and a read that
        quietly moves files around is a surprise. The application calls this
        once when tasks are opened, which is the moment a user would want to be
        told that a record was set aside.
        """

        moved: PathList = []
        if not self._root.is_dir():
            return moved
        for path in sorted(self._root.glob("*.json")):
            existed = path.is_file()
            if self._read(path.stem) is None and existed:
                # _read has already moved the file aside; the original path no
                # longer exists, which is why existence is captured first.
                target = path.with_suffix(".json.corrupt")
                if target.exists():
                    moved.append(target)
        return moved

    def quarantined(self) -> PathList:
        """Files that could not be parsed, kept for the user to recover."""

        if not self._root.is_dir():
            return []
        return sorted(self._root.glob("*.json.corrupt"))

    def _read(self, task_id: str) -> Task | None:
        path = self.path_for(task_id)
        if not path.is_file():
            return None
        payload = _read_json(path)
        if payload is None:
            quarantine(path, "unreadable JSON")
            return None
        if not isinstance(payload, dict):
            quarantine(path, "not a JSON object")
            return None
        try:
            return Task.from_dict(payload)
        except PlanRejected as exc:
            quarantine(path, str(exc))
            return None
        except (TypeError, ValueError, KeyError) as exc:
            quarantine(path, f"unusable task record: {exc}")
            return None


def quarantine(path: Path, reason: str) -> Path:
    """Move an unreadable record aside instead of deleting it."""

    target = path.with_suffix(".json.corrupt")
    counter = 1
    while target.exists():
        target = path.with_suffix(f".json.corrupt.{counter}")
        counter += 1
    try:
        os.replace(path, target)
    except OSError:  # pragma: no cover - the record stays where it is
        logger.warning("could not quarantine %s: %s", path.name, reason)
        return path
    logger.warning("quarantined unreadable task record %s: %s", path.name, reason)
    return target


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    """Coerce a payload into something :func:`json.dumps` accepts.

    A record that cannot be written is not a record that can be recovered, so
    anything unserialisable becomes a readable placeholder instead of raising at
    the worst possible moment.
    """

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


# Re-exported so callers can reason about what they are reading.
__all__ += ["SCHEMA_VERSION"]
