"""Tests for src/engram/eval/__init__.py.

Acceptance criteria (from docs/tasks/D-evals.md):
  - Recall@5 / MRR computed correctly against hand-verified fixtures
    (known-answer tests) — see TestRecallAt5 / TestMrr below.
  - A trace explains, for one query, exactly why each memory was or wasn't
    injected — see test_run_eval_persists_trace_per_case.
  - Injecting a known-stale memory raises stale_injection_rate as expected
    — see TestInjectionRate (synthetic status-list fixtures; today's
    retrieval safety filter never actually selects a non-active memory, so
    this is a regression guard rather than something a live run exercises).
  - engram eval exits non-zero on a metric regression beyond a threshold —
    covered at the cli.py level (not re-tested here; eval/ only owns the
    pure metric math + the replay runner).

Per CLAUDE.md, the metric math (recall_at_5, mrr, injection_rate,
abstain_rate) is forced test-first: pure functions, hand-verified fixtures,
no live DB needed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from engram.db.runner import open_db
from engram.eval import abstain_rate, injection_rate, load_gold_cases, mrr, recall_at_5, run_eval
from engram.models import EvalCase, Memory, Project
from engram.store.sqlite_store import SQLiteEventStore, SQLiteMemoryStore

_GOLD_SAMPLE = Path(__file__).parent / "fixtures" / "gold" / "sample.json"


# ---------------------------------------------------------------------------
# recall_at_5 — pure, hand-verified fixtures
# ---------------------------------------------------------------------------


class TestRecallAt5:
    def test_hit_at_rank_1(self) -> None:
        assert recall_at_5(["a"], ["a", "b", "c"]) == 1.0

    def test_hit_at_rank_5_boundary(self) -> None:
        # "a" is the 5th (index 4) selected id -- still within top 5.
        assert recall_at_5(["a"], ["x", "x", "x", "x", "a"]) == 1.0

    def test_miss_outside_top_5(self) -> None:
        # "a" is the 6th selected id -- outside top 5.
        assert recall_at_5(["a"], ["x", "x", "x", "x", "x", "a"]) == 0.0

    def test_miss_no_overlap(self) -> None:
        assert recall_at_5(["a"], ["x", "y", "z"]) == 0.0

    def test_multiple_expected_any_hit_counts(self) -> None:
        assert recall_at_5(["a", "b"], ["x", "b", "z"]) == 1.0

    def test_empty_expected_is_zero(self) -> None:
        # Abstain-expecting cases contribute 0.0 here by definition (any()
        # over an empty list is False); abstain_rate tracks correctness
        # for these cases separately.
        assert recall_at_5([], ["a", "b"]) == 0.0

    def test_empty_selected_is_zero(self) -> None:
        assert recall_at_5(["a"], []) == 0.0


# ---------------------------------------------------------------------------
# mrr — pure, hand-verified fixtures
# ---------------------------------------------------------------------------


class TestMrr:
    def test_first_rank(self) -> None:
        assert mrr(["a"], ["a", "b", "c"]) == 1.0

    def test_second_rank(self) -> None:
        assert mrr(["a"], ["x", "a", "c"]) == pytest.approx(0.5)

    def test_third_rank(self) -> None:
        assert mrr(["a"], ["x", "y", "a"]) == pytest.approx(1.0 / 3.0)

    def test_no_match(self) -> None:
        assert mrr(["a"], ["x", "y", "z"]) == 0.0

    def test_empty_expected(self) -> None:
        assert mrr([], ["x", "y"]) == 0.0

    def test_empty_selected(self) -> None:
        assert mrr(["a"], []) == 0.0

    def test_first_matching_rank_wins_when_multiple_expected_present(self) -> None:
        # "b" is expected and appears at rank 1; "a" is also expected but
        # appears later -- reciprocal rank is of the *first* hit.
        assert mrr(["a", "b"], ["b", "a"]) == 1.0


# ---------------------------------------------------------------------------
# injection_rate — pure, synthetic status-list fixtures
# ---------------------------------------------------------------------------


class TestInjectionRate:
    def test_stale_fraction(self) -> None:
        assert injection_rate(["active", "stale", "active"], "stale") == pytest.approx(1 / 3)

    def test_conflict_fraction(self) -> None:
        assert injection_rate(["active", "conflict"], "conflict") == pytest.approx(0.5)

    def test_zero_injected_is_zero_not_nan(self) -> None:
        assert injection_rate([], "stale") == 0.0

    def test_all_target_status(self) -> None:
        assert injection_rate(["stale", "stale"], "stale") == 1.0

    def test_no_target_status_present(self) -> None:
        assert injection_rate(["active", "active"], "stale") == 0.0


# ---------------------------------------------------------------------------
# abstain_rate — pure, hand-built correctness-flag fixtures
# ---------------------------------------------------------------------------


class TestAbstainRate:
    def test_all_correct(self) -> None:
        assert abstain_rate([True, True]) == 1.0

    def test_all_incorrect(self) -> None:
        assert abstain_rate([False, False]) == 0.0

    def test_mixed(self) -> None:
        assert abstain_rate([True, False]) == 0.5

    def test_vacuous_when_no_case_expects_abstain(self) -> None:
        assert abstain_rate([]) == 1.0


# ---------------------------------------------------------------------------
# load_gold_cases
# ---------------------------------------------------------------------------


def test_load_gold_cases_sample_fixture() -> None:
    cases = load_gold_cases(_GOLD_SAMPLE)

    assert len(cases) == 3
    assert all(isinstance(c, EvalCase) for c in cases)

    first = cases[0]
    assert first.query == "what database does engram use as the source of truth"
    assert first.project_id == "proj-eval-fixture"
    assert first.expected_memory_ids == ["mem-decision-sqlite-source"]
    assert first.expected_memory_types == ["decision"]
    assert first.must_not_include_ids == ["mem-old-postgres-plan"]

    abstain_case = cases[2]
    assert abstain_case.expected_memory_ids == []
    assert abstain_case.tags == ["abstain", "out-of-scope"]


def test_load_gold_cases_rejects_non_array(tmp_path: Path) -> None:
    bad_file = tmp_path / "not_an_array.json"
    bad_file.write_text(json.dumps({"query": "oops"}))

    with pytest.raises(ValueError, match="must contain a JSON array"):
        load_gold_cases(bad_file)


def test_load_gold_cases_reports_case_index_on_validation_error(tmp_path: Path) -> None:
    bad_file = tmp_path / "missing_project_id.json"
    # project_id is required on EvalCase; omitting it must fail fast here,
    # not as a KeyError deep inside run_eval.
    bad_file.write_text(json.dumps([{"query": "valid case has no project_id"}]))

    with pytest.raises(ValueError, match=r"case\[0\]") as exc_info:
        load_gold_cases(bad_file)
    assert isinstance(exc_info.value.__cause__, ValidationError)


# ---------------------------------------------------------------------------
# run_eval — integration (in-memory SQLite, conn/store fixture pattern from
# tests/test_retrieval.py)
# ---------------------------------------------------------------------------

PROJECT_ID = "proj-eval-int-001"


@pytest.fixture()
def conn() -> sqlite3.Connection:
    return open_db(":memory:")


@pytest.fixture()
def store(conn: sqlite3.Connection) -> SQLiteMemoryStore:
    SQLiteEventStore(conn).create_project(
        Project(id=PROJECT_ID, root_path="/tmp/eval-int-test", name="eval-int-test")
    )
    return SQLiteMemoryStore(conn)


def _memory(*, title: str, content: str, type: str = "decision") -> Memory:
    return Memory(
        project_id=PROJECT_ID,
        scope="project",
        type=type,
        origin="user",
        title=title,
        content=content,
        content_hash=Memory.compute_hash(content),
    )


def test_run_eval_persists_run_and_aggregates_metrics(
    conn: sqlite3.Connection, store: SQLiteMemoryStore
) -> None:
    mem = _memory(
        title="Use SQLite as source of truth",
        content="SQLite is the authoritative store; FTS5 and vector are derived state.",
    )
    store.create_memory(mem)

    hit_case = EvalCase(
        query="sqlite source of truth",
        project_id=PROJECT_ID,
        expected_memory_ids=[mem.id],
        expected_memory_types=["decision"],
    )
    abstain_case = EvalCase(
        query="deploy a rust microservice to kubernetes",
        project_id=PROJECT_ID,
        expected_memory_ids=[],
    )

    run = run_eval([hit_case, abstain_case], memory_store=store, run_name="test-run")

    assert run.run_name == "test-run"
    assert run.recall_at_5 == pytest.approx(0.5)  # 1 hit, 1 (zero-by-definition) abstain case
    assert run.mrr == pytest.approx(0.5)
    assert run.abstain_rate == 1.0  # the one abstain-expecting case correctly returned nothing
    assert run.stale_injection_rate == 0.0
    assert run.conflict_injection_rate == 0.0

    persisted = store.list_eval_runs()
    assert len(persisted) == 1
    assert persisted[0].id == run.id
    assert persisted[0].run_name == "test-run"


def test_run_eval_persists_trace_per_case(
    conn: sqlite3.Connection, store: SQLiteMemoryStore
) -> None:
    mem = _memory(
        title="Use SQLite as source of truth",
        content="SQLite is the authoritative store; FTS5 and vector are derived state.",
    )
    store.create_memory(mem)

    case = EvalCase(
        query="sqlite source of truth",
        project_id=PROJECT_ID,
        expected_memory_ids=[mem.id],
    )

    run_eval([case], memory_store=store, run_name="trace-test")

    rows = conn.execute("SELECT * FROM retrieval_traces").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["query"] == "sqlite source of truth"
    assert row["project_id"] == PROJECT_ID
    assert json.loads(row["selected_memory_ids_json"]) == [mem.id]
    assert row["outcome_label"] == "ok"


def test_run_eval_labels_must_not_include_violation(
    conn: sqlite3.Connection, store: SQLiteMemoryStore
) -> None:
    # The query contains a token unique to `forbidden`'s content, so BM25
    # guarantees it's selected -- this deterministically exercises the
    # violation path instead of depending on incidental ranking behaviour.
    forbidden = _memory(
        title="Forbidden decision", content="zzqxforbiddentoken policy superseded note"
    )
    store.create_memory(forbidden)

    case = EvalCase(
        query="zzqxforbiddentoken policy",
        project_id=PROJECT_ID,
        expected_memory_ids=[],
        must_not_include_ids=[forbidden.id],
    )

    run_eval([case], memory_store=store, run_name="violation-test")

    row = conn.execute("SELECT * FROM retrieval_traces").fetchone()
    selected = json.loads(row["selected_memory_ids_json"])
    assert forbidden.id in selected
    assert row["outcome_label"] == "violation:must_not_include"
