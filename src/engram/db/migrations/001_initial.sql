-- Engram initial schema — all tables, FTS5, triggers, indexes.
-- Run once per database; tracked by the _migrations table in runner.py.
-- WAL mode is set here so it persists even if connection settings change.

PRAGMA journal_mode=WAL;

-- -------------------------------------------------------------------------
-- projects
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS projects (
    id         TEXT PRIMARY KEY,
    root_path  TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL,
    repo_url   TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- -------------------------------------------------------------------------
-- agent_sessions
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_sessions (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL REFERENCES projects(id),
    external_session_id TEXT NOT NULL,
    memory_thread_id    TEXT NOT NULL,
    agent               TEXT NOT NULL,
    branch              TEXT,
    git_sha             TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active', 'completed', 'failed')),
    started_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    ended_at            TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_project
    ON agent_sessions(project_id);

-- -------------------------------------------------------------------------
-- events  (append-only; gaps in seq are provable capture loss per ADR 0004)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    id                 TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects(id),
    session_id         TEXT NOT NULL REFERENCES agent_sessions(id),
    seq                INTEGER NOT NULL,
    source_type        TEXT NOT NULL
                           CHECK(source_type IN ('hook', 'mcp', 'cli', 'api', 'transcript')),
    source_seq         INTEGER,
    raw_ref_file       TEXT,
    raw_ref_offset     INTEGER,
    capture_confidence TEXT NOT NULL DEFAULT 'unknown'
                           CHECK(capture_confidence IN ('exact', 'likely', 'unknown')),
    event_type         TEXT NOT NULL,
    payload_json       TEXT NOT NULL DEFAULT '{}',
    content_hash       TEXT NOT NULL,
    occurred_at        TEXT NOT NULL,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE(session_id, seq)
);

-- (project_id, session_id) — used by consolidation to pull all session events
CREATE INDEX IF NOT EXISTS idx_events_project_session
    ON events(project_id, session_id);

-- session_id + seq — used for sequence reconciliation at session_end
CREATE INDEX IF NOT EXISTS idx_events_session_seq
    ON events(session_id, seq);

-- -------------------------------------------------------------------------
-- task_contexts  (short-term memory; TTL-based)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS task_contexts (
    id                   TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES projects(id),
    session_id           TEXT NOT NULL REFERENCES agent_sessions(id),
    task_key             TEXT NOT NULL,
    title                TEXT NOT NULL,
    content              TEXT NOT NULL,
    changed_files_json   TEXT NOT NULL DEFAULT '[]',
    next_steps_json      TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL DEFAULT 'active'
                             CHECK(status IN ('active', 'completed', 'expired')),
    ttl_until            TEXT,
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_task_contexts_project_session
    ON task_contexts(project_id, session_id);

CREATE INDEX IF NOT EXISTS idx_task_contexts_status
    ON task_contexts(status);

-- -------------------------------------------------------------------------
-- memories  (long-term memory)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memories (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id),
    scope                 TEXT NOT NULL CHECK(scope IN ('user', 'project', 'session')),
    type                  TEXT NOT NULL CHECK(type IN (
                              'preference', 'decision', 'project_fact',
                              'failure_pattern', 'command', 'constraint')),
    origin                TEXT NOT NULL CHECK(origin IN ('user', 'extracted', 'synthesized')),
    title                 TEXT NOT NULL,
    content               TEXT NOT NULL,
    content_hash          TEXT NOT NULL UNIQUE,  -- exact-match dedup; duplicate insert → already stored
    status                TEXT NOT NULL DEFAULT 'active'
                              CHECK(status IN ('active', 'stale', 'superseded', 'conflict', 'deleted')),
    confidence            REAL NOT NULL DEFAULT 1.0
                              CHECK(confidence >= 0.0 AND confidence <= 1.0),
    valid_from            TEXT,
    valid_until           TEXT,
    last_seen_at          TEXT,
    access_count          INTEGER NOT NULL DEFAULT 0,
    file_path             TEXT,
    file_hash             TEXT,
    supersedes_memory_id  TEXT REFERENCES memories(id),
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    metadata_json         TEXT NOT NULL DEFAULT '{}',
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_memories_project
    ON memories(project_id);

-- status — primary filter in every retrieval query
CREATE INDEX IF NOT EXISTS idx_memories_status
    ON memories(status);

CREATE INDEX IF NOT EXISTS idx_memories_project_status
    ON memories(project_id, status);

-- -------------------------------------------------------------------------
-- FTS5 virtual table for BM25 full-text search over memory title + content
--
-- Uses the "content table" feature so data is not duplicated on disk.
-- The three triggers below keep the FTS index in sync.
-- -------------------------------------------------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    title,
    content,
    content='memories',
    content_rowid='rowid'
);

-- After INSERT: index the new row
CREATE TRIGGER IF NOT EXISTS memories_ai
    AFTER INSERT ON memories
BEGIN
    INSERT INTO memories_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;

-- After UPDATE of indexed columns: remove old entry, add new one
CREATE TRIGGER IF NOT EXISTS memories_au
    AFTER UPDATE OF title, content ON memories
BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO memories_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;

-- After DELETE: remove from index
CREATE TRIGGER IF NOT EXISTS memories_ad
    AFTER DELETE ON memories
BEGIN
    INSERT INTO memories_fts(memories_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
END;

-- -------------------------------------------------------------------------
-- session_summaries
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_summaries (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id),
    session_id            TEXT NOT NULL REFERENCES agent_sessions(id),
    request               TEXT NOT NULL,
    completed             TEXT NOT NULL,
    learned               TEXT NOT NULL,
    next_steps            TEXT NOT NULL,
    files_read_json       TEXT NOT NULL DEFAULT '[]',
    files_modified_json   TEXT NOT NULL DEFAULT '[]',
    source_event_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- -------------------------------------------------------------------------
-- memory_sources  (provenance)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_sources (
    id               TEXT PRIMARY KEY,
    memory_id        TEXT NOT NULL REFERENCES memories(id),
    source_type      TEXT NOT NULL
                         CHECK(source_type IN ('event', 'session_summary', 'manual', 'import')),
    source_id        TEXT NOT NULL,
    quote_or_summary TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_sources_memory
    ON memory_sources(memory_id);

-- -------------------------------------------------------------------------
-- retrieval_traces
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retrieval_traces (
    id                       TEXT PRIMARY KEY,
    query                    TEXT NOT NULL,
    project_id               TEXT NOT NULL REFERENCES projects(id),
    selected_memory_ids_json  TEXT NOT NULL DEFAULT '[]',
    candidate_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    ranking_features_json    TEXT NOT NULL DEFAULT '{}',
    token_budget             INTEGER NOT NULL,
    injected_tokens          INTEGER NOT NULL DEFAULT 0,
    outcome_label            TEXT,
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- -------------------------------------------------------------------------
-- eval_cases
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_cases (
    id                       TEXT PRIMARY KEY,
    query                    TEXT NOT NULL,
    project_id               TEXT NOT NULL REFERENCES projects(id),
    expected_memory_ids_json TEXT NOT NULL DEFAULT '[]',
    expected_behavior        TEXT,
    tags_json                TEXT NOT NULL DEFAULT '[]',
    created_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- -------------------------------------------------------------------------
-- eval_runs
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS eval_runs (
    id                   TEXT PRIMARY KEY,
    run_name             TEXT NOT NULL,
    recall_at_5          REAL NOT NULL DEFAULT 0.0,
    mrr                  REAL NOT NULL DEFAULT 0.0,
    stale_injection_rate REAL NOT NULL DEFAULT 0.0,
    avg_injected_tokens  REAL NOT NULL DEFAULT 0.0,
    created_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
