PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id                 TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    seq                INTEGER NOT NULL,
    role               TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content            TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'refused', 'error', 'cancelled', 'interrupted')),
    client_message_id  TEXT,
    parent_message_id  TEXT REFERENCES messages(id) ON DELETE CASCADE,
    attempt            INTEGER NOT NULL DEFAULT 1,
    error_code         TEXT,
    refusal_code       TEXT,
    provenance         TEXT,
    created_at         TEXT NOT NULL,
    completed_at       TEXT,
    UNIQUE (conversation_id, client_message_id),
    UNIQUE (conversation_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_messages_parent ON messages(parent_message_id) WHERE role = 'assistant';
CREATE INDEX IF NOT EXISTS ix_messages_conv_seq ON messages(conversation_id, seq);

CREATE TABLE IF NOT EXISTS citations (
    id             INTEGER PRIMARY KEY,
    message_id     TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    source_type    TEXT NOT NULL CHECK (source_type IN ('document', 'database')),
    doc_code       TEXT,
    doc_title      TEXT,
    doc_version    TEXT,
    doc_status     TEXT,
    section        TEXT,
    page           INTEGER,
    chunk_id       TEXT,
    snippet        TEXT,
    sql            TEXT,
    row_count      INTEGER,
    data_snapshot  TEXT,
    UNIQUE (message_id, ordinal)
);

CREATE TABLE IF NOT EXISTS message_metrics (
    message_id            TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    model                 TEXT,
    served_model          TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cost_usd              REAL NOT NULL DEFAULT 0,
    latency_ms            INTEGER NOT NULL DEFAULT 0,
    llm_calls             INTEGER NOT NULL DEFAULT 0,
    retries               INTEGER NOT NULL DEFAULT 0,
    tool_calls            INTEGER NOT NULL DEFAULT 0,
    pii_hits              INTEGER NOT NULL DEFAULT 0,
    provider_request_ids  TEXT
);
