<p align="center">
  <img src="logo.svg" alt="OptimCE-Logo" width="160">
</p>

# OptimCE — Rechnungsdienst

[![Website](https://img.shields.io/badge/Website-optimce.be-2e7d32.svg)](https://www.optimce.be/de/)
[![Lizenz](https://img.shields.io/badge/Lizenz-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-lightgrey.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-43a047.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](README.nl.md)

Erzeugt, stellt aus, verfolgt und gleicht Rechnungen für
Energy-Sharing-Vorgänge ab (Wallonie / CWaPE in v1). Die Gemeinschaft (der
*représentant du partage*) stellt den Mitgliedern die lokal geteilte
Elektrizität in Rechnung und vergütet die Erzeuger, indem sie die
Abrechnungsdaten bepreist, die das zentrale CRM bereits in `meter_consumption`
speichert.

Dies ist ein **Annexe**-Dienst: Er besitzt seine eigene Datenbank und liest die
zentrale CRM-Datenbank nur lesend. Er **importiert** oder analysiert **keine**
CWaPE-Dokumente — das übernimmt das zentrale CRM; dieser Dienst liest
`meter_consumption` und bepreist es.

Dieser Dienst ist Teil der [OptimCE](https://www.optimce.be/de/)-Plattform. Der
vollständige Stack (Gateway, Authentifizierung, Datenbanken und die übrigen
Dienste) wird aus dem [Monorepo](https://github.com/OptimCE/monorepo)
ausgeführt.

## Zwei Deployables

- **billing-api** (FastAPI, `main.py`) — CRUD der Tarife, Orchestrierung der
  Abrechnungsläufe, Ausstellen/Versenden/Bezahlen/Gutschrift und die
  Lese-Modelle der Rechnungen. Reiht schwere Arbeit in die Warteschlange ein.
- **billing-worker** (`worker/main.py`) — NATS-JetStream-Consumer: bepreist den
  eingefrorenen Snapshot eines Laufs zu DRAFT-Rechnungen und fordert/hängt
  Rechnungs-PDFs vom Dienst document-generation an. Idempotent pro Lauf / pro
  Rechnung.

## Kernablauf

1. `POST /sharing-operations/{id}/tariffs` — von der Gemeinschaft festgelegte
   Preise (freies Feld), zwei Achsen (`kind`: consumer-selling /
   producer-buyback), Geltungsbereiche GLOBAL → SEGMENT (`client_type`) → EAN,
   der spezifischste gewinnt.
2. `POST /sharing-operations/{id}/billing-runs {period_start, period_end}` —
   Vorprüfung (Verbrauch vorhanden, IBAN + rechtlicher Name der Gemeinschaft
   vorhanden, ein GLOBAL-Tarif je abgerechneter Richtung, kein Doppelimport),
   dann **einfrieren** eines `settlement_snapshot` (`SUM(shared)` /
   `SUM(inj_shared)` je EAN) und in die Warteschlange stellen.
3. Der Worker bepreist den Snapshot → **DRAFT**-Rechnungen (eine je
   Mitglied/Richtung; eine Verbrauchsrechnung und/oder ein
   Erzeuger-Abrechnungsbeleg).
4. `POST /invoices/{id}/issue` — vergibt eine lückenlose Nummer je Serie
   (`F-YYYY-#####` / `NC-…` / `DP-…`), eine belgische strukturierte
   OGM-Mitteilung, setzt ISSUED; der Worker rendert anschließend das PDF über
   document-generation und hängt es an.
5. `POST /invoices/{id}/send` (Noop-E-Mail in v1) → SENT · `POST …/payments` →
   PAID · `POST /billing-runs/overdue-sweep` → OVERDUE · `POST …/credit-note` →
   eine negierte DRAFT-Gutschrift (stellen Sie sie für ihre eigene NC-Nummer
   aus).

Regionale Regeln (MwSt., rechtliche Hinweise, Nummerierungsformat,
Zahlungsfristen) liegen hinter einem `BillingRegime`, das aus dem
`regulator`-Code der Gemeinschaft aufgelöst wird; v1 liefert
`CwapeWalloniaRegime`. Preise gehören **nie** dem Regime — sie sind freie Felder
der Gemeinschaft.

## Ausführen und prüfen

Erfordert Docker (pytest-docker startet Postgres auf 5433) und das venv des
Dienstes.

```sh
# aus billing/
ENV=test .venv/Scripts/python.exe -m pytest -q          # vollständige Suite
.venv/Scripts/python.exe -m ruff check .                # lint
.venv/Scripts/python.exe -m mypy api ports regime utils worker
```

Lokale Entwicklung: Kopieren Sie `.env.exemple` → `.env.local`, dann
`uvicorn main:app --reload` (API) und `python -m worker.main` (Worker).

## Deployment-Hinweise

- **Regulierer-Registry:** Die Paritätsassertion beim Start liest die
  gemeinsame Datei `reference/regulators.json`. In einem Container liegt sie
  außerhalb des Build-Kontexts — mounten Sie sie und setzen Sie
  `REGULATORS_CONFIG_PATH`. `regime/billing_regimes.json` (MwSt.,
  Zahlungsfristen, Nummernformat, rechtliche Hinweise) ist mitgeliefert.
- **Dokumentvorlagen:** Laden Sie
  `document-templates/billing/{invoice,producer_statement}/v1/` in den S3-Bucket
  `optimce-templates` hoch und lassen Sie `INVOICE_TEMPLATE_URI` /
  `PRODUCER_STATEMENT_TEMPLATE_URI` darauf zeigen. Das `required_fields`-Schema
  muss mit `api/billing/mappers.py::build_docgen_data` im Gleichschritt bleiben.
- **Schema:** rohes SQL (`scripts/sql/schema.sql`), als Ganzes auf eine frische
  DB angewendet; eine bestehende DB über `scripts/sql/migrations/`
  weiterentwickeln.

## Offene Punkte (ausstehende Freigabe)

- MwSt.-Satz/-Befreiungen (21 % Platzhalter; MwSt. des Erzeuger-Abrechnungsbelegs
  zu bestätigen) — steuerliche Prüfung.
- `KWH_SCALE=1.0` — vor dem ersten echten Lauf mit echten CWaPE-Daten erneut
  bestätigen.

## Mitwirken

Beiträge sind willkommen! Bitte lesen Sie die
[Richtlinien für Beiträge](../CONTRIBUTING.md) und unseren
[Verhaltenskodex](../CODE_OF_CONDUCT.md) (auf Englisch), bevor Sie ein Issue oder
einen Pull Request eröffnen.

## Sicherheit

Um eine Sicherheitslücke zu melden, folgen Sie bitte der
[Sicherheitsrichtlinie](../SECURITY.md) — eröffnen Sie kein öffentliches Issue.

## Lizenz

Dieses Projekt ist unter der [Apache-Lizenz 2.0](../LICENSE) lizenziert.
