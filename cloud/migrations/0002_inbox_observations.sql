-- Observations a remote agent extracted when it saved the memo (JSON array).
-- `memex cloud pull` writes them as the memo's .obs.jsonl sidecar.
ALTER TABLE inbox ADD COLUMN observations TEXT NOT NULL DEFAULT '[]';
