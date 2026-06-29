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
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


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
            except OSError as exc:
                # File disappeared or is unreadable; skip this iteration. The
                # offset is not advanced, so a transient error retries next pass;
                # a persistent one (e.g. permissions) would otherwise drop a whole
                # session silently — log it.
                logger.warning("cannot read transcript %s: %s", jsonl_file, exc)
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
                decoded = raw_line.decode(errors="replace")
                if "�" in decoded:
                    logger.warning(
                        "invalid UTF-8 in %s at offset %d; bytes replaced with U+FFFD",
                        jsonl_file,
                        byte_offset,
                    )
                yield decoded, jsonl_file, byte_offset

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
        # Missing file is the normal first-run case — silent. A file that EXISTS
        # but is corrupt is abnormal: resetting to {} reprocesses every transcript
        # from byte 0, and with no DB-level content_hash dedup that re-emits
        # duplicate events. Surface it so the duplicate ingestion is explained.
        if not self._offset_store_path.exists():
            return {}
        try:
            with self._offset_store_path.open() as fh:
                data = json.load(fh)
            return {k: int(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            logger.warning(
                "offset store %s is corrupt (%s); resetting — transcripts will be "
                "reprocessed and may produce duplicate events",
                self._offset_store_path,
                exc,
            )
            return {}

    def _save_offsets(self) -> None:
        try:
            self._offset_store_path.parent.mkdir(parents=True, exist_ok=True)
            with self._offset_store_path.open("w") as fh:
                json.dump(self._offsets, fh)
        except OSError as exc:
            # Best-effort; next run will reprocess from last saved offset (which
            # can re-emit duplicates). Log so a persistent failure is visible.
            logger.warning("could not persist offset store %s: %s", self._offset_store_path, exc)


def parse_transcript_lines(
    lines: list[str],
    *,
    raw_ref_file: str = "",
    start_offset: int = 0,
) -> Iterator[tuple[dict[str, Any], str, int, int]]:
    """Parse transcript lines into (record_dict, raw_ref_file, byte_offset, source_seq).

    ``source_seq`` is the 1-based index of the line among non-blank lines — i.e.
    the source-of-truth line number used for completeness checking (spec §26).
    The caller passes ``source_seq`` straight through to ``record_event`` so a
    dropped record surfaces as a gap at ``session_end``.

    Completeness invariant: a malformed (non-JSON) line is NOT silently skipped.
    Its source_seq is consumed (the counter advances) but no event is emitted for
    it, so the missing number shows up as a provable gap rather than vanishing.
    Blank lines are not records and do not consume a source_seq.
    """
    offset = start_offset
    source_seq = 0
    for line in lines:
        line_bytes = line.encode()
        if line.strip():
            source_seq += 1
            try:
                record = json.loads(line)
                yield record, raw_ref_file, offset, source_seq
            except json.JSONDecodeError:
                # Consume the source_seq but emit nothing → detectable gap, not a
                # silent drop. Never raise: one bad line must not abort capture.
                logger.warning(
                    "unparseable transcript line at %s offset %d (source_seq=%d); "
                    "recorded as a capture gap",
                    raw_ref_file or "<memory>",
                    offset,
                    source_seq,
                )
        offset += len(line_bytes) + 1  # +1 for the newline


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _read_from_offset(file_path: Path, offset: int) -> bytes:
    """Read bytes starting at *offset* from *file_path*."""
    with file_path.open("rb") as fh:
        fh.seek(offset)
        return fh.read()
