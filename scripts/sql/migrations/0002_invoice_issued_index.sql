-- 0002_invoice_issued_index — index invoice issued_at for date-range list queries.
--
-- The invoice-list endpoints (GET /invoices, GET /invoices/mine) now filter and
-- sort by issued_at within a tenant. This adds a composite (id_community,
-- issued_at) index so those range scans stay cheap. Also folded into
-- scripts/sql/schema.sql (next to ix_invoice_status) so a fresh install and a
-- migrated install converge. Idempotent; safe to re-run.
CREATE INDEX IF NOT EXISTS ix_invoice_issued ON invoice (id_community, issued_at);
