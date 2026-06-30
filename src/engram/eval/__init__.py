"""Replay evals: gold-set loader, metric math, and the eval runner (spec §12).

Workflow::

    cases = load_gold_cases(Path("tests/fixtures/gold/sample.json"))
    run = run_eval(cases, memory_store=memory_store, run_name="baseline")

Design notes:
- Metric math (recall_at_5, mrr, injection_rate) is pure: no I/O, no store
  access, hand-verifiable against fixtures.  CLAUDE.md forces test-first here
  because eval metric math is high-risk (a silently wrong metric makes a
  regression invisible).
- run_eval is the only impure piece: it calls memory_context (WS-C retrieval)
  and memory_store.get_memory / create_retrieval_trace / create_eval_run,
  then delegates all arithmetic to the pure functions above — it does not
  duplicate their logic.
- Gold cases are not persisted to the eval_cases table for P0; the JSON file
  under tests/fixtures/gold/ is the durable, git-tracked source of truth
  (see docs/conventions.md §4).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from engram.models import EvalCase, EvalRun, RetrievalTrace
from engram.retrieval import memory_context
from engram.store.base import MemoryStore

__all__ = [
    "load_gold_cases",
    "recall_at_5",
    "mrr",
    "injection_rate",
    "abstain_rate",
    "run_eval",
]


# ---------------------------------------------------------------------------
# Gold-set loader (spec §12.1)
# ---------------------------------------------------------------------------


def load_gold_cases(path: Path) -> list[EvalCase]:
    """Load a JSON array of gold eval cases and validate each via EvalCase.

    Each array entry is shaped like spec §12.1's example: query, project_id,
    expected_memory_ids, expected_memory_types, must_not_include_ids, tags,
    and an optional expected_behavior.

    Raises ValueError (wrapping the underlying pydantic.ValidationError) that
    names the file and the offending case index if any entry is malformed,
    so a broken gold file fails fast and loud at load time rather than as a
    KeyError deep inside run_eval.
    """
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"gold file {path} must contain a JSON array, got {type(raw).__name__}")

    cases: list[EvalCase] = []
    for i, item in enumerate(raw):
        try:
            cases.append(EvalCase.model_validate(item))
        except ValidationError as exc:
            raise ValueError(f"gold file {path} case[{i}] failed validation: {exc}") from exc
    return cases


# ---------------------------------------------------------------------------
# Pure metric functions (spec §12.2) — test-first, no store/I-O required.
# ---------------------------------------------------------------------------


def recall_at_5(expected_ids: list[str], selected_ids: list[str]) -> float:
    """1.0 if any expected id appears in the first 5 selected ids, else 0.0.

    This is a per-case boolean hit, not a fraction of expected ids covered.
    EvalRun.recall_at_5 (the persisted run aggregate) is the *mean* of this
    value across all cases in the run — the spec doesn't spell out the
    aggregation, so that's the documented convention run_eval uses.  A case
    that expects abstain (expected_ids == []) contributes 0.0 here, since
    any() over an empty list is False; abstain correctness is tracked
    separately by abstain_rate.
    """
    top5 = selected_ids[:5]
    return 1.0 if any(mid in top5 for mid in expected_ids) else 0.0


def mrr(expected_ids: list[str], selected_ids: list[str]) -> float:
    """Reciprocal rank (1-indexed) of the first selected id that is expected.

    0.0 if none of selected_ids is in expected_ids (including when
    expected_ids is empty — there is nothing to rank).
    """
    expected = set(expected_ids)
    for rank, mid in enumerate(selected_ids, start=1):
        if mid in expected:
            return 1.0 / rank
    return 0.0


def injection_rate(statuses: list[str], target_status: str) -> float:
    """Fraction of injected memories whose status equals target_status.

    *statuses* is a plain list of Memory.status values for memories that
    were actually injected (selected) by retrieval — not the candidate set.
    Used for both stale_injection_rate (target_status="stale") and
    conflict_injection_rate (target_status="conflict") by the caller.

    Returns 0.0 when statuses is empty: "nothing was injected" reads as zero
    injected-bad-memories, not an undefined rate.

    Today's retrieval safety filter (memory_context Phase 4) never selects a
    non-"active" memory, so in a real run this will stay 0.0 — this function
    is a regression guard for that invariant.  It's still independently
    unit-testable against a hand-built status list like
    ["active", "stale", "active"] without any store or live retrieval.
    """
    if not statuses:
        return 0.0
    return sum(1 for s in statuses if s == target_status) / len(statuses)


def abstain_rate(correct_flags: list[bool]) -> float:
    """Fraction of abstain-expecting cases that were handled correctly.

    A case "expects abstain" when its expected_memory_ids == [].  It is
    correctly handled when retrieval's selected_ids is also empty for that
    case.  *correct_flags* is one bool per abstain-expecting case in the run
    (True iff that case's selected_ids was empty) — cases that don't expect
    abstain are not represented here at all.

    Vacuously 1.0 when correct_flags is empty, i.e. no case in the run
    expects abstain: there is nothing to get wrong on this axis, so a gold
    set with zero abstain cases shouldn't read as a regression on this
    metric.
    """
    if not correct_flags:
        return 1.0
    return sum(correct_flags) / len(correct_flags)


# ---------------------------------------------------------------------------
# Replay runner (spec §12.2/§12.3) — the impure half.
# ---------------------------------------------------------------------------


def _outcome_label(case: EvalCase, selected_ids: list[str]) -> str:
    """Outcome label persisted on RetrievalTrace.outcome_label for one case.

    must_not_include_ids violations aren't one of the 6 persisted EvalRun
    metrics (spec §12.2 doesn't define a column for them), so we record a
    violation by labelling the trace rather than inventing a new schema
    column.  A violation is reported even if the case also got a recall hit
    — injecting a forbidden memory is always a failure.
    """
    if any(mid in case.must_not_include_ids for mid in selected_ids):
        return "violation:must_not_include"
    if not case.expected_memory_ids:
        return "ok" if not selected_ids else "miss"  # abstain case
    return "ok" if recall_at_5(case.expected_memory_ids, selected_ids) == 1.0 else "miss"


def run_eval(
    cases: list[EvalCase],
    *,
    memory_store: MemoryStore,
    run_name: str,
    token_budget: int = 1200,
) -> EvalRun:
    """Replay every case through retrieval, persist traces, aggregate metrics.

    For each case: calls memory_context (WS-C) with the case's query and
    project_id, persists one RetrievalTrace built from the returned trace
    dict, and folds the per-case recall/MRR/injected-tokens values into the
    run aggregate.

    stale_injection_rate / conflict_injection_rate are computed over the
    *pooled* set of all memories injected across every case in the run (not
    a mean of per-case rates), so a case with more injections isn't
    underweighted relative to one with fewer — see injection_rate's
    docstring for the rate definition itself.

    abstain_rate is computed via the pure abstain_rate() function over one
    correctness flag per abstain-expecting case (see its docstring for the
    vacuous-1.0 convention when no case expects abstain).
    """
    recalls: list[float] = []
    mrrs: list[float] = []
    injected_token_counts: list[float] = []
    pooled_statuses: list[str] = []
    abstain_flags: list[bool] = []

    for case in cases:
        result = memory_context(
            case.query,
            memory_store=memory_store,
            project_id=case.project_id,
            token_budget=token_budget,
        )
        selected_ids: list[str] = result["memory_ids"]
        trace = result["trace"]

        memory_store.create_retrieval_trace(
            RetrievalTrace(
                query=trace["query"],
                project_id=trace["project_id"] or "",
                selected_memory_ids=trace["selected_ids"],
                candidate_memory_ids=trace["candidate_ids"],
                ranking_features=trace["ranking_features"],
                token_budget=trace["token_budget"],
                injected_tokens=trace["injected_tokens"],
                outcome_label=_outcome_label(case, selected_ids),
            )
        )

        recalls.append(recall_at_5(case.expected_memory_ids, selected_ids))
        mrrs.append(mrr(case.expected_memory_ids, selected_ids))
        injected_token_counts.append(float(result["injected_tokens"]))

        for mid in selected_ids:
            mem = memory_store.get_memory(mid)
            if mem is not None:
                pooled_statuses.append(mem.status)

        if not case.expected_memory_ids:
            abstain_flags.append(not selected_ids)

    n = len(cases)
    run = EvalRun(
        run_name=run_name,
        recall_at_5=(sum(recalls) / n) if n else 0.0,
        mrr=(sum(mrrs) / n) if n else 0.0,
        stale_injection_rate=injection_rate(pooled_statuses, "stale"),
        conflict_injection_rate=injection_rate(pooled_statuses, "conflict"),
        avg_injected_tokens=(sum(injected_token_counts) / n) if n else 0.0,
        abstain_rate=abstain_rate(abstain_flags),
    )
    return memory_store.create_eval_run(run)
