-- 0003_invoice_credited_status — backfill InvoiceStatus.CREDITED (7).
--
-- Issuing a credit note now moves the consumer invoice it corrects to CREDITED,
-- so a credited bill stops counting as unpaid and is never swept OVERDUE. Credit
-- notes issued before this change left their invoice ISSUED/SENT/OVERDUE/PAID;
-- this brings those rows to the state the service now produces.
--
-- Data only: `invoice.status` is a plain INTEGER with no CHECK, so schema.sql
-- needs no change (a fresh database has no rows to backfill). Idempotent; safe
-- to re-run. Mirrors CREDITABLE_STATUSES in shared/const.py:
--   types:    1 = INVOICE, 2 = CREDIT_NOTE
--   statuses: 0 = DRAFT, 1 = ISSUED, 2 = SENT, 3 = PAID, 4 = OVERDUE,
--             5 = CANCELLED, 6 = RENDER_FAILED, 7 = CREDITED
-- No notification is sent for the backfilled rows.
UPDATE invoice AS original
SET status = 7
FROM invoice AS credit
WHERE credit.corrects_invoice_id = original.id
  AND credit.id_community = original.id_community
  AND credit.type = 2
  AND credit.status NOT IN (0, 5)
  AND original.type = 1
  AND original.status IN (1, 2, 3, 4, 6);
