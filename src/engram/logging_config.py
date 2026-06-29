"""Centralized logging configuration for Engram.

Call ``setup_logging()`` once at CLI startup.  Every module keeps its own
``logging.getLogger(__name__)`` unchanged — this module only configures the
handlers, formatter, and level on the ``"engram"`` logger namespace.

Logs are sent to ``sys.stderr`` so they never corrupt the JSON-RPC channel
on ``stdout`` used by ``engram mcp``.
"""

from __future__ import annotations

import logging
import os
import sys

_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_DEFAULT_LEVEL = logging.INFO


def setup_logging() -> None:
    """Configure the ``engram`` logger for the current process.

    Behaviour:
    - **stderr only**: logs go to ``sys.stderr``; stdout is never touched.
    - **Level from env**: reads ``ENGRAM_LOG_LEVEL`` (e.g. ``"DEBUG"``,
      ``"INFO"``, ``"WARNING"``); invalid values fall back to ``INFO`` without
      raising.
    - **Idempotent**: a second call is a no-op — duplicate handlers are not
      added.
    - **Scoped**: configures the ``"engram"`` logger with
      ``propagate = False`` so it does not hijack root logging for third-party
      libraries.
    """
    logger = logging.getLogger("engram")

    # Idempotency guard: already configured, nothing to do.
    if logger.handlers:
        return

    level_str = os.environ.get("ENGRAM_LOG_LEVEL", "").strip().upper()
    level = getattr(logging, level_str, None)
    if not isinstance(level, int):
        level = _DEFAULT_LEVEL

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))

    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
