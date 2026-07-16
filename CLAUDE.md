# billing — working notes for Claude

OptimCE billing annexe (FastAPI + NATS worker, async SQLAlchemy 2.0). Scaffolded
from the `simulation-key` template; mirror its conventions. Read `README.md` for
the domain. Full plan: `~/.claude/plans/snappy-mixing-dahl.md`.

## Layout
- `api/billing/` — `routes` → `service` (orchestration) → `repository` (owned DB) + `mappers`/`schemas`/`deps`.
- `ports/` — `crm_core*` (read-only CRM adapter), `document_generation*` (async NATS), `email*` (Noop), `events` (NATS publish).
- `regime/` — `BillingRegime` Protocol + `CwapeWalloniaRegime` + `registry` (startup parity gate); config from `reference/regulators.json` + `regime/billing_regimes.json`.
- `worker/` — `dispatcher` (consumers) → `persistence.process_billing_run`, `issue.process_issue`, `docgen_results.process_docgen_result` (each callable directly for tests via injected sessions).
- `utils/` — `money` (Decimal HALF_UP), `ogm` (mod-97), `numbering`.
- `scripts/sql/schema.sql` — raw DDL (NO Alembic). `shared/models/local_models.py` mirrors it.

## Conventions
- Tenant column is `id_community`; scope every owned-DB SELECT with `with_community_scope(stmt, Model)`. INSERTs stamp `id_community` from the `current_internal_community_id` ContextVar (set by `resolve_internal_community` on the API, `worker.context.with_tenant` on the worker).
- Cross-DB refs (`id_sharing_operation`, `id_member`, `ean`) are plain columns, never FKs.
- Errors: raise `ErrorException(errors.billing.X, status_code=...)`; every error key needs entries in `locales/{en,fr,de,nl}.json` (tests/test_locales enforces it).
- Money: full Decimal precision on kWh; `round_money` (half-up, 2dp) only at line/VAT/total.

## Gotchas
- **`api/billing/routes.py` must NOT `from __future__ import annotations`.** `with_default_error` resolves string annotations against its own module globals, so stringified Pydantic body types get demoted to query params (FastAPI 422 `loc:[query,body]`). Keep real annotation objects in any wrapped-route module.
- document-generation is async over NATS: publish a FLAT body (no `Event` envelope) to `docgen.request`; the result arrives on `docgen.result.billing`. Issue is two-phase (numbered/ISSUED before the PDF exists).
- `meter_data.client_type` is a segment {1 Résidentiel, 2 Pro, 3 Indus}, not a role — direction derives from `shared` vs `inj_shared` volumes. No unique on `meter_consumption(ean, timestamp)`: the snapshot stores `row_count` vs `distinct_ts_count` → `DOUBLE_IMPORT_DETECTED`.
- NATS streams must have disjoint subjects: BILLING (work_queue) / BILLING_EVENTS (limits) / BILLING_DLQ.

## Verify
`cd billing` then: `ENV=test .venv/Scripts/python.exe -m pytest -q` (needs Docker Postgres on 5433) · `.venv/Scripts/python.exe -m ruff check .` · `... -m mypy api ports regime utils worker`.
