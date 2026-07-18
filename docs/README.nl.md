<p align="center">
  <img src="logo.svg" alt="OptimCE-logo" width="160">
</p>

# OptimCE — Facturatiedienst

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be/nl/)
[![Licentie](https://img.shields.io/badge/Licentie-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-43a047.svg)](README.nl.md)

Genereert, stelt op, volgt en verrekent facturen voor energiedeeloperaties
(Wallonië / CWaPE in v1). De gemeenschap (de *représentant du partage*)
factureert leden voor lokaal gedeelde elektriciteit en vergoedt producenten,
door de verrekengegevens te beprijzen die het centrale CRM al opslaat in
`meter_consumption`.

Dit is een **annexe**-dienst: hij heeft zijn eigen database en leest de centrale
CRM-database alleen-lezen. Hij **importeert** of verwerkt **geen**
CWaPE-documenten — dat doet het centrale CRM; deze dienst leest
`meter_consumption` en beprijst het.

Deze dienst maakt deel uit van het [OptimCE](https://www.optimce.be/nl/)-platform.
De volledige stack (gateway, authenticatie, databases en de overige diensten)
draait vanuit de [monorepo](https://github.com/OptimCE/monorepo).

## Twee deployables

- **billing-api** (FastAPI, `main.py`) — CRUD van tarieven, orkestratie van
  facturatieruns, uitgifte/verzending/betaling/creditnota, en de leesmodellen
  van de facturen. Plaatst zwaar werk in de wachtrij.
- **billing-worker** (`worker/main.py`) — NATS JetStream-consumers: beprijst de
  bevroren snapshot van een run tot DRAFT-facturen, en vraagt/koppelt
  factuur-PDF's op bij de dienst document-generation. Idempotent per run / per
  factuur.

## Kernstroom

1. `POST /sharing-operations/{id}/tariffs` — door de gemeenschap ingestelde
   prijzen (vrij veld), twee assen (`kind`: consumer-selling / producer-buyback),
   scopes GLOBAL → SEGMENT (`client_type`) → EAN, de meest specifieke wint.
2. `POST /sharing-operations/{id}/billing-runs {period_start, period_end}` —
   pre-flight (verbruik bestaat, IBAN + wettelijke naam van de gemeenschap
   aanwezig, een GLOBAL-tarief per gefactureerde richting, geen dubbele import),
   dan een `settlement_snapshot` **bevriezen** (`SUM(shared)` /
   `SUM(inj_shared)` per EAN) en in de wachtrij plaatsen.
3. De worker beprijst de snapshot → **DRAFT**-facturen (één per lid/richting; een
   verbruiksfactuur en/of een productieafrekening).
4. `POST /invoices/{id}/issue` — kent een sluitend nummer per reeks toe
   (`F-YYYY-#####` / `NC-…` / `DP-…`), een Belgische gestructureerde
   OGM-mededeling, zet op ISSUED; de worker rendert vervolgens de PDF via
   document-generation en koppelt hem.
5. `POST /invoices/{id}/send` (Noop-e-mail in v1) → SENT · `POST …/payments` →
   PAID · `POST /billing-runs/overdue-sweep` → OVERDUE · `POST …/credit-note` →
   een DRAFT-creditnota met omgekeerd teken (geef ze uit voor haar eigen
   NC-nummer).

Regionale regels (btw, wettelijke vermeldingen, nummeringsformaat,
betaaltermijnen) zitten achter een `BillingRegime` die wordt afgeleid uit de
`regulator`-code van de gemeenschap; v1 levert `CwapeWalloniaRegime`. Prijzen
zijn **nooit** eigendom van het regime — het zijn vrije velden van de
gemeenschap.

## Uitvoeren en verifiëren

Vereist Docker (pytest-docker start Postgres op 5433) en de venv van de dienst.

```sh
# vanuit billing/
ENV=test .venv/Scripts/python.exe -m pytest -q          # volledige suite
.venv/Scripts/python.exe -m ruff check .                # lint
.venv/Scripts/python.exe -m mypy api ports regime utils worker
```

Lokale ontwikkeling: kopieer `.env.exemple` → `.env.local`, dan
`uvicorn main:app --reload` (API) en `python -m worker.main` (worker).

## Deployment-opmerkingen

- **Regulator-register:** de pariteitsassertie bij het opstarten leest het
  gedeelde bestand `reference/regulators.json`. In een container bevindt het zich
  buiten de build-context — mount het en stel `REGULATORS_CONFIG_PATH` in.
  `regime/billing_regimes.json` (btw, betaaltermijnen, nummerformaat, wettelijke
  vermeldingen) is meegeleverd.
- **Documentsjablonen:** upload
  `document-templates/billing/{invoice,producer_statement}/v1/` naar de S3-bucket
  `optimce-templates` en laat `INVOICE_TEMPLATE_URI` /
  `PRODUCER_STATEMENT_TEMPLATE_URI` daarnaar verwijzen. Het
  `required_fields`-schema moet gelijk blijven lopen met
  `api/billing/mappers.py::build_docgen_data`.
- **Schema:** ruwe SQL (`scripts/sql/schema.sql`), in zijn geheel toegepast op
  een verse DB; ontwikkel een bestaande DB verder via `scripts/sql/migrations/`.

## Openstaande punten (in afwachting van goedkeuring)

- Btw-tarief/-vrijstellingen (21 % placeholder; btw van de productieafrekening te
  bevestigen) — fiscale evaluatie.
- `KWH_SCALE=1.0` — opnieuw bevestigen met echte CWaPE-gegevens vóór de eerste
  echte run.

## Bijdragen

Bijdragen zijn welkom! Lees de [bijdragerichtlijnen](../CONTRIBUTING.md) en onze
[gedragscode](../CODE_OF_CONDUCT.md) (in het Engels) voordat je een issue of pull
request opent.

## Beveiliging

Om een beveiligingsprobleem te melden, volg je het
[beveiligingsbeleid](../SECURITY.md) — open geen openbare issue.

## Licentie

Dit project is gelicentieerd onder de [Apache-licentie 2.0](../LICENSE).
