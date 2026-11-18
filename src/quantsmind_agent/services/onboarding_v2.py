"""Onboarding v2 seeding (WP v1.5).

Seed a workspace with git init (optional), a starter profile and memory
conventions from existing ARCHITECTURE/docs. Idempotent; never overwrites.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from quantsmind_agent.services.memory import KnowledgeKind, MemoryEntry, MemoryStore
from quantsmind_agent.services.profiles import EngineeringProfile, ProfileStore

__all__ = ["seed_workspace"]


def seed_workspace(root: str | Path, *, init_git: bool = True) -> dict[str, object]:
    root = Path(root)
    actions: list[str] = []
    if init_git and not (root / ".git").is_dir():
        try:
            subprocess.run(
                ["git", "init", "-b", "main"], cwd=str(root), capture_output=True, check=True
            )
            actions.append("git init")
        except (OSError, subprocess.CalledProcessError):
            actions.append("git init skipped (git unavailable)")
    store = ProfileStore(root)
    if store.get("default") is None:
        store.save(
            EngineeringProfile(
                profile_id="default",
                title="Starter profile",
                languages=["Python"],
                frameworks=["PySide6"],
                build_commands=["python -m pytest -q"],
            )
        )
        actions.append("profile seeded")
    mem = MemoryStore(root)
    if not mem.search("conventions"):
        mem.add(
            MemoryEntry(
                entry_id="seed-conventions",
                kind=KnowledgeKind.CONVENTION,
                title="conventions seeded",
                body="See ARCHITECTURE; ask before large changes.",
                verified=False,
                source="onboarding-v2",
            )
        )
        actions.append("memory seeded")
    return {"actions": actions, "root": str(root)}
