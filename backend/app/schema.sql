CREATE EXTENSION IF NOT EXISTS btree_gin;

CREATE TABLE IF NOT EXISTS imports (
  id uuid PRIMARY KEY,
  filename text NOT NULL,
  format text NOT NULL CHECK (format IN ('txt', 'json')),
  agency text NOT NULL DEFAULT '',
  sha256 text NOT NULL,
  path text NOT NULL,
  size_bytes bigint NOT NULL,
  status text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed', 'cancelled')),
  bytes_offset bigint NOT NULL DEFAULT 0,
  lines_read bigint NOT NULL DEFAULT 0,
  inserted bigint NOT NULL DEFAULT 0,
  duplicates bigint NOT NULL DEFAULT 0,
  errors bigint NOT NULL DEFAULT 0,
  rolled_back bigint NOT NULL DEFAULT 0,
  failure text,
  created_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS imports_sha_active ON imports (sha256) WHERE status IN ('running', 'done');

CREATE TABLE IF NOT EXISTS transactions (
  seq bigserial NOT NULL,
  id uuid PRIMARY KEY,
  type text NOT NULL CHECK (type IN ('CREDIT', 'DEBIT')),
  amount bigint NOT NULL CHECK (amount > 0),
  description varchar(255) NOT NULL DEFAULT '',
  agency text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz,
  deleted_at timestamptz,
  manual_edit boolean NOT NULL DEFAULT false,
  import_id uuid REFERENCES imports (id),
  source_line bigint,
  search tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, description)) STORED
);

CREATE INDEX IF NOT EXISTS tx_live_seq ON transactions (seq DESC) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS tx_deleted ON transactions (deleted_at) WHERE deleted_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS tx_import ON transactions (import_id, source_line) WHERE import_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS tx_search ON transactions USING gin (search, type, amount) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS import_errors (
  id bigserial PRIMARY KEY,
  import_id uuid NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
  line_no bigint NOT NULL,
  byte_start bigint NOT NULL,
  byte_end bigint NOT NULL,
  col int NOT NULL DEFAULT 0,
  content text NOT NULL,
  reason text NOT NULL
);

CREATE INDEX IF NOT EXISTS import_errors_import ON import_errors (import_id, id);

CREATE TABLE IF NOT EXISTS daily_balance (
  day date NOT NULL,
  agency text NOT NULL,
  net bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (day, agency)
);

CREATE TABLE IF NOT EXISTS bench_results (
  challenge int PRIMARY KEY,
  label text NOT NULL,
  duration_ms double precision,
  rows_ok bigint,
  errors_reported bigint,
  detail text,
  measured_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION tx_balance_insert() RETURNS trigger AS $$
BEGIN
  INSERT INTO daily_balance (day, agency, net)
  SELECT (created_at AT TIME ZONE 'UTC')::date AS d, agency AS a,
         SUM(CASE WHEN type = 'CREDIT' THEN amount ELSE -amount END)
  FROM new_rows
  WHERE deleted_at IS NULL
  GROUP BY 1, 2
  ORDER BY 1, 2
  ON CONFLICT (day, agency) DO UPDATE SET net = daily_balance.net + EXCLUDED.net;
  RETURN NULL;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tx_balance_update() RETURNS trigger AS $$
BEGIN
  INSERT INTO daily_balance (day, agency, net)
  SELECT d, a, SUM(s)
  FROM (
    SELECT (created_at AT TIME ZONE 'UTC')::date AS d, agency AS a,
           -(CASE WHEN type = 'CREDIT' THEN amount ELSE -amount END) AS s
    FROM old_rows WHERE deleted_at IS NULL
    UNION ALL
    SELECT (created_at AT TIME ZONE 'UTC')::date, agency,
           CASE WHEN type = 'CREDIT' THEN amount ELSE -amount END
    FROM new_rows WHERE deleted_at IS NULL
  ) x
  GROUP BY d, a
  ORDER BY d, a
  ON CONFLICT (day, agency) DO UPDATE SET net = daily_balance.net + EXCLUDED.net;
  RETURN NULL;
END
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tx_balance_delete() RETURNS trigger AS $$
BEGIN
  INSERT INTO daily_balance (day, agency, net)
  SELECT (created_at AT TIME ZONE 'UTC')::date AS d, agency AS a,
         -SUM(CASE WHEN type = 'CREDIT' THEN amount ELSE -amount END)
  FROM old_rows
  WHERE deleted_at IS NULL
  GROUP BY 1, 2
  ORDER BY 1, 2
  ON CONFLICT (day, agency) DO UPDATE SET net = daily_balance.net + EXCLUDED.net;
  RETURN NULL;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tx_balance_insert ON transactions;
CREATE TRIGGER tx_balance_insert AFTER INSERT ON transactions
  REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION tx_balance_insert();

DROP TRIGGER IF EXISTS tx_balance_update ON transactions;
CREATE TRIGGER tx_balance_update AFTER UPDATE ON transactions
  REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION tx_balance_update();

DROP TRIGGER IF EXISTS tx_balance_delete ON transactions;
CREATE TRIGGER tx_balance_delete AFTER DELETE ON transactions
  REFERENCING OLD TABLE AS old_rows FOR EACH STATEMENT EXECUTE FUNCTION tx_balance_delete();
