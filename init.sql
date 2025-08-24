-- Создаём роль и базу
DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN
      CREATE ROLE app WITH LOGIN PASSWORD 'app' NOSUPERUSER NOCREATEDB NOCREATEROLE;
   END IF;
END
$$;

DO $$
BEGIN
   IF NOT EXISTS (SELECT FROM pg_database WHERE datname = 'bridge') THEN
      CREATE DATABASE bridge OWNER app;
   END IF;
END
$$;

\connect bridge

BEGIN;

-- Users
CREATE TABLE IF NOT EXISTS users (
  id           BIGSERIAL PRIMARY KEY,
  tg_user_id   BIGINT UNIQUE,
  first_name   TEXT,
  last_name    TEXT,
  display_name TEXT,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Tournaments
CREATE TABLE IF NOT EXISTS tournaments (
  id         BIGSERIAL PRIMARY KEY,
  name       TEXT NOT NULL,
  config     JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Results
CREATE TABLE IF NOT EXISTS results (
  id                 BIGSERIAL PRIMARY KEY,
  tournament_id      BIGINT NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  round_number       INT,
  table_number       INT,
  board_number       INT,
  pair_ns_number     INT,
  pair_ew_number     INT,
  contract           TEXT,
  declarer           TEXT,
  result_tricks      INT,
  score_ns           INT,
  entered_by_user_id BIGINT REFERENCES users(id) ON DELETE RESTRICT,
  entered_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tournament_id, board_number, table_number)
);

-- Индексы results
CREATE INDEX IF NOT EXISTS idx_results_by_tournament_board
  ON results (tournament_id, board_number);

CREATE INDEX IF NOT EXISTS idx_results_by_tournament_round_table
  ON results (tournament_id, round_number, table_number);

CREATE INDEX IF NOT EXISTS idx_results_pairs_ns
  ON results (tournament_id, pair_ns_number);

CREATE INDEX IF NOT EXISTS idx_results_pairs_ew
  ON results (tournament_id, pair_ew_number);

-- Audit (история правок)
CREATE TABLE IF NOT EXISTS result_audit (
  id                BIGSERIAL PRIMARY KEY,
  tournament_id     BIGINT NOT NULL REFERENCES tournaments(id) ON DELETE CASCADE,
  result_id         BIGINT NOT NULL REFERENCES results(id)     ON DELETE CASCADE,
  edited_by_user_id BIGINT NOT NULL REFERENCES users(id)       ON DELETE RESTRICT,
  edited_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  old_payload       JSONB,
  new_payload       JSONB
);

CREATE INDEX IF NOT EXISTS idx_result_audit_by_tournament
  ON result_audit (tournament_id, edited_at DESC);

COMMIT;
