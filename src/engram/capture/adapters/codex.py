"""Codex transcript adapter — best-effort stub.

capture_confidence = "likely" | "unknown": Codex transcripts have a weaker,
less documented format than Claude Code.  This adapter is NOT guaranteed
complete and is explicitly labeled as such (ADR 0004).

Do not use this adapter in completeness calculations.  Gaps detected here
should always set capture_incomplete on the session.

TODO (P1 WS-A follow-up): implement real Codex transcript parsing once the
actual JSONL format is confirmed against live fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from engram.capture.adapters.claude_code import ParsedEvent


class CodexAdapter:
    """Best-effort adapter for Codex session transcripts.

    capture_confidence is "likely" for records where the shape is recognisable,
    "unknown" for everything else.  Do NOT rely on this adapter for completeness
    guarantees — see ADR 0004.
    """

    def process_record(
        self,
        record: dict[str, Any],
        byte_offset: int,
        source_seq: int,
    ) -> list[ParsedEvent]:
        """Parse one Codex record.  Returns zero or more ParsedEvents.

        Stub: emits a single generic tool_call event for any dict it receives,
        marked capture_confidence="unknown".  Replace with real parsing when
        the Codex format is confirmed.
        """
        ts = datetime.now(UTC)
        return [
            ParsedEvent(
                event_type="tool_call",
                payload={"raw": record},
                occurred_at=ts,
                source_seq=source_seq,
                raw_ref_offset=byte_offset,
                capture_confidence="unknown",  # NOT exact — best-effort only
            )
        ]
