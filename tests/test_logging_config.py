"""Tests for centralized logging configuration (src/engram/logging_config.py).

Covers:
- handler is attached to stderr, never stdout
- ENGRAM_LOG_LEVEL env var is honoured; invalid value falls back to INFO
- calling setup_logging() twice does not add duplicate handlers
"""

from __future__ import annotations

import logging
import sys

import pytest

from engram.logging_config import setup_logging


def _reset_engram_logger() -> None:
    """Return the 'engram' logger to an unconfigured state."""
    lg = logging.getLogger("engram")
    lg.handlers.clear()
    lg.setLevel(logging.NOTSET)
    lg.propagate = True


@pytest.fixture(autouse=True)
def _clean_logger():
    """Ensure each test starts and ends with a fresh engram logger."""
    _reset_engram_logger()
    yield
    _reset_engram_logger()


# ---------------------------------------------------------------------------
# stderr / stdout
# ---------------------------------------------------------------------------


def test_handler_targets_stderr(monkeypatch):
    monkeypatch.delenv("ENGRAM_LOG_LEVEL", raising=False)
    setup_logging()
    lg = logging.getLogger("engram")
    assert lg.handlers, "no handlers attached after setup_logging()"
    handler = lg.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert handler.stream is sys.stderr


def test_handler_never_targets_stdout(monkeypatch):
    monkeypatch.delenv("ENGRAM_LOG_LEVEL", raising=False)
    setup_logging()
    for h in logging.getLogger("engram").handlers:
        assert getattr(h, "stream", None) is not sys.stdout, (
            "a handler writes to stdout — this corrupts the MCP JSON-RPC channel"
        )


# ---------------------------------------------------------------------------
# Level from env
# ---------------------------------------------------------------------------


def test_default_level_is_info(monkeypatch):
    monkeypatch.delenv("ENGRAM_LOG_LEVEL", raising=False)
    setup_logging()
    assert logging.getLogger("engram").level == logging.INFO


def test_env_level_debug_honored(monkeypatch):
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "DEBUG")
    setup_logging()
    assert logging.getLogger("engram").level == logging.DEBUG


def test_env_level_warning_honored(monkeypatch):
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "WARNING")
    setup_logging()
    assert logging.getLogger("engram").level == logging.WARNING


def test_invalid_env_level_falls_back_to_info_without_raising(monkeypatch):
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "NOTAVALIDLEVEL")
    setup_logging()  # must not raise
    assert logging.getLogger("engram").level == logging.INFO


def test_empty_env_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("ENGRAM_LOG_LEVEL", "")
    setup_logging()
    assert logging.getLogger("engram").level == logging.INFO


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_two_calls_produce_exactly_one_handler(monkeypatch):
    monkeypatch.delenv("ENGRAM_LOG_LEVEL", raising=False)
    setup_logging()
    setup_logging()
    assert len(logging.getLogger("engram").handlers) == 1


# ---------------------------------------------------------------------------
# Scope (does not hijack root / third-party loggers)
# ---------------------------------------------------------------------------


def test_propagate_is_false(monkeypatch):
    monkeypatch.delenv("ENGRAM_LOG_LEVEL", raising=False)
    setup_logging()
    assert logging.getLogger("engram").propagate is False


def test_root_logger_handlers_unchanged(monkeypatch):
    monkeypatch.delenv("ENGRAM_LOG_LEVEL", raising=False)
    root_handlers_before = list(logging.getLogger().handlers)
    setup_logging()
    assert logging.getLogger().handlers == root_handlers_before
