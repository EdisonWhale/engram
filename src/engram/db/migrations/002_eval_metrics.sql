-- Add the eval fields required by spec §12.1/§12.2 that were missing from the
-- frozen 001 schema: EvalCase.expected_memory_types / must_not_include_ids,
-- and EvalRun.conflict_injection_rate / abstain_rate (WS-D).

ALTER TABLE eval_cases ADD COLUMN expected_memory_types_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE eval_cases ADD COLUMN must_not_include_ids_json TEXT NOT NULL DEFAULT '[]';

ALTER TABLE eval_runs ADD COLUMN conflict_injection_rate REAL NOT NULL DEFAULT 0.0;
ALTER TABLE eval_runs ADD COLUMN abstain_rate REAL NOT NULL DEFAULT 0.0;
