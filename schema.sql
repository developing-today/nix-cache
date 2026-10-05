-- D1 system of record for the nix binary cache.
-- One row per uploaded store path (keyed by the narinfo hash).

CREATE TABLE IF NOT EXISTS artifacts (
  hash        TEXT PRIMARY KEY,  -- <hash> from <hash>.narinfo
  store_path  TEXT NOT NULL,
  url         TEXT NOT NULL,     -- R2 key of the NAR, e.g. nar/<sha>.nar.xz
  compression TEXT NOT NULL DEFAULT '',
  file_hash   TEXT,
  file_size   INTEGER,
  nar_hash    TEXT,
  nar_size    INTEGER,
  refs        TEXT NOT NULL DEFAULT '',
  deriver     TEXT NOT NULL DEFAULT '',
  ca          TEXT NOT NULL DEFAULT '',
  sig         TEXT NOT NULL DEFAULT '',
  created_at  INTEGER NOT NULL   -- unixepoch()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_path ON artifacts(store_path);
