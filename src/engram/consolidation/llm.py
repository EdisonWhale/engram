"""LLM client interface and implementations.

LLMs are called ONLY in idle-time consolidation (summaries, extraction,
conflict explanation). They are NEVER invoked on the write/capture path
(ADR 0003, CLAUDE.md architectural invariant).

Public surface:
- LLMClient   — Protocol; the single call site the rest of consolidation uses.
- MockLLMClient  — deterministic fake for tests (no API key needed).
- AnthropicLLMClient — real client; lazy SDK import so tests that don't need it
                       run without the anthropic package installed.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class LLMClient(Protocol):
    """Minimal interface for synchronous LLM text completion."""

    def complete(self, prompt: str) -> str:
        """Send *prompt* and return the text completion as a plain string."""
        ...


class MockLLMClient:
    """Deterministic mock for tests.

    Pass ``canned`` to always return the same string regardless of prompt.
    All calls are recorded in ``self.calls`` for assertion in tests.
    """

    def __init__(self, canned: str = "") -> None:
        self._canned = canned
        self.calls: list[str] = []

    def complete(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self._canned

    @property
    def call_count(self) -> int:
        return len(self.calls)


class AnthropicLLMClient:
    """Real Anthropic-backed LLM client.

    The SDK client is constructed lazily so importing this module never
    requires the ``anthropic`` package to be installed.  Tests that don't
    exercise the live path can import and instantiate AnthropicLLMClient
    freely.

    Model defaults to ``claude-sonnet-4-6``; override via *model* param or
    set ``ENGRAM_LLM_MODEL`` in the environment.
    """

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        max_tokens: int = 1024,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._client: object | None = None  # lazy

    def _get_client(self) -> object:
        if self._client is None:
            try:
                import anthropic  # type: ignore[import-not-found]

                self._client = anthropic.Anthropic()
            except ImportError as exc:
                raise ImportError(
                    "The 'anthropic' package is required for AnthropicLLMClient. "
                    "Install it with: pip install 'engram[llm]'"
                ) from exc
        return self._client

    def complete(self, prompt: str) -> str:
        client = self._get_client()
        # Access via attribute to avoid hard import at module scope
        message = client.messages.create(  # type: ignore[attr-defined]
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text  # type: ignore[union-attr]
