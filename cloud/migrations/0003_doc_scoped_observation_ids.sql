-- Observation ids are now scoped to their document (o:<path hash>:<content hash>).
-- Marking every set's hash stale makes the next `memex cloud push` re-send all
-- observation sets: replacing a set deletes its old-format rows and vectors,
-- and a document whose observations were all removed locally gets an empty set
-- (the CLI sends one for any listed set it no longer has).
UPDATE obs_sets SET set_hash = 'legacy-ids';
