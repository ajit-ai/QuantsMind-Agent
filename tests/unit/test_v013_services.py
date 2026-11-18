"""WP1/WP2/WP3/WP4/WP5 unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantsmind_agent.errors import InvalidPathError, InvalidWorkspaceError
from quantsmind_agent.services.context_intelligence import rank_context
from quantsmind_agent.services.memory import MemoryEntry, MemoryStore
from quantsmind_agent.services.profiles import EngineeringProfile, ProfileStore
from quantsmind_agent.services.tasks.dependencies import (
    TaskStateStore,
    dependency_order,
    propagate_failure,
    resume_candidates,
)
from quantsmind_agent.services.tasks.models import Plan, PlannedStep
from quantsmind_agent.services.workspace.multi_root import MultiRootWorkspace


def _plan(order=("a", "b", "c"), deps=None):
    steps = []
    for sid in order:
        steps.append(
            PlannedStep(
                id=sid, description=sid, tool="x", depends_on=tuple((deps or {}).get(sid, ()))
            )
        )
    return Plan(objective="objective", steps=steps)


class TestMultiRoot:
    def test_register_remove(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        m = MultiRootWorkspace(tmp_path / "a")
        m.add_root("b", tmp_path / "b")
        assert m.root_ids() == ("b", "primary")
        root_id, p = m.resolve("b:file.txt")
        assert root_id == "b" and p.name == "file.txt"
        assert m.remove_root("b")
        assert m.root_ids() == ("primary",)

    def test_duplicate_missing(self, tmp_path: Path) -> None:
        m = MultiRootWorkspace(tmp_path)
        with pytest.raises(InvalidWorkspaceError):
            m.add_root("primary", tmp_path)
        with pytest.raises(InvalidWorkspaceError):
            m.workspace("nope")

    def test_traversal_refused(self, tmp_path: Path) -> None:
        m = MultiRootWorkspace(tmp_path)
        with pytest.raises(InvalidPathError):
            m.resolve("../escape.txt")
        with pytest.raises(InvalidPathError):
            m.resolve("nope:x.txt")

    def test_primary_promotion(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir(exist_ok=True)
        (tmp_path / "b").mkdir(exist_ok=True)
        m2 = MultiRootWorkspace(tmp_path / "a")
        m2.add_root("b", tmp_path / "b")
        m2.remove_root("primary")
        assert m2.primary_id() == "b"


class TestProfiles:
    def test_crud_and_validation(self, tmp_path: Path) -> None:
        store = ProfileStore(tmp_path)
        assert store.list() == []
        p = EngineeringProfile(
            profile_id="p1", title="T", build_commands=["make"], test_commands=["pytest"]
        )
        store.save(p)
        assert store.get("p1") is not None
        assert store.describe_commands(p) == ["make", "pytest"]
        assert store.delete("p1")
        from quantsmind_agent.services.profiles import ProfileValidationError

        with pytest.raises(ProfileValidationError):
            store.save(EngineeringProfile(profile_id="", build_commands=["x"] * 30))

    def test_isolation(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        ProfileStore(tmp_path / "a").save(EngineeringProfile(profile_id="pa", title="A"))
        assert ProfileStore(tmp_path / "b").list() == []


class TestContext:
    def test_ranking_budget_dedup_provenance(self, tmp_path: Path) -> None:
        (tmp_path / "auth.py").write_text("def login():\n    pass\n")
        (tmp_path / "other.py").write_text("x = 1\n" * 10)
        (tmp_path / "dup.py").write_text("x = 1\n" * 10)  # content dup of other
        (tmp_path / "api_key.json").write_text("{}")
        pack = rank_context({"primary": tmp_path}, "login", max_files=5, token_budget=1000)
        assert pack.items
        assert pack.items[0].path == "auth.py"
        assert pack.items[0].reason
        assert all(i.root_id == "primary" for i in pack.items)
        assert pack.total_tokens <= 1000
        paths = [i.path for i in pack.items]
        assert "api_key.json" not in paths
        assert len(paths) == len(set(paths))

    def test_empty_query_lists(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x=1\n")
        pack = rank_context({"primary": tmp_path}, "")
        assert len(pack.items) >= 1


class TestDependencies:
    def test_order_and_cycle(self) -> None:
        plan = _plan(deps={"b": ("a",), "c": ("b",)})
        assert dependency_order(plan) == ["a", "b", "c"]
        from quantsmind_agent.services.tasks.models import PlanRejected

        with pytest.raises(PlanRejected):
            dependency_order(_plan(order=("a", "b"), deps={"a": ("b",), "b": ("a",)}))

    def test_failure_propagation_and_resume(self) -> None:
        plan = _plan(deps={"b": ("a",), "c": ("b",)})
        blocked, runnable = propagate_failure(plan, "a", {"a"})
        assert blocked == {"b", "c"}
        assert runnable == set()
        statuses = {"a": "succeeded", "b": "pending", "c": "pending"}
        assert resume_candidates(statuses, plan) == ["b"]

    def test_retry_store(self, tmp_path: Path) -> None:
        from quantsmind_agent.services.tasks.dependencies import RetryPolicy

        store = TaskStateStore.open(tmp_path)
        store.record("t1", "s1", "failed", attempts=2)
        assert store.can_retry("t1", "s1", RetryPolicy(max_attempts=3))
        assert not store.can_retry("t1", "s1", RetryPolicy(max_attempts=2))
        # Restart-safe: reopen from disk, completed step not re-run.
        assert store.snapshot("t1")["steps"]["s1"]["attempts"] == 2


class TestMemory:
    def test_crud_verified_vs_suggested(self, tmp_path: Path) -> None:
        store = MemoryStore(tmp_path)
        e = MemoryEntry(
            entry_id="e1",
            title="Auth uses JWT",
            body="12h expiry",
            verified=True,
            source="docs/arch.md",
            provenance="arch.md",
        )
        store.add(e)
        assert store.list(verified_only=True)
        assert store.search("JWT")
        upd = store.update("e1", body="24h expiry")
        assert upd and upd.body == "24h expiry"
        assert store.invalidate("e1")
        assert store.list() == []

    def test_dedup_and_secret_refusal(self, tmp_path: Path) -> None:
        store = MemoryStore(tmp_path)
        store.add(MemoryEntry(entry_id="a", title="Same", body="1", verified=True))
        store.add(MemoryEntry(entry_id="b", title="Same", body="2", verified=False))
        assert len(store.list()) == 1
        from quantsmind_agent.services.memory import MemoryValidationError

        with pytest.raises(MemoryValidationError):
            store.add(MemoryEntry(entry_id="c", title="Leak", body="api_key = xyz"))

    def test_isolation(self, tmp_path: Path) -> None:
        (tmp_path / "a").mkdir()
        (tmp_path / "b").mkdir()
        MemoryStore(tmp_path / "a").add(MemoryEntry(entry_id="x", title="Only A", verified=True))
        assert MemoryStore(tmp_path / "b").list() == []
