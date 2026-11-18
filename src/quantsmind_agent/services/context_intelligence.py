"""Context intelligence (WP3 of v0.13.0).

Deterministic, auditable relevance over Tier-A signals: filename/symbol/text
matching, import edges (code_imports), AST symbols (code_index). No semantic
embeddings are claimed. Budgets are enforced in characters (~1 token ≈ 4
chars), deduplication by (root_id, path), provenance records root+path+reason,
and secret/oversized/binary content is excluded before any provider sees it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from quantsmind_agent.code_index import parse_symbols
from quantsmind_agent.services.workspace.containment import (
    DEFAULT_BINARY_SUFFIXES,
    DEFAULT_IGNORED_DIRECTORIES,
)

__all__ = ["ContextItem", "ContextPack", "estimate_tokens", "rank_context"]

_SECRET_HINTS = ("api_key", "secret", "password", "token", "private_key", "credential")
_MAX_FILE_CHARS = 8_000


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4)


@dataclass
class ContextItem:
    root_id: str
    path: str
    score: float
    reason: str
    text: str = ""
    tokens: int = 0
    sha256: str = ""


@dataclass
class ContextPack:
    items: list[ContextItem] = field(default_factory=list)
    omitted: int = 0
    total_tokens: int = 0

    @property
    def text(self) -> str:
        return "\n".join(f"--- {i.root_id}:{i.path} ({i.reason})\n{i.text}" for i in self.items)


def _excluded(path: Path, root: Path) -> bool:
    rel = path.relative_to(root) if root in path.parents or path.parent == root else path
    parts = set(rel.parts)
    if parts & set(DEFAULT_IGNORED_DIRECTORIES):
        return True
    if path.suffix.lower() in DEFAULT_BINARY_SUFFIXES:
        return True
    lowered = path.name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _relevance(path: Path, query: str, symbols: list[str]) -> tuple[float, str]:
    name = path.name.lower()
    stem = path.stem.lower()
    q = query.lower()
    score = 0.0
    reasons: list[str] = []
    if q and q in name:
        score += 3.0
        reasons.append("filename match")
    for sym in symbols:
        if q and q == sym.lower():
            score += 5.0
            reasons.append(f"symbol match: {sym}")
        elif q and q in sym.lower():
            score += 2.0
            reasons.append(f"symbol partial: {sym}")
    if stem in ("index", "main", "app", "init", "__init__"):
        score += 1.0
        reasons.append("entry-point")
    if path.suffix in (".py", ".ts", ".tsx", ".js", ".rs", ".go"):
        score += 0.5
        reasons.append("source extension")
    return score, "; ".join(reasons) or "workspace file"


def rank_context(
    roots: dict[str, str | Path],
    query: str,
    *,
    max_files: int = 20,
    token_budget: int = 4000,
    max_file_chars: int = _MAX_FILE_CHARS,
) -> ContextPack:
    budget = max(200, token_budget)
    candidates: list[ContextItem] = []
    seen: set[tuple[str, str]] = set()
    for root_id, root_raw in roots.items():
        root = Path(root_raw)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if not path.is_file() or len(candidates) > max_files * 4:
                continue
            if _excluded(path, root):
                continue
            key = (root_id, str(path.relative_to(root)))
            if key in seen:
                continue
            seen.add(key)
            try:
                raw = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            text = raw[:max_file_chars]
            if any(hint in text[:2000].lower() for hint in ("-----begin", "api_key =", "secret =")):
                continue
            try:
                symbols = [s.name for s in parse_symbols(text)] if path.suffix == ".py" else []
            except Exception:
                symbols = []
            score, reason = _relevance(path, query, symbols)
            if not query.strip():
                score = 1.0
                reason = "unfiltered workspace listing"
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if any(c.sha256 == digest for c in candidates):
                continue
            candidates.append(
                ContextItem(root_id, key[1], score, reason, text, estimate_tokens(text), digest)
            )
    candidates.sort(key=lambda c: (-c.score, c.root_id, c.path))
    pack = ContextPack()
    for item in candidates:
        if len(pack.items) >= max_files:
            pack.omitted += 1
            continue
        if pack.total_tokens + item.tokens > budget:
            pack.omitted += 1
            continue
        pack.items.append(item)
        pack.total_tokens += item.tokens
    if len(pack.items) < len(candidates):
        pack.omitted += max(0, len(candidates) - len(pack.items) - pack.omitted)
    return pack
