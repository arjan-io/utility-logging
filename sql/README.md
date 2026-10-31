# Installatie en test van `nuts_v2`

De bestaande `nuts-*` databases blijven tijdens de bouw in gebruik. Test
de SQL eerst in een lege, aparte MariaDB-testomgeving. De nieuwe P1- en
waterlezers worden pas gestart wanneer de oude lezers hun apparaat hebben
vrijgegeven.

## Uitvoering

1. Voer [`001_schema_v2.sql`](001_schema_v2.sql) uit in een lege
   MariaDB-testomgeving. Dit bestand maakt `nuts_v2` en haar tabellen aan.
   Voer het niet opnieuw uit na een gedeeltelijke fout; onderzoek de fout.
2. Voer [`002_verify_schema_v2.sql`](002_verify_schema_v2.sql) uit. Verwacht
   11 tabellen, 8 foreign keys en aanvankelijk overal 0 rijen. Bewaar de
   resultaten en eventuele fouten voor de migratiecontrole. Test ook de
   nieuwe scripts met voorbeelddata in deze testomgeving.
3. Maak vlak vóór de productieomschakeling een **verse backup** van alle
   betrokken oude databases. Bewaar die buiten GitHub. Een oudere export
   is nuttig voor inspectie, maar is geen actuele omschakelbackup.
4. Voer de geteste schema-SQL eenmaal uit in de nieuwe productie-database
   op de NAS. Maak opnieuw een inventaris van de oude tabellen en importeer
   daarna de historie. Vergelijk aantallen en totalen.
5. Stop per apparaat de oude lezer en start de nieuwe lezer. Importeer
   daarna de laatste oude rijen nog eens met stabiele import-ID's, zodat
   de metingen die tijdens de hoofdimport zijn binnengekomen aansluiten.

## Nog niet uitvoeren

De historische importer is nog onvolledig. Voer hem niet op productie uit
tot hij in de testomgeving op echte voorbeeldgegevens en alle brontabellen
is gecontroleerd. Er mag nooit een tweede lezer tegelijk op de P1-poort of
GPIO17 draaien.

Alle meettijdstippen in `nuts_v2` zijn UTC. `DATE`-velden voor contracten,
dag- en maandtotalen volgen de kalender in `Europe/Amsterdam`. Een
`valid_until` is exclusief. Een onbekende hoeveelheid is `NULL` en geen 0.
