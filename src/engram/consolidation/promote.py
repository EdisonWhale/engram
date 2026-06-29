"""Promotion policy: classify events into long-term memory candidates (spec §6.3, §10.3).

Decides WHAT events become long-term memories and WHAT type they get.
Does not touch stores — pure classification and model construction.
"""

from __future__ import annotations

from typing import Literal

from engram.models import Event, Memory, MemoryOrigin, MemorySource, MemoryType

# Promotion reason vocabulary mirrors spec §6.3 promotion rules.
PromotionReason = Literal[
    "standing_instruction",
    "correction",
    "confirmed_decision",
    "verified_fact",
    "stable_command",
    "repeated_failure",
]

# Map event_type → promotion reason.
# Only event types in this map trigger long-term promotion; all others are ignored.
_PROMOTION_MAP: dict[str, PromotionReason] = {
    "decision": "confirmed_decision",
    "instruction": "standing_instruction",
    "correction": "correction",
    "fact": "verified_fact",
    "command": "stable_command",
    "failure_pattern": "repeated_failure",
}

# Map promotion reason → Memory.type
_REASON_TO_MEMORY_TYPE: dict[PromotionReason, MemoryType] = {
    "standing_instruction": "preference",
    "correction": "preference",
    "confirmed_decision": "decision",
    "verified_fact": "project_fact",
    "stable_command": "command",
    "repeated_failure": "failure_pattern",
}


def classify_event_for_promotion(event: Event) -> PromotionReason | None:
    """Return the promotion reason for *event*, or None if it should not be promoted."""
    return _PROMOTION_MAP.get(event.event_type)


def is_promotable(event: Event) -> bool:
    """True if this event warrants long-term memory promotion."""
    return classify_event_for_promotion(event) is not None


def build_memory_from_event(
    event: Event,
    reason: PromotionReason,
    title: str,
    content: str,
    origin: MemoryOrigin,
) -> tuple[Memory, list[MemorySource]]:
    """Construct a Memory + one MemorySource provenance row from a single event.

    Does not call the store.  Caller is responsible for running dedup checks
    before persisting the returned objects.

    Returns:
        (memory, [source]) — source list always has exactly one element.
    """
    mem = Memory(
        project_id=event.project_id,
        scope="project",
        type=_REASON_TO_MEMORY_TYPE[reason],
        origin=origin,
        title=title,
        content=content,
        content_hash=Memory.compute_hash(content),
        confidence=1.0,
    )
    source = MemorySource(
        memory_id=mem.id,
        source_type="event",
        source_id=event.id,
        quote_or_summary=content[:500],
    )
    return mem, [source]
