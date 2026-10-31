-- Nieuw schema naast de bestaande nuts-* databases.
-- Test dit eerst in een lege, aparte MariaDB-testomgeving. De productie-
-- uitvoering op de NAS hoort bij de omschakeling, na een verse backup.
-- Het bestand wijzigt geen bestaande nuts-* tabellen.
-- Meter-tijdstippen zijn UTC DATETIME; contractdatums zijn lokale datums
-- in Europe/Amsterdam. Einddatums zijn exclusief.

CREATE DATABASE IF NOT EXISTS nuts_v2
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE nuts_v2;

CREATE TABLE p1_reading (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  event_id CHAR(36) CHARACTER SET ascii NOT NULL,
  source ENUM('collector', 'legacy_import') NOT NULL,
  measured_at_utc DATETIME(3) NOT NULL,
  received_at_utc DATETIME(3) NULL,
  import_low_kwh DECIMAL(14,3) NOT NULL,
  import_high_kwh DECIMAL(14,3) NOT NULL,
  export_low_kwh DECIMAL(14,3) NOT NULL,
  export_high_kwh DECIMAL(14,3) NOT NULL,
  gas_m3 DECIMAL(14,3) NULL,
  gas_measured_at_utc DATETIME(3) NULL,
  import_w INT UNSIGNED NOT NULL,
  export_w INT UNSIGNED NOT NULL,
  phase_l1_import_w INT UNSIGNED NULL,
  phase_l2_import_w INT UNSIGNED NULL,
  phase_l3_import_w INT UNSIGNED NULL,
  phase_l3_export_w INT UNSIGNED NULL,
  UNIQUE KEY uq_p1_event (event_id),
  KEY ix_p1_measured_at (measured_at_utc),
  CHECK (source = 'legacy_import' OR received_at_utc IS NOT NULL),
  CHECK (import_low_kwh >= 0 AND import_high_kwh >= 0),
  CHECK (export_low_kwh >= 0 AND export_high_kwh >= 0),
  CHECK (gas_m3 IS NULL OR gas_m3 >= 0)
) ENGINE=InnoDB;

-- Een event-ID wordt op de Pi gemaakt en eerst lokaal duurzaam bewaard.
-- Dezelfde puls kan na een NAS-storing veilig opnieuw worden aangeboden.
-- Nulmetingen blijven bewaard als tijdanker voor Grafana.
CREATE TABLE water_event (
  event_id CHAR(36) CHARACTER SET ascii NOT NULL PRIMARY KEY,
  source ENUM('collector', 'legacy_import') NOT NULL,
  measured_at_utc DATETIME(3) NOT NULL,
  received_at_utc DATETIME(3) NULL,
  event_kind ENUM('pulse', 'heartbeat') NOT NULL,
  liters DECIMAL(10,3) NOT NULL,
  CHECK (
    (event_kind = 'heartbeat' AND liters = 0)
    OR (event_kind = 'pulse' AND liters > 0)
  ),
  CHECK (source = 'legacy_import' OR received_at_utc IS NOT NULL),
  KEY ix_water_measured_at (measured_at_utc)
) ENGINE=InnoDB;

-- Optioneel: de P1-import/export blijft bruikbaar als de omvormer-API uitvalt.
CREATE TABLE pv_reading (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  event_id CHAR(36) CHARACTER SET ascii NOT NULL,
  source ENUM('collector', 'legacy_import') NOT NULL,
  measured_at_utc DATETIME(3) NOT NULL,
  received_at_utc DATETIME(3) NULL,
  generated_total_kwh DECIMAL(14,3) NOT NULL,
  current_w INT UNSIGNED NULL,
  UNIQUE KEY uq_pv_event (event_id),
  KEY ix_pv_measured_at (measured_at_utc),
  CHECK (source = 'legacy_import' OR received_at_utc IS NOT NULL),
  CHECK (generated_total_kwh >= 0)
) ENGINE=InnoDB;

CREATE TABLE contract (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  commodity ENUM('electricity', 'gas', 'water') NOT NULL,
  supplier VARCHAR(120) NOT NULL,
  description VARCHAR(200) NULL,
  valid_from DATE NOT NULL,
  valid_until DATE NULL,
  CHECK (valid_until IS NULL OR valid_until > valid_from),
  KEY ix_contract_dates (commodity, valid_from, valid_until),
  UNIQUE KEY uq_contract_id_commodity (id, commodity)
) ENGINE=InnoDB;

-- Een contract kan meerdere afrekenperioden hebben; de grens van 2027
-- wordt expliciet als nieuwe periode vastgelegd.
CREATE TABLE settlement_period (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  contract_id BIGINT UNSIGNED NOT NULL,
  starts_on DATE NOT NULL,
  ends_on DATE NOT NULL,
  electricity_policy ENUM('saldering', 'separate_import_export') NULL,
  CONSTRAINT fk_settlement_contract FOREIGN KEY (contract_id) REFERENCES contract(id),
  CHECK (ends_on > starts_on),
  UNIQUE KEY uq_settlement_start (contract_id, starts_on)
) ENGINE=InnoDB;

-- Herkomst en ritme van tarieven staan los van elkaar. Een leverancier
-- verwijst naar een contract; netbeheer en belasting hebben eigen bronnen.
CREATE TABLE tariff_source (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  commodity ENUM('electricity', 'gas', 'water') NOT NULL,
  source_kind ENUM('supplier', 'network_operator', 'government') NOT NULL,
  source_name VARCHAR(120) NOT NULL,
  contract_id BIGINT UNSIGNED NULL,
  -- De dubbele sleutel verhindert een gasbron bij een stroomcontract.
  CONSTRAINT fk_source_contract_commodity FOREIGN KEY (contract_id, commodity)
    REFERENCES contract(id, commodity),
  CHECK (
    (source_kind = 'supplier' AND contract_id IS NOT NULL)
    OR (source_kind <> 'supplier' AND contract_id IS NULL)
  ),
  KEY ix_source_kind (commodity, source_kind)
) ENGINE=InnoDB;

-- Positieve bedragen zijn kosten, negatieve bedragen zijn vergoedingen.
-- Vastrecht, energiebelasting, terugleverkosten en netbeheer zijn aparte
-- regels. Zo kan een kostenoverzicht netbeheer uitsluiten.
CREATE TABLE tariff_rule (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  tariff_source_id BIGINT UNSIGNED NOT NULL,
  code VARCHAR(64) NOT NULL,
  component ENUM(
    'supply', 'energy_tax', 'fixed_supply', 'tax_credit',
    'export_compensation', 'feedin_cost', 'network'
  ) NOT NULL,
  flow ENUM('import', 'export', 'none') NOT NULL DEFAULT 'none',
  register_code ENUM('low', 'high', 'all') NOT NULL DEFAULT 'all',
  basis ENUM('per_kwh', 'per_m3', 'per_day', 'per_month', 'per_year') NOT NULL,
  amount_eur DECIMAL(15,8) NOT NULL,
  includes_vat BOOLEAN NOT NULL,
  vat_rate DECIMAL(5,4) NOT NULL DEFAULT 0,
  valid_from DATE NOT NULL,
  valid_until DATE NULL,
  -- NULL betekent altijd van toepassing. Een band wordt bij de
  -- afrekening bepaald uit de totale hoeveelheid in de genoemde periode.
  band_quantity ENUM('settlement_export_kwh', 'settlement_import_kwh', 'calendar_gas_m3') NULL,
  band_from DECIMAL(14,3) NULL,
  band_until DECIMAL(14,3) NULL,
  status ENUM('draft', 'active', 'superseded') NOT NULL DEFAULT 'draft',
  revision INT UNSIGNED NOT NULL DEFAULT 1,
  supersedes_rule_id BIGINT UNSIGNED NULL,
  entered_at_utc DATETIME(3) NOT NULL,
  entered_by VARCHAR(80) NOT NULL,
  notes VARCHAR(255) NULL,
  CONSTRAINT fk_tariff_source FOREIGN KEY (tariff_source_id) REFERENCES tariff_source(id),
  CONSTRAINT fk_tariff_previous FOREIGN KEY (supersedes_rule_id) REFERENCES tariff_rule(id),
  CHECK (valid_until IS NULL OR valid_until > valid_from),
  CHECK (vat_rate >= 0 AND vat_rate <= 1),
  CHECK (revision > 0),
  CHECK (band_from IS NULL OR band_from >= 0),
  CHECK (band_until IS NULL OR band_until > band_from),
  CHECK (
    (band_quantity IS NULL AND band_from IS NULL AND band_until IS NULL)
    OR (band_quantity IS NOT NULL AND band_from IS NOT NULL)
  ),
  KEY ix_tariff_dates (tariff_source_id, valid_from, valid_until),
  UNIQUE KEY uq_tariff_version (tariff_source_id, code, valid_from, revision)
) ENGINE=InnoDB;

-- Herberekenbaar dagtotaal, met Amsterdamse kalenderdagen. NULL betekent
-- onbekend; 0 is alleen toegestaan als nulverbruik werkelijk is vastgesteld.
-- Status staat per nutssoort zodat een waterstoring geen geldige P1-dag
-- ongeldig maakt. Een Pi-uitval over een daggrens verdeelt de P1-delta
-- niet automatisch over de dagen.
CREATE TABLE daily_usage (
  usage_date DATE NOT NULL PRIMARY KEY,
  import_low_kwh DECIMAL(14,3) NULL,
  import_high_kwh DECIMAL(14,3) NULL,
  export_low_kwh DECIMAL(14,3) NULL,
  export_high_kwh DECIMAL(14,3) NULL,
  gas_m3 DECIMAL(14,3) NULL,
  water_m3 DECIMAL(14,3) NULL,
  pv_generated_kwh DECIMAL(14,3) NULL,
  electricity_quality ENUM('complete', 'estimated', 'unknown', 'not_collected') NOT NULL,
  gas_quality ENUM('complete', 'estimated', 'unknown', 'not_collected') NOT NULL,
  water_quality ENUM('complete', 'estimated', 'unknown', 'not_collected') NOT NULL,
  pv_quality ENUM('complete', 'estimated', 'unknown', 'not_collected') NOT NULL,
  electricity_source ENUM('meter_derived', 'legacy_import', 'not_available') NOT NULL,
  gas_source ENUM('meter_derived', 'legacy_import', 'not_available') NOT NULL,
  water_source ENUM('meter_derived', 'legacy_import', 'not_available') NOT NULL,
  pv_source ENUM('meter_derived', 'legacy_import', 'not_available') NOT NULL,
  calculated_at_utc DATETIME(3) NOT NULL,
  CHECK (COALESCE(import_low_kwh, 0) >= 0 AND COALESCE(import_high_kwh, 0) >= 0),
  CHECK (COALESCE(export_low_kwh, 0) >= 0 AND COALESCE(export_high_kwh, 0) >= 0),
  CHECK (COALESCE(gas_m3, 0) >= 0 AND COALESCE(water_m3, 0) >= 0),
  CHECK (COALESCE(pv_generated_kwh, 0) >= 0)
) ENGINE=InnoDB;

-- Maandtotalen zijn een volwaardige historische laag. Nieuwe maanden worden
-- uit daily_usage opgebouwd wanneer alle dagen bekend zijn. Bij een P1-kloof
-- die binnen een maand blijft, kan een totale maanddelta soms rechtstreeks
-- uit de omliggende cumulatieve standen worden bepaald (boundary_delta),
-- ook als de afzonderlijke dagen onbekend blijven. Een kloof over de
-- maandgrens krijgt geen schijnnauwkeurig maandtotaal. Oude maanden kunnen
-- als historisch aggregaat worden geimporteerd.
-- usage_month is altijd de eerste dag van de lokale kalendermaand.
CREATE TABLE monthly_usage (
  usage_month DATE NOT NULL PRIMARY KEY,
  import_low_kwh DECIMAL(14,3) NULL,
  import_high_kwh DECIMAL(14,3) NULL,
  export_low_kwh DECIMAL(14,3) NULL,
  export_high_kwh DECIMAL(14,3) NULL,
  gas_m3 DECIMAL(14,3) NULL,
  water_m3 DECIMAL(14,3) NULL,
  pv_generated_kwh DECIMAL(14,3) NULL,
  electricity_quality ENUM('complete', 'estimated', 'unknown', 'not_collected') NOT NULL,
  gas_quality ENUM('complete', 'estimated', 'unknown', 'not_collected') NOT NULL,
  water_quality ENUM('complete', 'estimated', 'unknown', 'not_collected') NOT NULL,
  pv_quality ENUM('complete', 'estimated', 'unknown', 'not_collected') NOT NULL,
  electricity_source ENUM('daily_sum', 'boundary_delta', 'legacy_import', 'not_available') NOT NULL,
  gas_source ENUM('daily_sum', 'boundary_delta', 'legacy_import', 'not_available') NOT NULL,
  water_source ENUM('daily_sum', 'boundary_delta', 'legacy_import', 'not_available') NOT NULL,
  pv_source ENUM('daily_sum', 'boundary_delta', 'legacy_import', 'not_available') NOT NULL,
  calculated_at_utc DATETIME(3) NOT NULL,
  CHECK (DAYOFMONTH(usage_month) = 1),
  CHECK (COALESCE(import_low_kwh, 0) >= 0 AND COALESCE(import_high_kwh, 0) >= 0),
  CHECK (COALESCE(export_low_kwh, 0) >= 0 AND COALESCE(export_high_kwh, 0) >= 0),
  CHECK (COALESCE(gas_m3, 0) >= 0 AND COALESCE(water_m3, 0) >= 0),
  CHECK (COALESCE(pv_generated_kwh, 0) >= 0)
) ENGINE=InnoDB;

-- Afgeleide kosten, uit te splitsen per component. Schattingen kunnen
-- opnieuw worden berekend na een tariefwijziging of jaarafrekening.
CREATE TABLE daily_cost_line (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cost_date DATE NOT NULL,
  tariff_rule_id BIGINT UNSIGNED NULL,
  commodity ENUM('electricity', 'gas', 'water') NOT NULL,
  component ENUM(
    'supply', 'energy_tax', 'fixed_supply', 'tax_credit',
    'export_compensation', 'feedin_cost', 'network', 'settlement_adjustment'
  ) NOT NULL,
  line_key VARCHAR(128) NOT NULL,
  quantity DECIMAL(15,6) NULL,
  rate_basis ENUM('per_kwh', 'per_m3', 'per_day', 'per_month', 'per_year') NULL,
  rate_amount_eur DECIMAL(15,8) NULL,
  amount_eur DECIMAL(15,6) NOT NULL,
  status ENUM('estimate', 'final') NOT NULL DEFAULT 'estimate',
  note VARCHAR(255) NULL,
  calculated_at_utc DATETIME(3) NOT NULL,
  CONSTRAINT fk_cost_day FOREIGN KEY (cost_date) REFERENCES daily_usage(usage_date),
  CONSTRAINT fk_cost_tariff FOREIGN KEY (tariff_rule_id) REFERENCES tariff_rule(id),
  UNIQUE KEY uq_daily_cost (cost_date, commodity, line_key),
  KEY ix_cost_date (cost_date, commodity, component)
) ENGINE=InnoDB;

-- Ook oude maanden zonder daggegevens kunnen in hetzelfde nieuwe schema
-- kosten krijgen. Voor nieuwe maanden worden deze regels uit dagkosten
-- opgebouwd; voor historische maanden mag een schatting op maandniveau
-- worden opgeslagen. Verdeel een historische maand nooit kunstmatig over
-- dagen. Een tariefwissel midden in een historische maand kan de precieze
-- kosten onbekend maken.
CREATE TABLE monthly_cost_line (
  id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
  cost_month DATE NOT NULL,
  tariff_rule_id BIGINT UNSIGNED NULL,
  commodity ENUM('electricity', 'gas', 'water') NOT NULL,
  component ENUM(
    'supply', 'energy_tax', 'fixed_supply', 'tax_credit',
    'export_compensation', 'feedin_cost', 'network', 'settlement_adjustment'
  ) NOT NULL,
  line_key VARCHAR(128) NOT NULL,
  quantity DECIMAL(15,6) NULL,
  rate_basis ENUM('per_kwh', 'per_m3', 'per_day', 'per_month', 'per_year') NULL,
  rate_amount_eur DECIMAL(15,8) NULL,
  amount_eur DECIMAL(15,6) NULL,
  quality ENUM('complete', 'estimated', 'unknown') NOT NULL,
  source ENUM('daily_sum', 'monthly_estimate', 'legacy_import') NOT NULL,
  status ENUM('estimate', 'final') NOT NULL DEFAULT 'estimate',
  note VARCHAR(255) NULL,
  calculated_at_utc DATETIME(3) NOT NULL,
  CONSTRAINT fk_month_cost_month FOREIGN KEY (cost_month) REFERENCES monthly_usage(usage_month),
  CONSTRAINT fk_month_cost_tariff FOREIGN KEY (tariff_rule_id) REFERENCES tariff_rule(id),
  UNIQUE KEY uq_monthly_cost (cost_month, commodity, line_key),
  KEY ix_month_cost_date (cost_month, commodity, component),
  CHECK (DAYOFMONTH(cost_month) = 1)
) ENGINE=InnoDB;
