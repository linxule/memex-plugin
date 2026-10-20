-- Observation ids are now scoped to their document (o:<path hash>:<content hash>).
-- Forgetting every set's hash makes the next `memex cloud push` re-send all
-- observation sets; replacing a set deletes its old-format rows and vectors.
DELETE FROM obs_sets;
