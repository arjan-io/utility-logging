-- Lokale SQLite op de Pi. Dit bestand is geen MariaDB-migratie.
-- De applicatie zet synchronous=FULL op iedere SQLite-verbinding.
PRAGMA journal_mode = WAL;
PRAGMA synchronous = FULL;

CREATE TABLE water_outbox (
  event_id TEXT NOT NULL PRIMARY KEY,
  measured_at_utc TEXT NOT NULL,
  event_kind TEXT NOT NULL CHECK (event_kind IN ('pulse', 'heartbeat')),
  liters INTEGER NOT NULL CHECK (liters IN (0, 1)),
  delivered_at_utc TEXT NULL,
  CHECK (
    (event_kind = 'pulse' AND liters = 1)
    OR (event_kind = 'heartbeat' AND liters = 0)
  )
) WITHOUT ROWID;

CREATE INDEX ix_water_outbox_pending
  ON water_outbox (delivered_at_utc, measured_at_utc);

-- Lokale, persistente teller van geaccepteerde pulsen. Een trigger werkt
-- teller en outbox in dezelfde SQLite-transactie bij. De teller begint
-- bij de overdracht op 0; een eventuele fysieke meterbeginstand kan apart
-- worden vastgelegd zodra die is gecontroleerd.
CREATE TABLE water_counter (
  id INTEGER NOT NULL PRIMARY KEY CHECK (id = 1),
  pulses_total INTEGER NOT NULL DEFAULT 0 CHECK (pulses_total >= 0),
  updated_at_utc TEXT NULL
);

INSERT INTO water_counter (id, pulses_total) VALUES (1, 0);

CREATE TRIGGER tr_water_counter_after_pulse
AFTER INSERT ON water_outbox
WHEN NEW.event_kind = 'pulse'
BEGIN
  UPDATE water_counter
  SET pulses_total = pulses_total + NEW.liters,
      updated_at_utc = NEW.measured_at_utc
  WHERE id = 1;
END;
