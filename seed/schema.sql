-- ===========================================================================
-- IronAsk base schema.
--
-- The six operational tables are named and shaped exactly as the brief
-- specifies, because gold SQL is written against them.
--
-- Nothing in this file grants anything to an application role. Base tables are
-- reachable only by the owner; see views_and_roles.sql.
-- ===========================================================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------- operational

DROP TABLE IF EXISTS defects CASCADE;
DROP TABLE IF EXISTS downtime_events CASCADE;
DROP TABLE IF EXISTS work_orders CASCADE;
DROP TABLE IF EXISTS supplier_parts CASCADE;
DROP TABLE IF EXISTS machines CASCADE;
DROP TABLE IF EXISTS users CASCADE;

CREATE TABLE machines (
    machine_id      text PRIMARY KEY,
    line            text NOT NULL,
    type            text NOT NULL,
    model           text NOT NULL,
    commissioned_on date NOT NULL
);

CREATE TABLE work_orders (
    wo_id        text PRIMARY KEY,
    machine_id   text NOT NULL REFERENCES machines(machine_id),
    part_no      text NOT NULL,
    qty_planned  integer NOT NULL CHECK (qty_planned > 0),
    qty_good     integer NOT NULL CHECK (qty_good >= 0),
    started_at   timestamptz NOT NULL,
    ended_at     timestamptz
);

CREATE TABLE downtime_events (
    event_id    text PRIMARY KEY,
    machine_id  text NOT NULL REFERENCES machines(machine_id),
    start_at    timestamptz NOT NULL,
    end_at      timestamptz NOT NULL,
    category    text NOT NULL CHECK (category IN ('planned', 'breakdown', 'changeover', 'material')),
    reason_code text,
    alarm_code  text,
    CHECK (end_at >= start_at)
);

CREATE TABLE defects (
    defect_id   text PRIMARY KEY,
    wo_id       text NOT NULL REFERENCES work_orders(wo_id),
    part_no     text NOT NULL,
    defect_type text NOT NULL,
    qty         integer NOT NULL CHECK (qty > 0),
    found_at    timestamptz NOT NULL,
    stage       text NOT NULL
);

CREATE TABLE supplier_parts (
    part_no        text NOT NULL,
    supplier       text NOT NULL,
    unit_price_inr numeric(10, 2) NOT NULL,
    valid_from     date NOT NULL,
    PRIMARY KEY (part_no, supplier, valid_from)
);

-- The authority on who may see what. Never exposed to an application role;
-- the API reads it on the admin connection only.
CREATE TABLE users (
    user_id text PRIMARY KEY,
    name    text NOT NULL,
    role    text NOT NULL CHECK (role IN ('line_supervisor', 'quality_engineer', 'plant_head')),
    scope   text NOT NULL
);

CREATE INDEX ON work_orders (machine_id, started_at);
CREATE INDEX ON downtime_events (machine_id, start_at);
CREATE INDEX ON downtime_events (category);
CREATE INDEX ON downtime_events (alarm_code);
CREATE INDEX ON defects (wo_id);
CREATE INDEX ON defects (part_no, found_at);

-- ------------------------------------------------------------ document index
-- Dense + sparse retrieval live here. Access control for documents is applied
-- inside the retrieval queries (allowed_roles / scope), not by Postgres roles,
-- because no application role can reach this table at all.

DROP TABLE IF EXISTS doc_chunks CASCADE;
DROP TABLE IF EXISTS documents CASCADE;

CREATE TABLE documents (
    doc_id         text PRIMARY KEY,
    title          text NOT NULL,
    version        text,
    effective_date date,
    superseded     boolean NOT NULL DEFAULT false,
    superseded_on  date,
    allowed_roles  text[] NOT NULL,
    scope          text NOT NULL,
    source         text NOT NULL CHECK (source IN ('digital', 'scanned')),
    fmt            text NOT NULL,
    path           text NOT NULL,
    filler         boolean NOT NULL DEFAULT false
);

CREATE TABLE doc_chunks (
    chunk_id       text PRIMARY KEY,
    doc_id         text NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    title          text NOT NULL,
    section        text,
    page           integer,                -- printed page number, scanned docs
    version        text,
    effective_date date,
    superseded     boolean NOT NULL DEFAULT false,
    allowed_roles  text[] NOT NULL,
    scope          text NOT NULL,
    source         text NOT NULL,
    ord            integer NOT NULL,
    token_count    integer NOT NULL,
    text           text NOT NULL,
    -- instruction-like lines (e.g. a line starting "Assistant:") were stripped
    -- from `text`; this records that it happened so the flag is auditable.
    had_injection  boolean NOT NULL DEFAULT false,
    tsv            tsvector,
    embedding      vector(384)
);

CREATE INDEX doc_chunks_tsv_idx ON doc_chunks USING gin (tsv);
CREATE INDEX doc_chunks_roles_idx ON doc_chunks USING gin (allowed_roles);
CREATE INDEX doc_chunks_scope_idx ON doc_chunks (scope);
CREATE INDEX doc_chunks_doc_idx ON doc_chunks (doc_id);
CREATE INDEX doc_chunks_embedding_idx
    ON doc_chunks USING hnsw (embedding vector_cosine_ops);

-- Graph nodes/edges are mirrored here so the graph builder is reproducible and
-- so source_doc_ids can be access-filtered even if Neo4j is unavailable.
DROP TABLE IF EXISTS graph_edges CASCADE;
DROP TABLE IF EXISTS graph_nodes CASCADE;

CREATE TABLE graph_nodes (
    node_key       text PRIMARY KEY,   -- 'Label:Canonical Name'
    label          text NOT NULL,
    name           text NOT NULL,
    props          jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_doc_ids text[] NOT NULL DEFAULT '{}'
);

CREATE TABLE graph_edges (
    edge_id        bigserial PRIMARY KEY,
    src            text NOT NULL REFERENCES graph_nodes(node_key) ON DELETE CASCADE,
    rel            text NOT NULL,
    dst            text NOT NULL REFERENCES graph_nodes(node_key) ON DELETE CASCADE,
    props          jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_doc_ids text[] NOT NULL DEFAULT '{}',
    UNIQUE (src, rel, dst)
);
