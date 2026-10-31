-- Alleen-lezen controle nadat 001_schema_v2.sql op de NAS is uitgevoerd.
-- Verwacht 11 tabellen en 8 foreign keys. Geen bestaande nuts-* database
-- wordt door deze controles gewijzigd.

SELECT VERSION() AS mariadb_version;

SELECT COUNT(*) AS table_count, 11 AS expected_table_count
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'nuts_v2' AND TABLE_TYPE = 'BASE TABLE';

SELECT TABLE_NAME
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'nuts_v2' AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_NAME;

SELECT COUNT(*) AS foreign_key_count, 8 AS expected_foreign_key_count
FROM information_schema.TABLE_CONSTRAINTS
WHERE TABLE_SCHEMA = 'nuts_v2' AND CONSTRAINT_TYPE = 'FOREIGN KEY';

SELECT TABLE_NAME, CONSTRAINT_NAME
FROM information_schema.TABLE_CONSTRAINTS
WHERE TABLE_SCHEMA = 'nuts_v2' AND CONSTRAINT_TYPE = 'FOREIGN KEY'
ORDER BY TABLE_NAME, CONSTRAINT_NAME;

SELECT TABLE_NAME, row_count
FROM (
  SELECT 'p1_reading' AS TABLE_NAME, COUNT(*) AS row_count FROM nuts_v2.p1_reading
  UNION ALL SELECT 'water_event', COUNT(*) FROM nuts_v2.water_event
  UNION ALL SELECT 'pv_reading', COUNT(*) FROM nuts_v2.pv_reading
  UNION ALL SELECT 'contract', COUNT(*) FROM nuts_v2.contract
  UNION ALL SELECT 'settlement_period', COUNT(*) FROM nuts_v2.settlement_period
  UNION ALL SELECT 'tariff_source', COUNT(*) FROM nuts_v2.tariff_source
  UNION ALL SELECT 'tariff_rule', COUNT(*) FROM nuts_v2.tariff_rule
  UNION ALL SELECT 'daily_usage', COUNT(*) FROM nuts_v2.daily_usage
  UNION ALL SELECT 'monthly_usage', COUNT(*) FROM nuts_v2.monthly_usage
  UNION ALL SELECT 'daily_cost_line', COUNT(*) FROM nuts_v2.daily_cost_line
  UNION ALL SELECT 'monthly_cost_line', COUNT(*) FROM nuts_v2.monthly_cost_line
) AS counts
ORDER BY TABLE_NAME;
