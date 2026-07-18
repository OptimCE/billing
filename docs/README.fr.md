<p align="center">
  <img src="logo.svg" alt="Logo OptimCE" width="160">
</p>

# OptimCE — Service de facturation

[![Site web](https://img.shields.io/badge/Site%20web-optimce.be-2e7d32.svg)](https://www.optimce.be)
[![Licence](https://img.shields.io/badge/Licence-Apache%202.0-blue.svg)](../LICENSE)
[![en](https://img.shields.io/badge/lang-en-lightgrey.svg)](../README.md)
[![fr](https://img.shields.io/badge/lang-fr-43a047.svg)](README.fr.md)
[![de](https://img.shields.io/badge/lang-de-lightgrey.svg)](README.de.md)
[![nl](https://img.shields.io/badge/lang-nl-lightgrey.svg)](README.nl.md)

Génère, émet, suit et réconcilie les factures des opérations de partage
d'énergie (Wallonie / CWaPE en v1). La communauté (le *représentant du
partage*) facture aux membres l'électricité partagée localement et rémunère les
producteurs, en valorisant les données de règlement que le CRM central stocke
déjà dans `meter_consumption`.

Il s'agit d'un service **annexe** : il possède sa propre base de données et lit
la base du CRM central en lecture seule. Il n'**importe** ni n'analyse les
documents CWaPE — c'est le CRM central qui s'en charge ; ce service lit
`meter_consumption` et le valorise.

Ce service fait partie de la plateforme [OptimCE](https://www.optimce.be). La
stack complète (passerelle, authentification, bases de données et les autres
services) s'exécute depuis le [monorepo](https://github.com/OptimCE/monorepo).

## Deux déployables

- **billing-api** (FastAPI, `main.py`) — CRUD des tarifs, orchestration des
  exécutions de facturation, émission/envoi/paiement/note de crédit, et les
  modèles de lecture des factures. Met en file d'attente le travail lourd.
- **billing-worker** (`worker/main.py`) — consommateurs NATS JetStream :
  valorise le snapshot figé d'une exécution en factures DRAFT, et
  demande/attache les PDF de factures au service document-generation. Idempotent
  par exécution / par facture.

## Flux principal

1. `POST /sharing-operations/{id}/tariffs` — prix fixés par la communauté (champ
   libre), deux axes (`kind` : consumer-selling / producer-buyback), portées
   GLOBAL → SEGMENT (`client_type`) → EAN, la plus spécifique l'emporte.
2. `POST /sharing-operations/{id}/billing-runs {period_start, period_end}` —
   pré-vérification (consommation existante, IBAN + raison sociale de la
   communauté présents, un tarif GLOBAL par direction facturée, pas de double
   import), puis **fige** un `settlement_snapshot` (`SUM(shared)` /
   `SUM(inj_shared)` par EAN) et met en file d'attente.
3. Le worker valorise le snapshot → factures **DRAFT** (une par membre/direction ;
   une facture de consommation et/ou un décompte de production).
4. `POST /invoices/{id}/issue` — attribue un numéro sans rupture par série
   (`F-YYYY-#####` / `NC-…` / `DP-…`), une communication structurée belge OGM,
   passe à ISSUED ; le worker rend ensuite le PDF via document-generation et
   l'attache.
5. `POST /invoices/{id}/send` (email Noop en v1) → SENT · `POST …/payments` →
   PAID · `POST /billing-runs/overdue-sweep` → OVERDUE · `POST …/credit-note` →
   une note de crédit DRAFT négativée (émettez-la pour son propre numéro NC).

Les règles régionales (TVA, mentions légales, format de numérotation, délais de
paiement) sont gérées derrière un `BillingRegime` résolu à partir du code
`regulator` de la communauté ; la v1 fournit `CwapeWalloniaRegime`. Les prix ne
sont **jamais** gérés par le régime — ce sont des champs libres de la
communauté.

## Exécution et vérification

Nécessite Docker (pytest-docker démarre Postgres sur 5433) et le venv du
service.

```sh
# depuis billing/
ENV=test .venv/Scripts/python.exe -m pytest -q          # suite complète
.venv/Scripts/python.exe -m ruff check .                # lint
.venv/Scripts/python.exe -m mypy api ports regime utils worker
```

Développement local : copiez `.env.exemple` → `.env.local`, puis
`uvicorn main:app --reload` (API) et `python -m worker.main` (worker).

## Notes de déploiement

- **Registre des régulateurs :** l'assertion de parité au démarrage lit le
  fichier partagé `reference/regulators.json`. Dans un conteneur, il se trouve
  hors du contexte de build — montez-le et définissez `REGULATORS_CONFIG_PATH`.
  `regime/billing_regimes.json` (TVA, délais de paiement, format de numéro,
  mentions légales) est inclus.
- **Modèles de documents :** téléversez
  `document-templates/billing/{invoice,producer_statement}/v1/` vers le bucket S3
  `optimce-templates` et faites pointer `INVOICE_TEMPLATE_URI` /
  `PRODUCER_STATEMENT_TEMPLATE_URI` dessus. Le schéma `required_fields` doit
  rester synchronisé avec `api/billing/mappers.py::build_docgen_data`.
- **Schéma :** SQL brut (`scripts/sql/schema.sql`), appliqué en bloc sur une base
  vierge ; faites évoluer une base existante via `scripts/sql/migrations/`.

## Points ouverts (en attente de validation)

- Taux/exonérations de TVA (placeholder à 21 % ; TVA du décompte de production à
  confirmer) — revue fiscale.
- `KWH_SCALE=1.0` — à reconfirmer avec des données CWaPE réelles avant la
  première exécution réelle.

## Contribuer

Les contributions sont les bienvenues ! Merci de lire le
[guide de contribution](../CONTRIBUTING.md) et notre
[code de conduite](../CODE_OF_CONDUCT.md) (en anglais) avant d'ouvrir une issue
ou une pull request.

## Sécurité

Pour signaler une faille de sécurité, veuillez suivre la
[politique de sécurité](../SECURITY.md) — n'ouvrez pas d'issue publique.

## Licence

Ce projet est distribué sous la [licence Apache 2.0](../LICENSE).
