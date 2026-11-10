# Utility logging

Docker-diensten voor elektriciteit, gas en water op een Raspberry Pi.
MariaDB en de MQTT-broker draaien elders. Dit project bevat uitsluitend
de uitvoerbare diensten, hun containerconfiguratie en dit installatieblad.
SQL voor het aanmaken van de MariaDB-tabellen en de eenmalige migratie wordt
apart aangeleverd en door de beheerder zelf uitgevoerd.

Alle ruwe meettijdstippen worden in UTC opgeslagen; de website kan die
naar de gewenste tijdzone omzetten. `usage_date` en `usage_month` zijn
kalenderlabels voor de Nederlandse dag of maand, geen tijdstippen. Hun
grenzen worden uit UTC-metingen in `Europe/Amsterdam` berekend, zodat
zomer- en wintertijd de dagtotalen niet een uur verschuiven.

## Wat draait er?

| Dienst | Taak |
| --- | --- |
| `p1` | Leest één P1-seriële poort en bewaart elk geldig telegram eerst in lokale SQLite. |
| `water` | Telt geaccepteerde pulsen op GPIO17 direct in lokale SQLite en schrijft nulmetingen. |
| `p1-sync` | Stuurt alleen P1-events naar MariaDB en herhaalt veilig na een storing. |
| `water-sync` | Stuurt alleen waterevents naar MariaDB en herhaalt veilig na een storing. |
| `aggregate` | Berekent gesloten dagen en maanden voor grafieken, met onbekende waarden bij meetgaten. |
| `maintenance` | Ruimt oude ruwe NAS-metingen in kleine porties op na de historische import. |
| `publisher` | Publiceert actueel vermogen, waterdebiet en variabele kosten naar MQTT. |
| `tariffs` | Beheert contracten, afrekenperioden en tariefregels via een lokale webpagina. |
| `costs` | Berekent geschatte dagkosten per onderdeel uit dagverbruik en actieve tarieven. |
| `pv-probe` | Leest eenmalig een GoodWe SEMS+-station zonder data op te slaan. |
| `pv` | Vraagt SEMS+ periodiek uit en bewaart de cumulatieve opbrengst in een eigen SQLite-buffer. |
| `pv-sync` | Stuurt PV-metingen onafhankelijk naar MariaDB en herhaalt na NAS-uitval. |

De eenmalige historische importer is ook in de standaardimage opgenomen.
Hij draait niet als achtergrondservice en begint zonder `--apply` in
alleen-lezen proefmodus.

De MQTT-instellingen voor de definitieve opzet zijn
`MQTT_PREFIX=sensors` en een lege `MQTT_COMPAT_PREFIX`. Daarmee verschijnen
de berichten uitsluitend op `sensors/dsmr/electricity`,
`sensors/dsmr/kosten`, `sensors/dsmr/gas`, `sensors/water` en `sensors/pv`.
Vul `MQTT_COMPAT_PREFIX` alleen wanneer je bewust tegelijk een tweede
reeks onderwerpen wilt publiceren. Na een wijziging van deze instellingen
moeten zowel `publisher` als `pv` opnieuw worden aangemaakt. De MQTT-kosten zijn
**variabele afnamekosten**. De aparte dienst `costs` berekent dagregels
voor vaste levering, belastingvermindering en netbeheer. Saldering en
afrekening van verbruiksbanden vragen nog een afzonderlijke berekening;
deze versie claimt daarvoor geen factuurbedrag.

De dienst `publisher` maakt uit `nuts_v2` dezelfde JSON-berichten voor
P1-stroom (`dsmr/electricity`), stroomkosten (`dsmr/kosten`), gasdebiet
en gaskosten (`dsmr/gas`) en waterdebiet en waterkosten (`water`). De
oude `usage.py` en het losse waterscript zijn daarvoor niet meer nodig.
De PV-container publiceert `pv[0].pac` en `pv[0].eDag` op `sensors/pv`.
Bij een daggrens vóór de P1-omschakeling gebruikt de gasberekening de
geïmporteerde P1-tijd als benadering voor de oude gasstand; nieuwe
gasstanden gebruiken de eigen DSMR-gastijd.

Het P1-bericht bevat naast `gebruik.hv` (netafname in W) en `gebruik.hl`
(teruglevering in W) nu ook `gebruik.pvW` en `gebruik.huisW`.
De laatste waarde is `max(0, pvW + hv - hl)`: rechtstreeks PV-verbruik in
huis plus afname van het net. De laatst ontvangen PV-meting mag maximaal
tien minuten oud zijn; de P1-meting maximaal twee minuten. Als PV te oud
is, blijven beide nieuwe velden leeg (`null`); als alleen P1 te oud is,
blijft `huisW` leeg.
De PV-waarde komt uit SEMS+ en kan ongeveer twee minuten achterlopen.

`water.rate` is het gemiddelde aantal liters per minuut over de laatste
drie minuten. Na drie minuten zonder puls wordt het nul, mits een
waterheartbeat in de laatste tien minuten bevestigt dat de lezer nog
werkt. Zonder zo'n heartbeat wordt het `null`. Bij zeer langzame pulsen
kan deze recente gemiddelde waarde afwisselend nul en positief zijn;
het is geen meting van een continu stromend debiet.

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
   `docker compose --profile publish up -d publisher`; controleer de
   onderwerpen onder de ingestelde `MQTT_PREFIX`.
5. Stop **eerst** de oude P1-lezer. Start pas daarna
   `docker compose --profile p1 up -d p1`. Controleer SQLite, MariaDB en
   de MQTT-onderwerpen.
6. Stop **eerst** de oude GPIO-lezer. Start pas daarna
   `docker compose --profile water up -d water`. Controleer pulsen tegen
   de fysieke watermeter.
7. Start na de historische import de dag- en maandberekening met
   `docker compose --profile aggregate up -d aggregate`.

Voor de P1-poort kies je in `.env` het apparaat op de host. Een stabiel pad
onder `/dev/serial/by-id/` heeft de voorkeur wanneer dat bestaat. De
container ziet het altijd als `/dev/p1`.
De P1-lezer bewaart standaard één geldig telegram per 30 seconden in SQLite
en daarna in MariaDB. De meter mag elke seconde blijven zenden. Met
`P1_CAPTURE_SECONDS` in `.env` kun je dit interval aanpassen. Een interval
van 30 seconden houdt de metingen rond de Nederlandse daggrens ruim binnen
de grens van twee minuten voor de dagberekening.

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
en opgewekte energie. Ontbrekende velden krijgen `null`;
`energy_counters_available=false` betekent dat SEMS+ bij die opvraag geen
bruikbare cumulatieve stand leverde. De proef vraagt ook de afzonderlijke
dagopbrengst van het station op onder `station_day_fallback`, met een korte
`fallback_status`. Herhaal de proef later als ook die ontbreekt. De collector
slaat zo'n onvolledig antwoord niet op en probeert bij de volgende cyclus
opnieuw. De PV-proef heeft
een eigen Python 3.12-image, omdat `pygoodwe` dat vereist. De officiële
image ondersteunt ARMv7 op de huidige 32-bit Pi en ARM64 op een latere Pi.
`pv-probe` blijft na afloop niet draaien en opent geen seriële of GPIO-poort.

Bij een geslaagde proef kunnen de getallen zonder eenheid uit de cloud komen.
Vergelijk `station_day_generation` en `station_total_generation` met dezelfde
waarden op de SEMS+-stationpagina. Voeg **pas daarna**
`GOODWE_TOTAL_UNIT=kWh` of `GOODWE_TOTAL_UNIT=MWh` toe aan de bestaande
lokale `.env`. De PV-dienst weigert te starten als deze
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

`pv` publiceert na iedere bruikbare SEMS+-uitlezing ook naar
`sensors/pv` (of `<MQTT_PREFIX>/pv`). De velden `pv[0].pac` (watt) en
`pv[0].eDag` (kWh) hebben hetzelfde JSON-pad als het oude GoodWe-script.
`measuredAtUtc` is het ophaaltijdstip, niet de onbekende meettijd in de
cloud. Als de dagteller tijdelijk ontbreekt, blijft de laatst ontvangen
dagwaarde voor dezelfde lokale dag beschikbaar en wordt
`dayGenerationFresh=false`. Als de cumulatieve teller ontbreekt maar het
huidige vermogen wel aanwezig is, verschijnt het vermogen toch op MQTT;
zo'n meting krijgt geen MariaDB-record. Bij een MQTT-storing blijft de
SQLite-opslag werken.

Met `GOODWE_POLL_SECONDS=60` begint een nieuwe opvraag ongeveer iedere
minuut. Duurt een cloudopvraag langer dan een minuut, dan begint de
volgende meteen na afloop. Het interval beschrijft de opvragen; de cloud
kan dezelfde meetwaarde meermaals teruggeven.

## Tarieven invoeren

De pagina gebruikt de bestaande tabellen `contract`, `settlement_period`,
`tariff_source` en `tariff_rule` in `nuts_v2`. Stroom, gas en water hebben
ieder een eigen contractlijn. Een nieuw contract sluit het vorige contract
voor dezelfde nutssoort op zijn ingangsdatum. Een tariefregel met dezelfde
herkenningsnaam en bron sluit bij activering de vorige regel op zijn
ingangsdatum. De einddatum is steeds exclusief. Netbeheer en overheid
hebben eigen bronnen buiten de leveringscontracten; jaarlijkse regels
kunnen bijvoorbeeld op 1 januari ingaan.
Dezelfde tabellen kunnen later door een nieuwe Node-website worden gelezen;
deze beheerpagina is een tijdelijke, zelfstandige invoerroute voor de
migratie.
De webdienst kan meerdere aanvragen tegelijk verwerken. Als een pagina meer
dan een halve seconde duurt, vermeldt de containerlog apart de tijd voor
MariaDB verbinden en voor laden/verzenden. Gebruik bij traagheid
`docker compose logs --since=10m tariffs` om de vertraging te lokaliseren.

Zet `TARIFF_ADMIN_USER` en een eigen sterk `TARIFF_ADMIN_PASSWORD` in
de lokale `.env`. Start vervolgens de aparte dienst:

```sh
cd /home/utility-logging
docker compose --profile tariffs build tariffs
docker compose --profile tariffs up -d tariffs
```

De poort is op de Pi alleen aan `127.0.0.1` gekoppeld. Open vanaf een
andere computer een SSH-tunnel met het eigen Pi-adres of de hostname:

```sh
ssh -L 8765:127.0.0.1:8765 pi@utilityPi.localdomain
```

Open daarna `http://127.0.0.1:8765/` in de browser en meld je aan met
de ingevoerde beheernaam en het wachtwoord. Kies vervolgens **Stroom**,
**Gas** of **Water**. Elke pagina toont alleen de tarieven van die nutssoort;
afgelopen, ingetrokken, verworpen en vervangen regels staan in een apart
archief. Contracten, bronnen en voor stroom de afrekenwijze voeg je op de
betreffende pagina toe. Het oude volledige overzicht blijft bereikbaar via
`/setup` voor uitzonderingen. De pagina kan draaien terwijl
de oude P1- en waterlezers actief blijven; ze gebruikt geen GPIO of
seriële poort.

Kies op de tarievenpagina **Bedrag invoeren**. Kies vervolgens de regel die
op de tariefkaart staat, bijvoorbeeld "Gas per m³" of "Vaste
terugleverkosten per maand". Vul bron, bedrag, eenheid (waar nodig), btw en
ingangsdatum in. Bij vaste terugleverkosten kun je ook een kWh-band
opgeven. De pagina vult onderdeel, richting en telwerk zelf in en slaat de
regel meteen actief op. Het uitgebreide formulier blijft beschikbaar voor
uitzonderingen.

Voer bedragen in met hun **oorspronkelijke eenheid**. Een leverancier kan
€5,99 per maand vragen, een netbeheerder €266,63 per jaar en de
belastingvermindering kan als negatief jaarbedrag zijn opgegeven. De
pagina toont ter controle het dagbedrag voor de huidige kalenderdag;
de opslag blijft per maand of jaar. Btw wordt alleen toegevoegd wanneer
een ingevoerd bedrag exclusief btw is.

Voor drinkwater zijn er aparte keuzes voor verbruik per m³, vastrecht en
belasting op leidingwater. Dunea is de leverancier voor verbruik en
vastrecht; de belasting krijgt een waterbron van de overheid. Bij water
staat de invoer standaard op exclusief 9% btw, zoals op de gedeelde oude
factuur. Controleer bij elke nieuwe tariefkaart opnieuw of het bedrag
inclusief of exclusief btw is.

De groene tariefkaart dient als invoercontrole: stroom normaal bestaat
uit €0,15010 levering plus €0,11085 belasting = €0,26095/kWh;
stroom dal uit €0,14768 plus €0,11085 = €0,25853/kWh; gas uit
€0,71510 plus €0,72680 = €1,44190/m³. Voer die totaaltarieven niet
nogmaals als aparte regel in. Maak eerst een contract of bron, dan de
tariefregels met de eenvoudige route. Controleer daarbij bedrag, btw,
eenheid en datum. Regels met een verbruiksband en de afrekening van
saldering staan afzonderlijk; het eenvoudige overzicht telt die niet
bij de actuele variabele afnametarieven op.

Een fout concept kun je met **Concept verwerpen** buiten gebruik stellen.
Een verkeerd actief tarief kun je via **Intrekken** met bevestiging buiten
gebruik stellen en opnieuw invoeren. Beide regels blijven in het archief
staan; ingetrokken regels tellen niet meer mee. Een normaal opvolgend tarief
kun je meteen invoeren met zijn eigen ingangsdatum.

Voor **Teruglevering boven verbruik (geen btw)** kies je op de stroompagina
**Bedrag invoeren** en daarna de passende terugleververgoeding. Kies de
stroomleverancier, vul de vergoeding als negatief bedrag
per kWh in en geef de geldigheidsperiode op. Onderdeel, richting en 0% btw
worden automatisch ingevuld. Deze vergoeding voor netto overschot na
salderen verschijnt pas in de afrekening, niet als gewone dagopbrengst.

Voor **vaste terugleverkosten per maand** staat er een tweede kort formulier.
Dit is een positieve kost met richting `Vast bedrag`, ook al ontstaat hij
door teruglevering. Vul bij een verbruiksband de ondergrens en de exclusieve
bovengrens in; voor 751 t/m 1000 kWh dus 751 en 1001. Zolang de
terugleverband van de afrekenperiode niet bekend is, boekt de
dagberekening deze bandkosten niet als een zeker bedrag.

De `publisher` gebruikt actieve leverings- en belastingregels voor zijn
huidige variabele MQTT-kosten. De aparte dagberekening schrijft vaste
levering en netbeheer ook naar `daily_cost_line`. De databasegebruiker
heeft hiervoor naast zijn bestaande rechten `DELETE` op alleen
`daily_cost_line` nodig: de dienst vervangt oude schattingen na een
tariefwijziging. Start de berekening nadat `aggregate` loopt en de
tarieven zijn ingevoerd:

Controleer vooraf de ingevoerde tarieven en de rekenregels zonder bestaande
daggegevens. De onderstaande opdracht leest alleen uit MariaDB. Ze rekent
met duidelijk gemarkeerd fictief verbruik: 4 kWh normaal, 2 kWh dal,
1 m³ gas, 0,1 m³ water en 1 kWh teruglevering. Gebruik twee datums om
de overgang op 1 januari 2027 te zien. Het resultaat toont ook welke
afrekenbanden niet als zekere dagkosten kunnen worden geboekt:

```sh
docker compose build costs
docker compose run --rm --no-deps costs python -m nuts.tariff_check \
  --day 2026-11-07 --day 2027-01-01
```

Start daarna, wanneer echte dagtotalen beschikbaar zijn:

```sh
docker compose --profile costs up -d costs
```

Een controle van één lokale dag schrijft niets:

```sh
docker compose run --rm --no-deps costs python -m nuts.costs --check-day 2026-11-05
```

De dienst herberekent standaard de laatste 35 gesloten Nederlandse
kalenderdagen. De kostenregels onderscheiden variabele levering, energiebelasting, vaste
levering, belastingvermindering en netbeheer. Een overzicht van kosten
zonder netbeheer kan daardoor dat onderdeel eenvoudig weglaten. Bedragen
per maand of jaar worden per echte kalenderdag verdeeld; vaste kosten
blijven bestaan als de meter die dag niets registreert. Ontbrekend
verbruik levert geen verzonnen variabele kosten op. Saldering en
verbruiksafhankelijke terugleverkosten blijven schattingen tot de
afrekening; regels met een onbekende afrekenband worden niet als nul
geboekt. De oude `nota.php`-functie kan later worden opgebouwd uit
maandverbruik en maandkosten.

## Voorschotten

Voer eerst de apart aangeleverde phpMyAdmin-SQL voor `advance_schedule` en
`advance_schedule_contract` uit. Daarna opent **Voorschotten** bovenaan
de tarievenpagina een apart invoerscherm. Een voorschot is het afgesproken
bedrag per betaling, geen geregistreerde bankbetaling en geen onderdeel
van het energietarief.

Kies één stroom-, gas- of watercontract, of kies stroom en gas samen.
Een gezamenlijk voorschot heeft één bedrag en twee contractkoppelingen;
tel het in een toekomstig `nota`-overzicht maar één keer. Water kan alleen
apart worden gekoppeld. Vul het bedrag per maand of kwartaal, de
ingangsdatum en de eerste betaaldatum in. Een nieuw bedrag voor dezelfde
contracten sluit de vorige regeling op zijn ingangsdatum. Een foutieve
regeling kun je verwerpen; het administratieve record blijft bewaard.

## Historische import voorbereiden

De oude website toont ongeveer 31 kalenderdagen uit dagtotalen, circa
13 maanden uit maandtotalen en meerjarige vergelijkingen uit maandtotalen.
Daarom bewaart `daily_usage` de beschikbare oude dagen en
`monthly_usage` de oude maanden als zelfstandige historische waarden.
Jaarcijfers worden uit `monthly_usage` opgeteld; er is geen aparte
jaartabel nodig. Voor nieuwe data blijven de afzonderlijke P1-metingen,
waterpulsen inclusief nulmetingen en PV-metingen in de ruwe tabellen
standaard 90 Nederlandse kalenderdagen beschikbaar. Wachtende SQLite-events
blijven altijd staan. Bevestigde SQLite-events worden na zeven dagen in
kleine porties opgeruimd; `water_counter` bewaart de lokale pulsteller.

## Ruwe metingen opruimen

De ruwe MariaDB-tabellen zijn veel groter dan de dag- en maandtabellen.
`maintenance` verwijdert uitsluitend oude rijen uit `p1_reading`,
`water_event` en `pv_reading`. `daily_usage` en `monthly_usage` blijven
volledig bewaard, ook voor de historische grafieken. Alleen volledig
afgelopen Nederlandse kalendermaanden met een opgeslagen maandtotaal
voor het betreffende onderdeel komen in aanmerking. De bewaartermijn
begint standaard 90 dagen terug. De bewaartermijn moet
minstens 60 dagen zijn en langer dan `CALC_LOOKBACK_DAYS`, zodat de
dagberekening nog op ruwe gegevens kan terugvallen. Het script verwijdert maximaal
10.000 rijen per tabel en ronde, met 500 rijen per transactie.

Start dit pas nadat de import en de dag- en maandtotalen zijn gecontroleerd.
De standaardopdracht is alleen-lezen:

```sh
docker compose --profile maintenance run --rm --no-deps maintenance python -m nuts.retention
```

Voor een eenmalige, begrensde opruiming:

```sh
docker compose --profile maintenance run --rm --no-deps maintenance python -m nuts.retention --apply
```

Herhaal die opdracht zolang er oude rijen over zijn. Als de uitslag klopt,
kan de achtergrondservice het daarna iedere 15 minuten voortzetten:

```sh
docker compose --profile maintenance up -d maintenance
```

Deze service heeft gerichte `DELETE`-rechten op de drie ruwe tabellen nodig;
de SQL daarvoor wordt afzonderlijk voor phpMyAdmin aangeleverd. Een nieuwe
`--apply` van de historische importer na het opruimen voegt de oude ruwe
bronrijen opnieuw toe. De oude `nuts-*`-databases worden door deze service
niet gewijzigd.

De importer leest oude P1- en watermetingen, de bestaande dag- en
maandtotalen en `nuts-pv.opbrengst`. Oude maanden blijven maandregels;
ontbrekende dagen worden niet uit een maandbedrag afgeleid. Ruwe
waterrijen met 0 liter blijven tijdankers. Waar opeenvolgende nulmetingen
een hele lokale dag of maand dekken, krijgen de ruwe pulsen voorrang op de
oude waterberekening. Bij gaten blijft het oude totaal hoogstens als
`estimated` beschikbaar; zonder oud totaal blijft de periode onbekend.
De import gebruikt vaste
event-ID's per oude bronrij, zodat een herhaling geen dubbele ruwe
metingen maakt. De oude PV-dagwaarde `eDag` en de laatst gedateerde
maandstand `eMaand` krijgen de kwaliteit `estimated`. Als de laatste
maandstand lager is dan een eerdere bruikbare stand, gebruikt de importer
de hoogste stand als schatting. Dubbele PV-dagregels blijven voor die dag
`unknown`. Negatieve oude waterperioden worden eveneens `unknown`.
Oude `TIMESTAMP`-waarden worden in een UTC-databasesessie gelezen en
daarna naar de Amsterdamse kalenderdatum omgerekend. Daardoor valt
bijvoorbeeld 30 september 22:00 UTC terecht in de maand oktober.

Voor de controle heeft de container leesrechten op de oude databases
nodig. Zet zo nodig een bestaand alleen-lezen account in
`LEGACY_DB_USER` en `LEGACY_DB_PASSWORD` in de lokale `.env`. De
schrijfgebruiker voor `nuts_v2` blijft `DB_USER`. De onderstaande proef
leest de oude tabellen en schrijft niets:

```sh
cd /home/utility-logging
docker compose build p1-sync
docker compose run --rm --no-deps p1-sync python -m nuts.import_legacy --pv-unit kWh --skip-pv-id 1717 --skip-pv-id 1718
```

Controleer de gemelde aantallen, datums, dubbelen en waarschuwingen. De
eenheid van de **oude** PV-kolommen moet afzonderlijk worden bevestigd;
`GOODWE_TOTAL_UNIT` beschrijft alleen de nieuwe cloudwaarden. De
uitvoermodus vereist daarom expliciet `--pv-unit kWh` of `--pv-unit MWh`
naast `--apply`. Gebruik die modus pas na een verse backup en een
geslaagde proef op de brongegevens. De importer leest ruwe rijen tijdens het
schrijven in kleine pagina's op bron-ID en schrijft ze in kleine transacties.
Een verbroken NAS-verbinding laat reeds bevestigde pagina's intact. Start
na een onderbreking dezelfde opdracht opnieuw; reeds aanwezige bron-ID's
worden veilig overgeslagen.
De oude PV-rijen 1717 en 1718 hebben datums uit 2024 maar tellerstanden
uit 2025. Hun juiste dagen zijn niet bewezen. De twee expliciete
`--skip-pv-id` opties houden ze buiten de aggregatie, zodat de geldige
2024-dagen behouden blijven. De oude tabel blijft ongewijzigd; gebruik
dezelfde opties later ook bij `--apply`.
Andere dubbele PV-dagen en dalende maandstanden verschijnen als waarschuwing.
De importer kiest voor een onopgeloste dubbele dag geen waarde. Bij een dalende maandstand
wordt de hoogste bruikbare stand als schatting vastgelegd. Na een backup kan
`--apply` ook met deze waarschuwingen draaien: onbekende waarden blijven
leeg en de overige maandhistorie blijft beschikbaar.

## Dagberekening zonder schrijven controleren

De dagcontrole gebruikt alleen `SELECT` op `nuts_v2`. Ze toont per bron
het aantal rijen rond de daggrenzen, de eerste en laatste UTC-meettijd,
het berekende verbruik en de kwaliteitsstatus. Als een bestaande
geïmporteerde dagwaarde behouden blijft, staat de bron in
`preserved_existing`. Er worden geen dag- of
maandregels opgeslagen:

```sh
cd /home/utility-logging
docker compose build aggregate
docker compose --profile aggregate run --rm --no-deps aggregate \
  python -m nuts.aggregate_probe 2026-11-05
```

Gebruik een afgesloten lokale kalenderdag. De eerste PV-dag kan `unknown`
zijn wanneer de cloudcollector pas midden op de dag begon; dat is geen
nulopbrengst. Voor een complete PV-dag zijn meetpunten rond beide
middernachtgrenzen nodig. Start de doorlopende `aggregate`-dienst pas
nadat de berekening en de historische gegevens zijn vergeleken.

## Huidige testgrens

De Python-logica is lokaal met gesimuleerde telegrammen, pulsen en
NAS-uitval getest. Een P1- en waterinsert met rollback en de weg van
tijdelijke SQLite-records naar MariaDB met rollback zijn vanuit een
container op de 32-bit Pi bevestigd. Beide synchronisatiecontainers
draaien. PV wordt live naar MariaDB gestuurd; de recente rijen kwamen
meestal ongeveer iedere minuut binnen. De nieuwe PV-MQTT-publicatie,
historische importer en dagaggregatie zijn nog niet op echte gegevens
gecontroleerd. GPIO-toegang en de P1-poort worden pas bij de overdracht
aan de nieuwe lezers getest.

Er staan geen wachtwoorden, database-exports of persoonlijke
migratienotities in deze repository.
