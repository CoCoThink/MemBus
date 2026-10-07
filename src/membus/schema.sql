PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_meta(key, value)
VALUES ('schema_version', '1');

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

    status TEXT NOT NULL DEFAULT 'active',

    importance REAL NOT NULL DEFAULT 0.5,
    confidence REAL NOT NULL DEFAULT 1.0,
    trust REAL NOT NULL DEFAULT 0.5,

    source_type TEXT,
    source_ref TEXT,

    valid_from TEXT,
    valid_until TEXT,
    supersedes_id TEXT,

    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,

    CHECK (importance >= 0.0 AND importance <= 1.0),
    CHECK (confidence >= 0.0 AND confidence <= 1.0),
    CHECK (trust >= 0.0 AND trust <= 1.0),

    FOREIGN KEY (supersedes_id) REFERENCES memories(id)
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
    debug_json TEXT,
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

CREATE INDEX IF NOT EXISTS idx_memories_status
    ON memories(status);
CREATE INDEX IF NOT EXISTS idx_memories_scope
    ON memories(scope);
CREATE INDEX IF NOT EXISTS idx_memories_workspace
    ON memories(workspace);
CREATE INDEX IF NOT EXISTS idx_memories_project
    ON memories(project);
CREATE INDEX IF NOT EXISTS idx_memories_repo
    ON memories(repo);
CREATE INDEX IF NOT EXISTS idx_memories_branch
    ON memories(branch);
CREATE INDEX IF NOT EXISTS idx_memories_updated_at
    ON memories(updated_at);
CREATE INDEX IF NOT EXISTS idx_events_memory_id
    ON memory_events(memory_id);
CREATE INDEX IF NOT EXISTS idx_retrieval_logs_created_at
    ON retrieval_logs(created_at);
