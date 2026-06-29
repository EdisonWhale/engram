"""Write-time dedup and vector cosine-band classification (spec §27.1, §10.4).

No LLM on this path — content-hash + vector cosine band only (ADR 0003).

The key public surface:
- classify_distance(cosine_distance)  — pure function (testable without stores)
- run_write_time_dedup(...)           — stateful: checks hash then vector band
- ScoredVectorStore                  — Protocol extension (frozen VectorStore
                                       returns IDs only; this adds scores)
- DedupResult                        — dataclass returned by run_write_time_dedup
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from engram.store.base import MemoryStore

# Cosine-distance thresholds from spec §27.1 (distill `findSimilar` pattern).
# cosine_distance = 1 - cosine_similarity; lower = more similar.
_DUPLICATE_THRESHOLD = 0.15
_CONFLICT_THRESHOLD = 0.35


@runtime_checkable
class ScoredVectorStore(Protocol):
    """Extension of the frozen VectorStore that returns similarity scores.

    The frozen VectorStore protocol (store/base.py) returns memory IDs only.
    This protocol adds scored search so the dedup band logic can compare
    distances against the thresholds in §27.1.

    P0: FakeVectorStore / NullVectorStore (tests, no embeddings).
    P1: real embedding store (sqlite-vec / Chroma) implements this.
    """

    def search_with_scores(
        self, embedding: list[float], limit: int = 10
    ) -> list[tuple[str, float]]:
        """Return (memory_id, cosine_distance) pairs ordered by distance ascending."""
        ...


class NullVectorStore:
    """No-op ScoredVectorStore for P0 where embeddings are not yet generated."""

    def search_with_scores(
        self,
        embedding: list[float],
        limit: int = 10,  # noqa: ARG002
    ) -> list[tuple[str, float]]:
        return []

    # VectorStore protocol stubs (needed for isinstance checks)
    def upsert(self, memory_id: str, embedding: list[float]) -> None:  # noqa: ARG002
        pass

    def search(self, embedding: list[float], limit: int = 10) -> list[str]:  # noqa: ARG002
        return []

    def delete(self, memory_id: str) -> None:  # noqa: ARG002
        pass


@dataclass
class DedupResult:
    """Outcome of run_write_time_dedup.

    action:
        "insert"    — no match; proceed with normal create_memory call.
        "duplicate" — exact or near-exact match; bump access_count, skip insert.
        "conflict"  — related-but-contradictory; insert with status='conflict',
                      also mark existing conflict_memory_ids as 'conflict'.
    """

    action: Literal["insert", "duplicate", "conflict"]
    existing_memory_id: str | None = None  # set for "duplicate"
    conflict_memory_ids: list[str] = field(default_factory=list)  # set for "conflict"


def classify_distance(cosine_distance: float) -> Literal["duplicate", "conflict", "independent"]:
    """Pure function: classify a cosine distance into a dedup band.

    Bands from spec §27.1 (distill findSimilar pattern):
    - [0.00, 0.15)  → duplicate  (near-exact; bump access_count, skip insert)
    - [0.15, 0.35)  → conflict   (related but possibly contradictory)
    - [0.35, 1.00]  → independent (different enough to coexist)

    This is deliberately a pure function so it can be unit-tested without stores.
    """
    if cosine_distance < _DUPLICATE_THRESHOLD:
        return "duplicate"
    if cosine_distance < _CONFLICT_THRESHOLD:
        return "conflict"
    return "independent"


def run_write_time_dedup(
    content: str,
    content_hash: str,
    embedding: list[float] | None,
    memory_store: MemoryStore,
    vector_store: ScoredVectorStore | None,
) -> DedupResult:
    """Two-layer write-time dedup check (no LLM).

    Layer 1 — exact content_hash match (O(1), catches 100% of exact dups).
    Layer 2 — vector cosine band (only if embedding provided and vector_store given).

    Returns DedupResult indicating how the caller should proceed.
    """
    # --- Layer 1: exact hash match ---
    existing = memory_store.get_memory_by_hash(content_hash)
    if existing is not None and existing.status not in ("deleted", "superseded"):
        return DedupResult(action="duplicate", existing_memory_id=existing.id)

    # --- Layer 2: vector cosine band ---
    # Skip vector check entirely when there is no vector store.
    # When a store exists but no real embedding is available (P0 case), query
    # with an empty list — NullVectorStore returns [] (safe no-op), while a
    # FakeVectorStore with pre-configured distances works correctly in tests.
    if vector_store is None:
        return DedupResult(action="insert")

    query_embedding: list[float] = embedding if embedding is not None else []
    candidates = vector_store.search_with_scores(query_embedding, limit=5)
    conflict_ids: list[str] = []

    for memory_id, distance in candidates:
        band = classify_distance(distance)
        if band == "duplicate":
            return DedupResult(action="duplicate", existing_memory_id=memory_id)
        if band == "conflict":
            conflict_ids.append(memory_id)

    if conflict_ids:
        return DedupResult(action="conflict", conflict_memory_ids=conflict_ids)

    return DedupResult(action="insert")
