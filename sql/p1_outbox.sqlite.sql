-- Lokale P1-buffer op de Pi. Bewaar dit SQLite-bestand op een persistent
-- hostvolume. Iedere verbinding zet synchronous=FULL. De event-ID wordt
-- pas na een bevestigde NAS-insert als afgeleverd gemarkeerd.
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE p1_outbox (
  event_id TEXT NOT NULL PRIMARY KEY,
  measured_at_utc TEXT NOT NULL,
  import_low_kwh TEXT NOT NULL,
  import_high_kwh TEXT NOT NULL,
  export_low_kwh TEXT NOT NULL,
  export_high_kwh TEXT NOT NULL,
  gas_m3 TEXT NULL,
  gas_measured_at_utc TEXT NULL,
  import_w INTEGER NOT NULL,
  export_w INTEGER NOT NULL,
  phase_l1_import_w INTEGER NULL,
  phase_l2_import_w INTEGER NULL,
  phase_l3_import_w INTEGER NULL,
  phase_l3_export_w INTEGER NULL,
  delivered_at_utc TEXT NULL
) WITHOUT ROWID;

CREATE INDEX ix_p1_outbox_pending
  ON p1_outbox (delivered_at_utc, measured_at_utc);
