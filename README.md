# Utility logging

Nieuwe registratie voor elektriciteit, gas en water op een Raspberry Pi,
met MariaDB en MQTT op een aparte server. Deze repository bevat het
nieuwe databaseschema, lokale meetbuffers en de eerste meet- en
synchronisatiecode.

## Stand van het project

De code is **in ontwikkeling**. De nieuwe P1- en GPIO-processen mogen
pas worden gestart nadat de bestaande lezers hun apparaat hebben
vrijgegeven. De SQL is nog niet op de productie-MariaDB uitgevoerd.
De historische import, kostenberekening, MQTT-publicatie, webpagina en
Docker-opzet zijn nog niet volledig getest of af.

## SQL voor phpMyAdmin

De databasewijzigingen zijn losse bestanden. Voer ze zelf uit, in deze
volgorde:

1. [`sql/001_schema_v2.sql`](sql/001_schema_v2.sql) maakt de aparte
   database `nuts_v2` en haar tabellen aan. Test eerst op een **lege,
   afzonderlijke MariaDB-testinstallatie**.
2. [`sql/002_verify_schema_v2.sql`](sql/002_verify_schema_v2.sql) is
   alleen-lezen en controleert de tabellen, relaties en aantallen.

Deze scripts raken de oude databases niet. De productie-uitvoering en
historische import horen pas bij de definitieve omschakeling, na een
verse backup. Zie [`sql/README.md`](sql/README.md) voor de volgorde.

## Lokale proef zonder Pi of server

De simulatie gebruikt alleen tijdelijke SQLite-bestanden en een nepserver:

```sh
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m nuts.simulate
PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

De P1-parser bewaart kWh-standen met drie decimalen. Waterpulsen en
nulmetingen worden eerst lokaal opgeslagen; een synchronisatiewerker
stuurt events later met vaste event-ID's naar MariaDB. De lokale proef
dekt onder meer herhalen na een serverstoring en meetgaten over een
daggrens. Hardware, MariaDB en Docker vereisen aparte tests.

## Configuratie

Gebruik [`.env.example`](.env.example) als sjabloon. Zet ingevulde
wachtwoorden, SQLite-bestanden, exports en backups nooit in GitHub.
