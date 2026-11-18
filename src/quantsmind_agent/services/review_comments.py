"""ChangeSet review comments (WP v1.8).

Local, per-task, per-path comments stored beside the workspace. No central
server; mirrors via Git like profiles/memory.
"""

from __future__ import annotations

import builtins
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

__all__ = ["CommentStore", "ReviewComment"]


@dataclass
class ReviewComment:
    task_id: str
    path: str
    author: str
    text: str
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("comment text required")
        if not self.created_at:
            self.created_at = time.time()


class CommentStore:
    def __init__(self, root: str | Path) -> None:
        self._path = Path(root) / ".quantsmind" / "review_comments.json"

    def list(self, task_id: str | None = None) -> list[ReviewComment]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        out = []
        for item in data if isinstance(data, list) else []:
            if not isinstance(item, dict):
                continue
            if task_id and str(item.get("task_id", "")) != task_id:
                continue
            # A record missing required fields is ignored rather than crashing
            # the whole listing. Corrupt or partially-written local data must
            # never make the review view unusable.
            required = ("task_id", "path", "author", "text")
            if any(k not in item for k in required):
                continue
            try:
                out.append(
                    ReviewComment(
                        **{
                            k: item[k]
                            for k in ("task_id", "path", "author", "text", "created_at")
                            if k in item
                        }
                    )
                )
            except (ValueError, TypeError):
                continue
        return out

    def add(self, comment: ReviewComment) -> ReviewComment:
        comments = [*self.list(), comment]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps([asdict(c) for c in comments], indent=2), encoding="utf-8")
        return comment

    def by_path(self, task_id: str, path: str) -> builtins.list[ReviewComment]:
        return [c for c in self.list(task_id) if c.path == path]
