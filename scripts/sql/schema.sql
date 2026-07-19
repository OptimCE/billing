-- ============================================================================
-- Local database schema (LOCAL_DATABASE_URL) — billing service.
--
-- Single source of truth for the LOCAL (owned) database. The CRM core tables
-- (community, sharing_operation, member, meter, meter_consumption, …) live in a
-- separate database and are NOT declared here; this service only reads them via
-- CrmCoreReadPort. Cross-DB references (id_sharing_operation, id_member, ean)
-- are plain columns, never foreign keys.
--
-- Mirrors shared/models/local_models.py. When changing models, update this file
-- and add a migration under scripts/sql/migrations/.
-- ============================================================================

-- ---- Shared utilities ------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at := CURRENT_TIMESTAMP;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE IF NOT EXISTS schema_version (
    version      INTEGER     PRIMARY KEY,
    description  TEXT        NOT NULL,
    applied_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO schema_version (version, description) VALUES
    (1, 'Billing initial schema')
ON CONFLICT DO NOTHING;

-- ---- tariff ----------------------------------------------------------------
-- Community-set prices (free field). Two independent axes (kind); resolution is
-- most-specific-wins EAN → SEGMENT → GLOBAL. The partial unique indexes keep at
-- most one active tariff per (op, kind, scope-ref, valid_from) so resolution is
-- unambiguous.
CREATE TABLE IF NOT EXISTS tariff (
    id                    SERIAL         PRIMARY KEY,
    id_community          INTEGER        NOT NULL,
    id_sharing_operation  INTEGER        NOT NULL,
    kind                  INTEGER        NOT NULL,   -- TariffKind: 1=CONSUMER_SELLING, 2=PRODUCER_BUYBACK
    scope                 INTEGER        NOT NULL,   -- TariffScope: 1=GLOBAL, 2=SEGMENT, 3=EAN
    scope_segment         INTEGER,                   -- client_type when scope=SEGMENT
    scope_ean             VARCHAR(64),               -- EAN when scope=EAN
    price_per_kwh         NUMERIC(12, 6) NOT NULL,
    currency              VARCHAR(3)     NOT NULL DEFAULT 'EUR',
    valid_from            DATE           NOT NULL,
    valid_to              DATE,
    label                 VARCHAR(255),
    created_at            TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_tariff_lookup
    ON tariff (id_community, id_sharing_operation, kind, scope);
CREATE UNIQUE INDEX IF NOT EXISTS uq_tariff_global
    ON tariff (id_community, id_sharing_operation, kind, valid_from)
    WHERE scope = 1;
CREATE UNIQUE INDEX IF NOT EXISTS uq_tariff_segment
    ON tariff (id_community, id_sharing_operation, kind, scope_segment, valid_from)
    WHERE scope = 2;
CREATE UNIQUE INDEX IF NOT EXISTS uq_tariff_ean
    ON tariff (id_community, id_sharing_operation, kind, scope_ean, valid_from)
    WHERE scope = 3;

DROP TRIGGER IF EXISTS trg_tariff_updated_at ON tariff;
CREATE TRIGGER trg_tariff_updated_at BEFORE UPDATE ON tariff
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- billing_run -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS billing_run (
    id                    SERIAL         PRIMARY KEY,
    id_community          INTEGER        NOT NULL,
    id_sharing_operation  INTEGER        NOT NULL,
    period_start          DATE           NOT NULL,
    period_end            DATE           NOT NULL,
    status                INTEGER        NOT NULL,   -- BillingRunStatus
    regulator             VARCHAR(32)    NOT NULL,
    kwh_scale             NUMERIC(12, 6) NOT NULL DEFAULT 1,
    warnings              JSONB,
    error_message         TEXT,
    created_at            TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_billing_run_op_period
        UNIQUE (id_community, id_sharing_operation, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS ix_billing_run_status ON billing_run (id_community, status);

DROP TRIGGER IF EXISTS trg_billing_run_updated_at ON billing_run;
CREATE TRIGGER trg_billing_run_updated_at BEFORE UPDATE ON billing_run
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- settlement_snapshot ---------------------------------------------------
-- Frozen per-(EAN, member) volumes: the reproducible pricing input. A meter
-- that changed owner mid-period yields one row per owner, each carrying only
-- the volume read during that owner's meter_data window; id_member NULL holds
-- orphan volume (readings covered by no ownership window — never invoiced).
-- row_count vs distinct_ts_count detects a duplicate settlement import.
CREATE TABLE IF NOT EXISTS settlement_snapshot (
    id                 SERIAL         PRIMARY KEY,
    id_community       INTEGER        NOT NULL,
    id_billing_run     INTEGER        NOT NULL REFERENCES billing_run (id) ON DELETE CASCADE,
    ean                VARCHAR(64)    NOT NULL,
    direction          INTEGER        NOT NULL,   -- BillingDirection: 1=CONSUMER, 2=PRODUCER
    client_type        INTEGER,                   -- segment, frozen for SEGMENT pricing
    shared_kwh         NUMERIC(18, 6) NOT NULL DEFAULT 0,
    inj_shared_kwh     NUMERIC(18, 6) NOT NULL DEFAULT 0,
    row_count          INTEGER        NOT NULL DEFAULT 0,
    distinct_ts_count  INTEGER        NOT NULL DEFAULT 0,
    id_member          INTEGER,
    -- Ownership window clamped to the run period; only set when it is a strict
    -- subset of the period (drives the invoice-line date-range suffix).
    owned_from         DATE,
    owned_to           DATE,
    created_at         TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_settlement_snapshot_run_ean_dir_member
        UNIQUE NULLS NOT DISTINCT (id_billing_run, ean, direction, id_member)
);

-- ---- invoice ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoice (
    id                   SERIAL         PRIMARY KEY,
    id_community         INTEGER        NOT NULL,
    id_billing_run       INTEGER        NOT NULL REFERENCES billing_run (id),
    id_member            INTEGER        NOT NULL,
    type                 INTEGER        NOT NULL,  -- InvoiceType: 1=INVOICE, 2=CREDIT_NOTE, 3=PRODUCER_STATEMENT
    status               INTEGER        NOT NULL,  -- InvoiceStatus
    number               VARCHAR(32),
    legal_entity_key     VARCHAR(64)    NOT NULL,
    currency             VARCHAR(3)     NOT NULL DEFAULT 'EUR',
    subtotal             NUMERIC(14, 2) NOT NULL DEFAULT 0,
    vat_rate             NUMERIC(8, 6)  NOT NULL DEFAULT 0,
    vat_amount           NUMERIC(14, 2) NOT NULL DEFAULT 0,
    total                NUMERIC(14, 2) NOT NULL DEFAULT 0,
    structured_comm      VARCHAR(20),
    issued_at            TIMESTAMPTZ,
    due_date             DATE,
    sent_at              TIMESTAMPTZ,
    paid_at              TIMESTAMPTZ,
    corrects_invoice_id  INTEGER,
    artifact_uri         VARCHAR(512),
    artifact_sha256      VARCHAR(64),
    docgen_request_id    VARCHAR(64),
    created_at           TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

-- Gapless numbering backstop: at most one row per (legal entity, number) once
-- numbered. Partial so DRAFTs (number IS NULL) never collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoice_number
    ON invoice (legal_entity_key, number)
    WHERE number IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_invoice_status ON invoice (id_community, status);
CREATE INDEX IF NOT EXISTS ix_invoice_issued ON invoice (id_community, issued_at);
CREATE INDEX IF NOT EXISTS ix_invoice_run ON invoice (id_billing_run);
CREATE INDEX IF NOT EXISTS ix_invoice_docgen ON invoice (docgen_request_id);

DROP TRIGGER IF EXISTS trg_invoice_updated_at ON invoice;
CREATE TRIGGER trg_invoice_updated_at BEFORE UPDATE ON invoice
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- ---- invoice_line ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoice_line (
    id            SERIAL         PRIMARY KEY,
    id_community  INTEGER        NOT NULL,
    id_invoice    INTEGER        NOT NULL REFERENCES invoice (id) ON DELETE CASCADE,
    ean           VARCHAR(64)    NOT NULL,
    direction     INTEGER        NOT NULL,   -- BillingDirection
    measure       INTEGER        NOT NULL,   -- Measure: 1=SHARED, 2=INJ_SHARED
    quantity_kwh  NUMERIC(18, 6) NOT NULL,
    unit_price    NUMERIC(12, 6) NOT NULL,
    amount        NUMERIC(14, 2) NOT NULL,
    description   VARCHAR(512),
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_invoice_line_invoice ON invoice_line (id_invoice);

-- ---- credit_note -----------------------------------------------------------
CREATE TABLE IF NOT EXISTS credit_note (
    id                   SERIAL      PRIMARY KEY,
    id_community         INTEGER     NOT NULL,
    id_original_invoice  INTEGER     NOT NULL REFERENCES invoice (id),
    id_credit_invoice    INTEGER     NOT NULL REFERENCES invoice (id),
    reason               TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_credit_note_pair UNIQUE (id_original_invoice, id_credit_invoice)
);

-- ---- payment ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS payment (
    id            SERIAL         PRIMARY KEY,
    id_community  INTEGER        NOT NULL,
    id_invoice    INTEGER        NOT NULL REFERENCES invoice (id),
    amount        NUMERIC(14, 2) NOT NULL,
    currency      VARCHAR(3)     NOT NULL DEFAULT 'EUR',
    method        INTEGER        NOT NULL,   -- PaymentMethod
    reference     VARCHAR(64),
    paid_at       TIMESTAMPTZ    NOT NULL,
    created_at    TIMESTAMPTZ    NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_payment_invoice ON payment (id_invoice);

-- ---- invoice_sequence ------------------------------------------------------
-- Gapless counter, one row per (community, legal entity, year). The
-- legal_entity_key encodes the document series (…:INV / :CN / :PS) so each
-- series numbers independently. Incremented under SELECT … FOR UPDATE at issue.
CREATE TABLE IF NOT EXISTS invoice_sequence (
    id                SERIAL      PRIMARY KEY,
    id_community      INTEGER     NOT NULL,
    legal_entity_key  VARCHAR(64) NOT NULL,
    year              INTEGER     NOT NULL,
    last_value        BIGINT      NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_invoice_sequence_scope UNIQUE (id_community, legal_entity_key, year)
);

DROP TRIGGER IF EXISTS trg_invoice_sequence_updated_at ON invoice_sequence;
CREATE TRIGGER trg_invoice_sequence_updated_at BEFORE UPDATE ON invoice_sequence
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
