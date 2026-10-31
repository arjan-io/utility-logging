# Utility logging

Docker-diensten voor elektriciteit, gas en water op een Raspberry Pi.
MariaDB en de MQTT-broker draaien elders. Dit project bevat uitsluitend
de uitvoerbare diensten, hun containerconfiguratie en dit installatieblad.
SQL voor het aanmaken van de MariaDB-tabellen en de eenmalige migratie wordt
apart aangeleverd en door de beheerder zelf uitgevoerd.

## Wat draait er?

| Dienst | Taak |
| --- | --- |
| `p1` | Leest één P1-seriële poort en bewaart elk geldig telegram eerst in lokale SQLite. |
| `water` | Telt geaccepteerde pulsen op GPIO17 direct in lokale SQLite en schrijft nulmetingen. |
| `p1-sync` | Stuurt alleen P1-events naar MariaDB en herhaalt veilig na een storing. |
| `water-sync` | Stuurt alleen waterevents naar MariaDB en herhaalt veilig na een storing. |
| `aggregate` | Berekent gesloten dagen en maanden voor grafieken, met onbekende waarden bij meetgaten. |
| `publisher` | Publiceert actueel vermogen, waterdebiet en variabele kosten naar MQTT. |

De oude MQTT-velden blijven beschikbaar. Met `MQTT_PREFIX=sensors/v2`
verschijnen ze eerst onder proefonderwerpen. Pas bij de definitieve
omschakeling wordt de prefix `sensors` gebruikt. De kosten zijn momenteel
**variabele afnamekosten**. Vaste leveringskosten, netbeheer, saldering en
teruglevering vragen nog een afzonderlijke afrekenberekening; deze versie
claimt daarvoor geen factuurbedrag.

## Voorbereiden op de Pi

1. Maak eerst de aparte MariaDB-database `nuts_v2` met de los aangeleverde
   phpMyAdmin-SQL. Zet de benodigde tariefregels in die database. De oude
   databases blijven bestaan.
2. Haal deze repository op de Pi op met `git clone`; `git init` is niet
   nodig. Vul een eigen `.env` in met [`.env.example`](.env.example) als
   voorbeeld. Bewaar `.env` en de map `data/` buiten Git. Zet `DATA_DIR`
   op een blijvende locatie op de Pi.
3. Bouw de image met `docker compose build`. Controleer de lokale
   configuratie met `docker compose config --quiet`.
4. Start de twee onafhankelijke synchronisatoren met
   `docker compose up -d p1-sync water-sync`. Start voor een MQTT-proef
   `docker compose --profile publish up -d publisher`; laat de prefix op
   `sensors/v2` staan.
5. Stop **eerst** de oude P1-lezer. Start pas daarna
   `docker compose --profile p1 up -d p1`. Controleer SQLite, MariaDB en
   de MQTT-proefonderwerpen.
6. Stop **eerst** de oude GPIO-lezer. Start pas daarna
   `docker compose --profile water up -d water`. Controleer pulsen tegen
   de fysieke watermeter.
7. Start na de historische import de dag- en maandberekening met
   `docker compose --profile aggregate up -d aggregate`.

```sh
git clone https://github.com/arjan-io/utility-logging.git
cd utility-logging
cp .env.example .env
# Vul .env met de eigen serveradressen, gebruikers en wachtwoorden.
docker compose build
docker compose up -d p1-sync water-sync
```

De profielen voor P1 en water voorkomen dat een gewone `docker compose up`
dezelfde poort of pin tegelijk met de oude scripts opent. P1 en water
hebben ieder een eigen containerpaar en een eigen SQLite-map. Een storing
in de ene meetbuffer stopt de andere synchronisator niet. Een SQLite-buffer
helpt bij NAS-uitval; een uitgeschakelde Pi kan geen waterpulsen tellen.

## Huidige testgrens

De Python-logica is lokaal met gesimuleerde telegrammen, pulsen en
NAS-uitval getest. De containers en GPIO-toegang zijn nog niet op de
32-bit Pi getest en de MariaDB-SQL is nog niet op een server uitgevoerd.
Gebruik deze versie daarom nog niet als vervanging van de oude lezers.

Er staan geen wachtwoorden, database-exports of persoonlijke
migratienotities in deze repository.
