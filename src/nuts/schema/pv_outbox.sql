-- GoodWe-metingen blijven lokaal bewaard tot MariaDB de event-ID bevestigt.
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE pv_outbox (
  event_id TEXT NOT NULL PRIMARY KEY,
  measured_at_utc TEXT NOT NULL,
  generated_total_kwh TEXT NOT NULL,
  current_w INTEGER NULL,
  delivered_at_utc TEXT NULL
) WITHOUT ROWID;

CREATE INDEX ix_pv_outbox_pending
  ON pv_outbox (delivered_at_utc, measured_at_utc);
