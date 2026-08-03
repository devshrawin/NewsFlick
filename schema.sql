-- newsdigest schema
--
-- Design rule: every stage must be re-runnable without redoing the one
-- before it. Raw text is stored once and never re-fetched. Re-clustering
-- at a new threshold touches only clusters/cluster_members. Re-summarising
-- touches only summaries. That's what makes the tuning loop 30 seconds
-- instead of 20 minutes.

-- NOTE: journal_mode deliberately left at the default (DELETE).
-- WAL is persistent and would leave committed rows sitting in news.db-wal,
-- which is gitignored -- i.e. silent data loss every time the DB is pushed.

CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    url         TEXT NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS articles (
    id            INTEGER PRIMARY KEY,
    source_id     INTEGER NOT NULL REFERENCES sources(id),
    url           TEXT NOT NULL UNIQUE,      -- dedup key: same URL never stored twice
    title         TEXT NOT NULL,
    feed_summary  TEXT,                      -- whatever the RSS gave us
    raw_text      TEXT,                      -- extracted body; NULL = extraction pending/failed
    extract_ok    INTEGER NOT NULL DEFAULT 0,
    extract_note  TEXT,
    published_at  TEXT,                      -- ISO8601 UTC, from feed
    fetched_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at);
CREATE INDEX IF NOT EXISTS idx_articles_pending   ON articles(extract_ok);

-- One row per article. Vector stored as raw float32 bytes; model+dims
-- recorded so a model swap doesn't silently mix incompatible vectors.
CREATE TABLE IF NOT EXISTS embeddings (
    article_id  INTEGER PRIMARY KEY REFERENCES articles(id),
    model       TEXT NOT NULL,
    dims        INTEGER NOT NULL,
    vector      BLOB NOT NULL,
    created_at  TEXT NOT NULL
);

-- A clustering RUN is versioned. Re-running with a new threshold creates
-- a new run rather than destroying the old one, so runs are comparable.
CREATE TABLE IF NOT EXISTS cluster_runs (
    id           INTEGER PRIMARY KEY,
    threshold    REAL NOT NULL,
    window_hours INTEGER NOT NULL,
    note         TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clusters (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES cluster_runs(id),
    label        TEXT NOT NULL,             -- human-facing id, e.g. C-0142
    lead_article INTEGER REFERENCES articles(id),
    size         INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clusters_run ON clusters(run_id);

CREATE TABLE IF NOT EXISTS cluster_members (
    cluster_id  INTEGER NOT NULL REFERENCES clusters(id),
    article_id  INTEGER NOT NULL REFERENCES articles(id),
    similarity  REAL,
    PRIMARY KEY (cluster_id, article_id)
);

CREATE TABLE IF NOT EXISTS summaries (
    id             INTEGER PRIMARY KEY,
    cluster_id     INTEGER NOT NULL REFERENCES clusters(id),
    prompt_version TEXT NOT NULL,           -- bump this whenever the prompt changes
    model          TEXT NOT NULL,
    headline       TEXT,
    body           TEXT NOT NULL,
    word_count     INTEGER,
    created_at     TEXT NOT NULL,
    UNIQUE (cluster_id, prompt_version)
);

-- The table that makes this an experiment instead of a hobby project.
-- target_type: 'cluster' | 'summary'
-- verdict for clusters: good | split | merged | junk
-- verdict for summaries: good | thin | wrong | unreadable
CREATE TABLE IF NOT EXISTS judgments (
    id          INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_ref  TEXT NOT NULL,              -- the C-0142 label
    verdict     TEXT NOT NULL,
    note        TEXT,
    judged_on   TEXT NOT NULL
);

-- Hand-labelled ground truth from day one. Articles sharing a group_key
-- are the same story. Used to tune the similarity threshold against
-- something real instead of by feel.
CREATE TABLE IF NOT EXISTS golden_set (
    article_url TEXT PRIMARY KEY,
    group_key   TEXT NOT NULL
);

-- Simple audit of what ran when.
CREATE TABLE IF NOT EXISTS runs (
    id         INTEGER PRIMARY KEY,
    stage      TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    ok         INTEGER,
    detail     TEXT
);
