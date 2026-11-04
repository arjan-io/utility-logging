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
| `pv-probe` | Leest eenmalig een GoodWe SEMS+-station zonder data op te slaan. |
| `pv` | Vraagt SEMS+ periodiek uit en bewaart de cumulatieve opbrengst in een eigen SQLite-buffer. |
| `pv-sync` | Stuurt PV-metingen onafhankelijk naar MariaDB en herhaalt na NAS-uitval. |

De oude MQTT-velden blijven beschikbaar. Met `MQTT_PREFIX=sensors/v2`
verschijnen ze eerst onder proefonderwerpen. Pas bij de definitieve
omschakeling wordt de prefix `sensors` gebruikt. De kosten zijn momenteel
**variabele afnamekosten**. Vaste leveringskosten, netbeheer, saldering en
teruglevering vragen nog een afzonderlijke afrekenberekening; deze versie
claimt daarvoor geen factuurbedrag.

## Voorbereiden op de Pi

Gebruik de Docker Compose-plugin met een spatie: `docker compose`. Dit
project gebruikt Compose-profielen. De oude Python-versie `docker-compose`
met koppelteken is hiervoor niet ondersteund; die kan `services:` als een
servicenaam lezen en dan ten onrechte een interpolatiefout melden. Controleer
eerst `docker compose version`. Als Compose ontbreekt, installeer op
Raspberry Pi OS de pakketnaam `docker-compose-plugin` uit de Docker-pakketten.
Dat kan, als de Docker-pakketbron al is ingesteld, met:

```sh
sudo apt-get update
sudo apt-get install docker-compose-plugin
docker compose version
```

Als `apt` geen pakketkandidaat vindt maar Docker Engine al werkt, kan de
Compose-plugin [handmatig voor alleen deze gebruiker worden geïnstalleerd](https://docs.docker.com/compose/install/linux/#install-the-plugin-manually).
Controleer eerst `dpkg --print-architecture` en `uname -m`. Voor een
32-bit Pi 4 met `armhf` en `armv7l` gebruikt de onderstaande proef de
officiële ARMv7-release. De checksum komt uit de
[Docker-imagebouw](https://github.com/docker-library/repo-info/blob/master/repos/docker/remote/dind-rootless.md).
De bestaande Docker Engine wordt hierbij niet vervangen.

```sh
mkdir -p /home/pi/.docker/cli-plugins
curl -fL https://github.com/docker/compose/releases/download/v5.6.1/docker-compose-linux-armv7 -o /home/pi/.docker/cli-plugins/docker-compose
printf '%s  %s\n' 'e2ed79b41693f5e6473eb9c1005bd4cbb83844d8220ac72fe5c6a6cc5e0bfee8' '/home/pi/.docker/cli-plugins/docker-compose' | sha256sum -c -
chmod +x /home/pi/.docker/cli-plugins/docker-compose
docker compose version
```

Voer `chmod` alleen uit als de checksum `OK` meldt. Een handmatig
geïnstalleerde plugin wordt niet automatisch bijgewerkt. Gebruik op een
latere 64-bit installatie bij voorkeur de
[officiële Docker-pakketbron](https://docs.docker.com/engine/install/debian/)
en `docker-compose-plugin` via `apt`.

Op deze Pi staat de repository in `/home/utility-logging`.
`DATA_DIR=./data` bewaart de SQLite-buffers
naast de repository; die map is uitgesloten van Git. Gebruik dezelfde
`data/`-map wanneer je later de Pi of het besturingssysteem vervangt.
Het Dockerfile heeft geen vastgelegde CPU-architectuur: Docker kiest op de
32-bit Pi 4 de ARMv7-image en op een 64-bit Pi de ARM64-image.

1. Maak eerst de aparte MariaDB-database `nuts_v2` met de los aangeleverde
   phpMyAdmin-SQL. Zet de benodigde tariefregels in die database. De oude
   databases blijven bestaan.
2. Haal deze repository op de Pi op met `git clone`; `git init` is niet
   nodig. Vul een eigen `.env` in met [`.env.example`](.env.example) als
   voorbeeld. Bewaar `.env` en de map `data/` buiten Git. Zet `DATA_DIR`
   op een blijvende locatie op de Pi.
3. Bouw de image met `docker compose build`. Controleer de lokale
   configuratie met `docker compose config --quiet`. Test daarna de
   NAS-verbinding en schrijfrechten zonder meetapparaten te openen:

   ```sh
   docker compose run --rm --no-deps p1-sync python -m nuts.dbcheck
   ```

   Deze proef schrijft met de containergebruiker één P1- en één waterrecord
   binnen een InnoDB-transactie en draait die terug. Verwacht een `OK`-regel.
   Vul eerst de echte NAS-gegevens in `.env` in; deel dat bestand nooit.
   De melding dat Buildx ontbreekt is bij een native build op de Pi alleen
   een waarschuwing zolang beide images met `Successfully built` eindigen.
   Controleer daarna de volledige weg van tijdelijke SQLite-buffers via de
   synchronisatiecode naar MariaDB. Ook deze proef draait de MariaDB-rijen
   terug; de tijdelijke SQLite-bestanden worden na afloop verwijderd:

   ```sh
   docker compose run --rm --no-deps p1-sync python -m nuts.sync_check
   ```
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

Voor de P1-poort kies je in `.env` het apparaat op de host. Een stabiel pad
onder `/dev/serial/by-id/` heeft de voorkeur wanneer dat bestaat. De
container ziet het altijd als `/dev/p1`.

Voor de watermeter kies je in `.env` de GPIO-chip met de pinnen van de
40-pins header. Op de Pi 4 is dat meestal `/dev/gpiochip0`; op de Pi 5
kan het afhankelijk van de kernel `/dev/gpiochip0` of `/dev/gpiochip4`
zijn. `gpiodetect` toont de chip met `pinctrl` in het label. Compose koppelt
de gekozen chip binnen de container altijd aan `/dev/gpiochip0`; de code
opent die chip expliciet. De device-tree wordt alleen-lezen gekoppeld zodat
GPIO Zero de BCM-pinnummers kan herkennen. Voor deze instellingen is geen
andere Python-code of Dockerfile nodig bij een overstap naar 64-bit of Pi 5.

```sh
git clone https://github.com/arjan-io/utility-logging.git /home/utility-logging
cd /home/utility-logging
cp .env.example .env
# Vul .env met de eigen serveradressen, gebruikers en wachtwoorden.
docker compose version
docker compose config --quiet
docker compose build
docker compose up -d p1-sync water-sync
```

De profielen voor P1 en water voorkomen dat een gewone `docker compose up`
dezelfde poort of pin tegelijk met de oude scripts opent. P1 en water
hebben ieder een eigen containerpaar en een eigen SQLite-map. Een storing
in de ene meetbuffer stopt de andere synchronisator niet. Een SQLite-buffer
helpt bij NAS-uitval; een uitgeschakelde Pi kan geen waterpulsen tellen.

## GoodWe SEMS+ controleren

De oude SEMS Classic-uitlezing werkt niet meer. De zakelijke
[GoodWe OpenAPI](https://openapi.goodwe.com/#/api/doc-10a00bfcb2c270)
vereist een afzonderlijke Client ID en Client Secret. Voor een bestaande
SEMS+-login gebruikt de `pv-probe` een Python-bibliotheek voor de
SEMS+-webinterface. Die interface is niet officieel gedocumenteerd en kan
veranderen. De proef leest alleen en schrijft niets naar MariaDB of MQTT.

Vul `GOODWE_STATION_ID`, `GOODWE_ACCOUNT` en `GOODWE_PASSWORD` uitsluitend
in de lokale `.env` op de Pi in. De station-ID staat in de URL van de
SEMS+-stationpagina; een eerder Classic-stationnummer kan na migratie ook
werken. Deel de `.env` en ruwe API-antwoorden niet. Voer dan uit:

```sh
cd /home/utility-logging
docker compose --profile pv build pv-probe
docker compose --profile pv run --rm --no-deps pv-probe
```

Verwacht één JSON-regel met alleen numerieke velden voor huidig PV-vermogen
en opgewekte energie. Ontbrekende velden krijgen `null`. De PV-proef heeft
een eigen Python 3.12-image, omdat `pygoodwe` dat vereist. De officiële
image ondersteunt ARMv7 op de huidige 32-bit Pi en ARM64 op een latere Pi.
`pv-probe` blijft na afloop niet draaien en opent geen seriële of GPIO-poort.

Bij een geslaagde proef kunnen de getallen zonder eenheid uit de cloud komen.
Vergelijk `station_day_generation` en `station_total_generation` met dezelfde
waarden op de SEMS+-stationpagina. Stel **pas daarna** `GOODWE_TOTAL_UNIT=kWh`
of `MWh` in de lokale `.env` in. De PV-dienst weigert te starten als deze
eenheid ontbreekt. Expliciete eenheden in de API-uitvoer worden altijd zelf
omgerekend. Het huidige vermogen uit `powerflow.pv` wordt als watt gelezen;
als dit veld ontbreekt, blijft `current_w` leeg. De oude `inverter_*`-velden
zijn voor dit omvormermodel niet nodig.

De PV-dienst gebruikt het ophaaltijdstip als meettijd. SEMS+ geeft hier geen
betrouwbare bron-tijdstempel terug. Na bevestiging van de eenheid kun je de
PV-buffer en synchronisatie apart starten, zonder de P1- of waterlezer te
stoppen:

```sh
cd /home/utility-logging
git pull --ff-only
docker compose build pv pv-sync
docker compose run --rm --no-deps pv-sync python -m nuts.sync_check
docker compose --profile pv up -d pv pv-sync
docker compose ps pv pv-sync
docker compose logs --tail=30 pv pv-sync
```

De synchronisatieproef gebruikt tijdelijke SQLite-bestanden en draait de
testrecords in MariaDB terug. `pv` en `pv-sync` delen alleen hun eigen
persistente map `data/pv/`. Bij een cloudfout blijft de dienst draaien en
probeert hij na het ingestelde interval opnieuw. Bij een NAS-storing blijft
de PV-uitlezing in SQLite staan tot `pv-sync` deze kan afleveren.

## Huidige testgrens

De Python-logica is lokaal met gesimuleerde telegrammen, pulsen en
NAS-uitval getest. Een P1- en waterinsert met rollback is vanuit een
container op de 32-bit Pi bevestigd. Beide synchronisatiecontainers zijn
op de Pi gestart en hebben hun SQLite-buffer aangemaakt. Een echte
SQLite-naar-MariaDB-proef, GPIO-toegang en de P1-poort moeten nog worden
getest. Gebruik deze versie nog niet als vervanging van de oude lezers.

Er staan geen wachtwoorden, database-exports of persoonlijke
migratienotities in deze repository.
