-- GoodWe-dagstanden en vermogensmetingen blijven lokaal tot bevestiging.

CREATE TABLE pv_day_outbox (
  event_id TEXT NOT NULL PRIMARY KEY,
  measured_at_utc TEXT NOT NULL,
  usage_date TEXT NOT NULL,
  generated_kwh TEXT NULL,
  current_w INTEGER NULL,
  delivered_at_utc TEXT NULL
) WITHOUT ROWID;

CREATE INDEX ix_pv_day_outbox_pending
  ON pv_day_outbox (delivered_at_utc, measured_at_utc);
