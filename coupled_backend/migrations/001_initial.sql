-- 001_initial.sql: Lakebase Postgres schema for coupled-backend
-- Run via direct connection (DATABASE_URL_UNPOOLED) on startup.

CREATE TABLE IF NOT EXISTS parts (
    mpn TEXT PRIMARY KEY,
    lcsc_id TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    package TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL DEFAULT '',
    price DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    stock INTEGER NOT NULL DEFAULT 0,
    voltage_max DOUBLE PRECISION,
    footprint_ref TEXT NOT NULL DEFAULT '',
    pins JSONB NOT NULL DEFAULT '[]'::jsonb,
    emb BYTEA NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_parts_mpn_lower ON parts (LOWER(mpn));
CREATE INDEX IF NOT EXISTS idx_parts_stock ON parts (stock);
CREATE INDEX IF NOT EXISTS idx_parts_category ON parts (category);
CREATE INDEX IF NOT EXISTS idx_parts_package ON parts (package);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    owner_user_id UUID,
    board_name TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    component_count INTEGER NOT NULL DEFAULT 0,
    has_board BOOLEAN NOT NULL DEFAULT FALSE,
    has_schematic BOOLEAN NOT NULL DEFAULT FALSE,
    drc_passed BOOLEAN,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DO $$
BEGIN
    IF to_regclass('neon_auth."user"') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1 FROM pg_constraint WHERE conname = 'projects_owner_user_id_fkey'
       ) THEN
        ALTER TABLE projects
            ADD CONSTRAINT projects_owner_user_id_fkey
            FOREIGN KEY (owner_user_id)
            REFERENCES neon_auth."user"(id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_projects_owner_updated ON projects (owner_user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS project_files (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, name)
);
