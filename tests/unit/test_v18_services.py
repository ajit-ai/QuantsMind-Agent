"""v1.8 tests: collab mirror, review comments, unified loop."""

from __future__ import annotations

from pathlib import Path

from quantsmind_agent.services.collab_mirror import export_mirror, import_mirror
from quantsmind_agent.services.review_comments import CommentStore, ReviewComment
from quantsmind_agent.services.unified_loop import UnifiedLoop


def test_mirror_roundtrip(tmp_path: Path) -> None:
    dest = export_mirror(
        tmp_path / "mirror", profiles=[{"profile_id": "p1"}], memories=[{"title": "t"}]
    )
    assert (dest / "profiles.json").exists()
    data = import_mirror(dest)
    assert data["profiles"] == [{"profile_id": "p1"}]
    assert data["memories"] == [{"title": "t"}]


def test_comments_per_path(tmp_path: Path) -> None:
    store = CommentStore(tmp_path)
    store.add(ReviewComment(task_id="t1", path="a.py", author="Ada", text="looks good"))
    store.add(ReviewComment(task_id="t1", path="b.py", author="Bo", text="typo"))
    assert len(store.list("t1")) == 2
    assert len(store.by_path("t1", "a.py")) == 1
    assert store.list("nope") == []


def test_unified_loop_delegates() -> None:
    class _Agent:
        def run_turn(self, text: str) -> str:
            return f"echo:{text}"

    assert UnifiedLoop(_Agent()).run_turn("hi") == "echo:hi"
