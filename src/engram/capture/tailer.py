"""Transcript tailer — tails ~/.claude/projects/<encoded_cwd>/*.jsonl.

Design decisions (spec §26, ADR 0002):
- Watch the DIRECTORY, not a single file — a new session may create a new JSONL
  file mid-run.
- Persist a byte offset per file so a restart resumes without reprocessing.
- Truncate reads at the last '\n' to avoid parsing half-written lines (the
  transcript JSONL is append-only, so a line without a terminal '\n' is
  still being written).
- store raw_ref_file + raw_ref_offset on every event for deterministic replay.

encodeClaudeProjectPath(cwd): replace '/' → '-' and '.' → '-'
  e.g. /Users/alice/project.git → -Users-alice-project-git
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def encode_project_path(cwd: str) -> str:
    """Encode a filesystem path to a Claude Code project directory name.

    Algorithm: replace every '/' and '.' character with '-'.
    This is the encodeClaudeProjectPath(cwd) function referenced in the spec.
    """
    return cwd.replace("/", "-").replace(".", "-")


def default_transcript_dir(cwd: str) -> Path:
    """Return ~/.claude/projects/<encoded_cwd>/ for a given working directory."""
    encoded = encode_project_path(cwd)
    return Path.home() / ".claude" / "projects" / encoded


class TranscriptTailer:
    """Tails all *.jsonl files under a transcript directory.

    Each call to tail_new_lines() yields new (line, file_path, byte_offset)
    tuples since the last call.  Byte offsets are persisted between runs via
    an offset store file.

    Usage::

        tailer = TranscriptTailer(cwd="/Users/alice/project")
        for line, file_path, byte_offset in tailer.tail_new_lines():
            rec = json.loads(line)
            ...

    Design note: this class is intentionally synchronous.  The async MCP layer
    calls it from a thread-pool executor if needed (no web framework — ADR 0006).
    """

    def __init__(
        self,
        cwd: str,
        offset_store_path: Path | None = None,
        transcript_dir: Path | None = None,
    ) -> None:
        self._cwd = cwd
        self._transcript_dir: Path = transcript_dir or default_transcript_dir(cwd)
        # Offset store: a JSON file mapping filename → byte offset
        self._offset_store_path: Path = offset_store_path or (
            self._transcript_dir / ".engram_offsets.json"
        )
        self._offsets: dict[str, int] = self._load_offsets()

    def get_transcript_dir(self) -> Path:
        """Return the directory being watched."""
        return self._transcript_dir

    def tail_new_lines(self) -> Iterator[tuple[str, Path, int]]:
        """Yield (decoded_line, file_path, byte_offset) for unread data.

        Truncates reads at the last '\\n' to avoid yielding partial lines.
        Persists byte offsets after each file is fully read.
        """
        if not self._transcript_dir.exists():
            return

        for jsonl_file in sorted(self._transcript_dir.glob("*.jsonl")):
            filename = jsonl_file.name
            current_offset = self._offsets.get(filename, 0)

            try:
                data = _read_from_offset(jsonl_file, current_offset)
            except OSError:
                # File disappeared or is unreadable; skip this iteration.
                continue

            if not data:
                continue

            # Truncate at the last newline to avoid parsing a half-written line.
            last_nl = data.rfind(b"\n")
            if last_nl == -1:
                # No complete line yet — wait for next tail.
                continue
            safe_data = data[: last_nl + 1]

            new_offset = current_offset
            for raw_line in safe_data.split(b"\n"):
                line_bytes = raw_line + b"\n"
                if not raw_line.strip():
                    new_offset += len(line_bytes)
                    continue
                byte_offset = new_offset
                new_offset += len(line_bytes)
                yield raw_line.decode(errors="replace"), jsonl_file, byte_offset

            self._offsets[filename] = new_offset
            self._save_offsets()

    def reset_offsets(self) -> None:
        """Reset all byte offsets to zero (re-process all transcript data)."""
        self._offsets = {}
        self._save_offsets()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_offsets(self) -> dict[str, int]:
        if not self._offset_store_path.exists():
            return {}
        try:
            with self._offset_store_path.open() as fh:
                data = json.load(fh)
            return {k: int(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError, ValueError):
            return {}

    def _save_offsets(self) -> None:
        try:
            self._offset_store_path.parent.mkdir(parents=True, exist_ok=True)
            with self._offset_store_path.open("w") as fh:
                json.dump(self._offsets, fh)
        except OSError:
            # Best-effort; next run will reprocess from last saved offset.
            pass


def parse_transcript_lines(
    lines: list[str],
    *,
    raw_ref_file: str = "",
    start_offset: int = 0,
) -> Iterator[tuple[dict[str, Any], str, int]]:
    """Parse pre-loaded transcript lines into (record_dict, raw_ref_file, byte_offset).

    Used in tests and one-shot backfill scenarios where the full content is
    already in memory.  For production live-tailing, use TranscriptTailer.

    Skips blank lines and non-JSON lines (logs the error, does not raise).
    """
    offset = start_offset
    for line in lines:
        line_bytes = line.encode()
        if line.strip():
            try:
                record = json.loads(line)
                yield record, raw_ref_file, offset
            except json.JSONDecodeError:
                pass  # Build defensively — skip unparseable lines
        offset += len(line_bytes) + 1  # +1 for the newline


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _read_from_offset(file_path: Path, offset: int) -> bytes:
    """Read bytes starting at *offset* from *file_path*."""
    with file_path.open("rb") as fh:
        fh.seek(offset)
        return fh.read()
