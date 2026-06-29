#!/usr/bin/env bash
# PostToolUse hook: auto format + lint-fix an edited Python file.
# No-op (exit 0) until ruff is installed, so it is harmless before WS-0 scaffolds the toolchain.
# Receives the tool-call payload as JSON on stdin.
set -euo pipefail

input="$(cat)"
file="$(printf '%s' "$input" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null || true)"

[ -z "$file" ] && exit 0
case "$file" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$file" ] || exit 0
command -v ruff >/dev/null 2>&1 || exit 0

ruff format "$file" >/dev/null 2>&1 || true
ruff check --fix "$file" >/dev/null 2>&1 || true
exit 0
