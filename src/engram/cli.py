"""Engram CLI entry point.

Subcommands:
  engram mcp      — start the MCP server over stdio (production use)
  engram eval     — run retrieval evals (stub; implemented by WS-D)
  engram doctor   — health-check: database, migrations, MCP wiring
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_DEFAULT_DB = Path.home() / ".engram" / "engram.db"


def cmd_mcp(args: argparse.Namespace) -> None:
    """Launch the MCP server over stdio.  Blocks until stdin is closed."""
    from engram.mcp.server import mcp

    mcp.run()


def cmd_eval(args: argparse.Namespace) -> None:
    """Run replay evals and print Recall@5 / MRR / stale_injection_rate.

    Not yet implemented — WS-D provides the body.
    """
    print("engram eval: not yet implemented (WS-D)")
    sys.exit(0)


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

    subparsers.add_parser("eval", help="Run retrieval evals (not yet implemented)")

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
