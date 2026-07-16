# billing — OptimCE billing generation service

Generates, issues, tracks, and reconciles invoices for energy-sharing operations
(Wallonia / CWaPE in v1). The community (the *représentant du partage*) bills
members for locally shared electricity and remunerates producers, pricing the
settlement data the core CRM already stores in `meter_consumption`.

This is an **annexe** service: it owns its own database and reads the CRM core DB
read-only. It does **not** import or parse CWaPE documents — the core CRM does
that; this service reads `meter_consumption` and prices it.

## Two deployables

- **billing-api** (FastAPI, `main.py`) — tariffs CRUD, billing-run orchestration,
  issue/send/pay/credit-note, and the invoice read models. Enqueues heavy work.
- **billing-worker** (`worker/main.py`) — NATS JetStream consumers: price a run's
  frozen snapshot into DRAFT invoices, and request/attach invoice PDFs from the
  document-generation service. Idempotent per run / per invoice.

## Core flow

1. `POST /sharing-operations/{id}/tariffs` — community-set prices (free field),
   two axes (`kind`: consumer-selling / producer-buyback), scopes GLOBAL → SEGMENT
   (`client_type`) → EAN, most-specific wins.
2. `POST /sharing-operations/{id}/billing-runs {period_start, period_end}` —
   pre-flight (consumption exists, community IBAN + legal name present, a GLOBAL
   tariff per billed direction, no double-import), then **freeze** a
   `settlement_snapshot` (`SUM(shared)` / `SUM(inj_shared)` per EAN) and enqueue.
3. Worker prices the snapshot → **DRAFT** invoices (one per member/direction; a
   consumer invoice and/or a producer statement).
4. `POST /invoices/{id}/issue` — assign a gapless per-series number
   (`F-YYYY-#####` / `NC-…` / `DP-…`), a Belgian OGM structured communication,
   set ISSUED; the worker then renders the PDF via document-generation and
   attaches it.
5. `POST /invoices/{id}/send` (Noop email v1) → SENT · `POST …/payments` → PAID ·
   `POST /billing-runs/overdue-sweep` → OVERDUE · `POST …/credit-note` → a negated
   DRAFT credit note (issue it for its own NC number).

Regional rules (VAT, legal mentions, numbering format, due days) live behind a
`BillingRegime` resolved from the community's `regulator` code; v1 ships
`CwapeWalloniaRegime`. Prices are **never** regime-owned — they are community
free fields.

## Run & verify

Requires Docker (pytest-docker starts Postgres on 5433) and the service venv.

```sh
# from billing/
ENV=test .venv/Scripts/python.exe -m pytest -q          # full suite
.venv/Scripts/python.exe -m ruff check .                # lint
.venv/Scripts/python.exe -m mypy api ports regime utils worker
```

Local dev: copy `.env.exemple` → `.env.local`, then `uvicorn main:app --reload`
(API) and `python -m worker.main` (worker).

## Deployment notes

- **Regulator registry:** the startup parity assertion reads the shared
  `reference/regulators.json`. In a container it lives outside the build context —
  mount it and set `REGULATORS_CONFIG_PATH`. `regime/billing_regimes.json` (VAT,
  due days, number format, legal mentions) is bundled.
- **Document templates:** upload `document-templates/billing/{invoice,producer_statement}/v1/`
  to the `optimce-templates` S3 bucket and point `INVOICE_TEMPLATE_URI` /
  `PRODUCER_STATEMENT_TEMPLATE_URI` at them. The `required_fields` schema must stay
  in lockstep with `api/billing/mappers.py::build_docgen_data`.
- **Schema:** raw SQL (`scripts/sql/schema.sql`), applied wholesale to a fresh DB;
  evolve an existing DB via `scripts/sql/migrations/`.

## Open items (pending sign-off)

- VAT rate/exemptions (21% placeholder; producer-statement VAT to confirm) —
  fiscal review.
- `KWH_SCALE=1.0` — re-confirm against live CWaPE data before the first real run.
