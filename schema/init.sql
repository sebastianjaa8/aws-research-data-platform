-- Research Data Platform — PostgreSQL schema
-- Requires: pgvector extension (CREATE EXTENSION vector)
--
-- Security model:
--   - app.researcher_id session variable set by application after JWT validation
--   - RLS policies use this variable to restrict all queries automatically
--   - No application-level WHERE clauses needed for data isolation

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- for future fuzzy matching

-- ─── Researchers ─────────────────────────────────────────────────────────────

CREATE TABLE researchers (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    auth0_sub  TEXT UNIQUE NOT NULL,    -- matches JWT sub claim
    email      TEXT NOT NULL,
    name       TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ─── Experiment chunks (searchable text + embeddings) ─────────────────────────

CREATE TABLE experiment_chunks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    researcher_id UUID REFERENCES researchers(id) ON DELETE CASCADE,
    instrument_id TEXT        NOT NULL,
    session_id    TEXT        NOT NULL,
    timestamp_utc FLOAT       NOT NULL,
    content       TEXT        NOT NULL,   -- human-readable summary of the record
    embedding     vector(1536),           -- Titan Embeddings V2 dimension
    metadata      JSONB,
    created_at    TIMESTAMPTZ DEFAULT now(),
    updated_at    TIMESTAMPTZ DEFAULT now(),

    CONSTRAINT uq_instrument_session UNIQUE (instrument_id, session_id)
);

-- Indexes for fast retrieval
CREATE INDEX idx_chunks_embedding   ON experiment_chunks USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX idx_chunks_fulltext    ON experiment_chunks USING gin (to_tsvector('english', content));
CREATE INDEX idx_chunks_researcher  ON experiment_chunks (researcher_id);
CREATE INDEX idx_chunks_session     ON experiment_chunks (session_id);

-- ─── Row-Level Security ───────────────────────────────────────────────────────
-- Researchers can only see their own data.
-- The app sets app.researcher_id after validating the Auth0 JWT.

ALTER TABLE experiment_chunks ENABLE ROW LEVEL SECURITY;

-- Policy: SELECT/INSERT/UPDATE/DELETE restricted to rows owned by the current researcher
CREATE POLICY researcher_owns_chunks ON experiment_chunks
    USING (
        researcher_id = (
            SELECT id FROM researchers
            WHERE  auth0_sub = current_setting('app.researcher_id', true)
        )
        AND current_setting('app.researcher_id', true) IS NOT NULL
    )
    WITH CHECK (
        researcher_id = (
            SELECT id FROM researchers
            WHERE  auth0_sub = current_setting('app.researcher_id', true)
        )
        AND current_setting('app.researcher_id', true) IS NOT NULL
    );

-- Application role — used by the API, does NOT bypass RLS
-- Password provisioned out-of-band via Secrets Manager after role creation:
--   ALTER ROLE platform_app PASSWORD '<value-from-secrets-manager>';
-- Never commit a password literal in DDL, even a placeholder.
CREATE ROLE platform_app LOGIN;
GRANT SELECT, INSERT, UPDATE ON experiment_chunks TO platform_app;
GRANT SELECT ON researchers TO platform_app;
GRANT USAGE ON SCHEMA public TO platform_app;

-- ─── Audit log ───────────────────────────────────────────────────────────────

CREATE TABLE query_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    researcher_id UUID REFERENCES researchers(id),
    question      TEXT,
    chunks_used   INT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

ALTER TABLE query_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY researcher_log ON query_log USING (
    researcher_id = (
        SELECT id FROM researchers
        WHERE  auth0_sub = current_setting('app.researcher_id', true)
    )
);
GRANT SELECT, INSERT ON query_log TO platform_app;
