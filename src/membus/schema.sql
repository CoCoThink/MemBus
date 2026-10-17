PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta(key, value)
VALUES ('schema_version', '2');

CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,

    memory_type TEXT NOT NULL,
    scope TEXT NOT NULL,

    workspace TEXT,
    project TEXT,
    repo TEXT,
    branch TEXT,

    source_type TEXT,
    source_ref TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    actor TEXT,
    created_at TEXT NOT NULL,

    FOREIGN KEY(memory_id) REFERENCES memories(id)
);

CREATE TABLE IF NOT EXISTS retrieval_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT,
    scope_json TEXT,
    candidate_count INTEGER NOT NULL,
    result_ids_json TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    created_at TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    memory_id UNINDEXED,
    title,
    content,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS memories_ai
AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(memory_id, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_au
AFTER UPDATE OF title, content ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
    INSERT INTO memory_fts(memory_id, title, content)
    VALUES (new.id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad
AFTER DELETE ON memories BEGIN
    DELETE FROM memory_fts WHERE memory_id = old.id;
END;

CREATE INDEX IF NOT EXISTS idx_events_memory_id
    ON memory_events(memory_id);
