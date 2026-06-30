"""Engram CLI entry point.

Subcommands:
  engram mcp      — start the MCP server over stdio (production use)
  engram eval     — run retrieval replay evals against a gold set (WS-D)
  engram doctor   — health-check: database, migrations, MCP wiring
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from engram.logging_config import setup_logging

_DEFAULT_DB = Path.home() / ".engram" / "engram.db"

# `engram eval` exits non-zero if recall_at_5 or mrr drops by more than this
# many absolute points vs. the immediately preceding eval_runs row (CI gate;
# docs/tasks/D-evals.md acceptance criteria).
_REGRESSION_THRESHOLD = 0.05


def cmd_mcp(args: argparse.Namespace) -> None:
    """Launch the MCP server over stdio.  Blocks until stdin is closed."""
    from engram.mcp.server import mcp

    mcp.run()


def cmd_eval(args: argparse.Namespace) -> None:
    """Run replay evals against a gold set and print the metric table.

    Loads gold cases from --gold, verifies every project_id they reference
    has actually been captured in this database, replays each case through
    retrieval (persisting an EvalRun + per-case RetrievalTraces), and prints
    all 6 metrics.  Exits non-zero if recall_at_5 or mrr regressed by more
    than _REGRESSION_THRESHOLD vs. the previous eval_runs row (CI gate).
    """
    from engram.db.runner import open_db
    from engram.eval import load_gold_cases, run_eval
    from engram.store.sqlite_store import SQLiteEventStore, SQLiteMemoryStore

    db_path = Path(args.db) if args.db else _DEFAULT_DB
    conn = open_db(str(db_path))
    event_store = SQLiteEventStore(conn)
    memory_store = SQLiteMemoryStore(conn)

    gold_path = Path(args.gold)
    try:
        cases = load_gold_cases(gold_path)
    except (OSError, ValueError) as exc:
        print(f"engram eval: failed to load gold file {gold_path}: {exc}")
        sys.exit(1)

    for project_id in sorted({c.project_id for c in cases}):
        if event_store.get_project(project_id) is None:
            print(
                f"engram eval: project '{project_id}' referenced by {gold_path} "
                "has not been captured in this database — run capture first "
                "or fix the gold file"
            )
            sys.exit(1)

    run_name = args.run_name or f"eval-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    run = run_eval(cases, memory_store=memory_store, run_name=run_name)

    print(f"engram eval: {run.run_name} ({len(cases)} cases)")
    print(f"  recall_at_5              {run.recall_at_5:.3f}")
    print(f"  mrr                      {run.mrr:.3f}")
    print(f"  stale_injection_rate     {run.stale_injection_rate:.3f}")
    print(f"  conflict_injection_rate  {run.conflict_injection_rate:.3f}")
    print(f"  avg_injected_tokens      {run.avg_injected_tokens:.1f}")
    print(f"  abstain_rate             {run.abstain_rate:.3f}")

    # list_eval_runs is ordered created_at DESC: [0] is the run just inserted
    # above, [1] (if present) is the previous run to diff against.
    runs = memory_store.list_eval_runs()
    if len(runs) < 2:
        print("  (no previous run to compare)")
        sys.exit(0)

    previous = runs[1]
    regressed = False
    for metric_name in ("recall_at_5", "mrr"):
        delta = getattr(run, metric_name) - getattr(previous, metric_name)
        if delta < -_REGRESSION_THRESHOLD:
            print(
                f"  [REGRESSION] {metric_name} dropped by {-delta:.3f} "
                f"({getattr(previous, metric_name):.3f} -> {getattr(run, metric_name):.3f})"
            )
            regressed = True
        else:
            print(f"  {metric_name} delta vs previous run: {delta:+.3f}")

    sys.exit(1 if regressed else 0)


def cmd_doctor(args: argparse.Namespace) -> None:
    """Check Engram health: database, migrations, and MCP tool registration.

    Exits 0 if everything is healthy, 1 if any check fails.
    """
    db_path = Path(args.db) if args.db else _DEFAULT_DB
    ok = True

    print(f"engram doctor — checking {db_path}")

    # 1. Database reachable and migrations applied
    try:
        from engram.db.runner import open_db

        conn = open_db(str(db_path))

        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        expected_tables = {
            "projects",
            "agent_sessions",
            "events",
            "task_contexts",
            "memories",
            "session_summaries",
            "memory_sources",
            "retrieval_traces",
            "eval_cases",
            "eval_runs",
            "_migrations",
        }
        missing = expected_tables - tables
        if missing:
            print(f"  [FAIL] missing tables: {sorted(missing)}")
            ok = False
        else:
            print("  [OK]   all tables present")

        # Check WAL mode
        wal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        if wal == "wal":
            print("  [OK]   WAL mode active")
        else:
            print(f"  [WARN] journal_mode={wal} (expected wal)")

        conn.close()
    except Exception as exc:
        print(f"  [FAIL] database error: {exc}")
        ok = False

    # 2. MCP tool registration
    try:
        import asyncio

        from engram.mcp.server import _EXPECTED_TOOLS, mcp

        tools = asyncio.run(mcp.list_tools())
        registered = {t.name for t in tools}
        missing_tools = _EXPECTED_TOOLS - registered
        if missing_tools:
            print(f"  [FAIL] missing MCP tools: {sorted(missing_tools)}")
            ok = False
        else:
            print(f"  [OK]   {len(registered)} MCP tools registered")
    except Exception as exc:
        print(f"  [FAIL] MCP tool check error: {exc}")
        ok = False

    if ok:
        print("engram doctor: all checks passed")
        sys.exit(0)
    else:
        print("engram doctor: some checks failed")
        sys.exit(1)


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(
        prog="engram",
        description="Cross-session memory MCP server for AI coding agents.",
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        help=f"Path to the Engram SQLite database (default: {_DEFAULT_DB})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("mcp", help="Start the MCP server over stdio")

    eval_parser = subparsers.add_parser(
        "eval", help="Run retrieval replay evals against a gold set"
    )
    eval_parser.add_argument(
        "--gold",
        required=True,
        metavar="PATH",
        help="Path to a gold eval-case JSON file (spec §12.1)",
    )
    eval_parser.add_argument(
        "--run-name",
        metavar="NAME",
        help="Name for this eval run (default: eval-<UTC timestamp>)",
    )

    subparsers.add_parser("doctor", help="Health-check database and MCP wiring")

    args = parser.parse_args()

    if args.command == "mcp":
        cmd_mcp(args)
    elif args.command == "eval":
        cmd_eval(args)
    elif args.command == "doctor":
        cmd_doctor(args)


if __name__ == "__main__":
    main()
