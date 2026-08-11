-- Test-only DDL for the CRM tables this service READS (and the audit_log and
-- notification rows it writes). The real CRM schema is owned by crm-backend; we
-- mirror only the
-- minimum columns the billing suite needs, using identical column names so the
-- CrmCoreReadPort queries run unchanged against the production CRM DB.

-- ---- community (identity + bank/legal + regulator) -------------------------
-- Mirrors crm-backend community. Billing fields are nullable except regulator.
CREATE TABLE IF NOT EXISTS community (
    id                       INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                     VARCHAR(255) NOT NULL UNIQUE,
    auth_community_id        VARCHAR(255) NOT NULL UNIQUE,
    regulator                VARCHAR(32)  NOT NULL DEFAULT 'BE-WAL-CWAPE',
    vat_number               VARCHAR(32),
    legal_name               VARCHAR(255),
    iban                     VARCHAR(34),
    account_holder_name      VARCHAR(255),
    headquarters_address_id  INTEGER,
    created_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at               TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS community_subscription (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_community INTEGER     NOT NULL,
    feature      VARCHAR(64) NOT NULL,
    is_active    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_community_subscription_community_feature UNIQUE (id_community, feature)
);

CREATE INDEX IF NOT EXISTS idx_community_subscription_id_community
    ON community_subscription (id_community);

-- ---- address ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS address (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    street       VARCHAR(255),
    number       INTEGER,  -- matches the real CRM: house number is an integer column
    postcode     VARCHAR(16),
    supplement   VARCHAR(255),
    city         VARCHAR(255),
    id_community INTEGER,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---- sharing_operation -----------------------------------------------------
CREATE TABLE IF NOT EXISTS sharing_operation (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         VARCHAR(255) NOT NULL,
    type         INTEGER,
    is_public    BOOLEAN NOT NULL DEFAULT FALSE,
    id_community INTEGER NOT NULL,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ---- manager / member / individual / company -------------------------------
CREATE TABLE IF NOT EXISTS manager (
    id           INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name         VARCHAR(255),
    email        VARCHAR(255),
    id_community INTEGER,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS member (
    id                  INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name                VARCHAR(255) NOT NULL,
    member_type         INTEGER NOT NULL,   -- 1=INDIVIDUAL, 2=COMPANY
    status              INTEGER,
    iban                VARCHAR(255),
    id_home_address     INTEGER,
    id_billing_address  INTEGER,
    id_community        INTEGER NOT NULL,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS individual (
    id            INTEGER PRIMARY KEY REFERENCES member (id),
    first_name    VARCHAR(255),
    nrn           VARCHAR(255),
    email         VARCHAR(255),
    phone_number  VARCHAR(255),
    social_rate   BOOLEAN NOT NULL DEFAULT FALSE,
    id_manager    INTEGER
);

CREATE TABLE IF NOT EXISTS company (
    id          INTEGER PRIMARY KEY REFERENCES member (id),
    vat_number  VARCHAR(255),
    id_manager  INTEGER
);

-- ---- meter / meter_data / meter_consumption --------------------------------
CREATE TABLE IF NOT EXISTS meter (
    ean                VARCHAR(64) PRIMARY KEY,
    meter_number       VARCHAR(255),
    id_address         INTEGER,
    tarif_group        INTEGER,
    phases_number      INTEGER,
    reading_frequency  INTEGER,
    id_community       INTEGER NOT NULL,
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meter_data (
    id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ean                   VARCHAR(64) NOT NULL REFERENCES meter (ean),
    id_member             INTEGER,
    id_sharing_operation  INTEGER,
    status                INTEGER,   -- 1=ACTIVE
    client_type           INTEGER,   -- 1=Résidentiel, 2=Professionnel, 3=Industriel
    injection_status      INTEGER,
    production_chain      INTEGER,
    start_date            DATE,
    end_date              DATE,
    id_community          INTEGER NOT NULL,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_meter_data_op ON meter_data (id_sharing_operation);

CREATE TABLE IF NOT EXISTS meter_consumption (
    id                    INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ean                   VARCHAR(64) NOT NULL REFERENCES meter (ean),
    id_sharing_operation  INTEGER,
    timestamp             TIMESTAMPTZ NOT NULL,
    gross                 DOUBLE PRECISION,
    net                   DOUBLE PRECISION,
    shared                DOUBLE PRECISION,
    inj_gross             DOUBLE PRECISION,
    inj_shared            DOUBLE PRECISION,
    inj_net               DOUBLE PRECISION,
    id_community          INTEGER NOT NULL,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_meter_consumption_lookup
    ON meter_consumption (id_sharing_operation, timestamp);

-- ---- app_user (audit writer identity) --------------------------------------
CREATE TABLE IF NOT EXISTS app_user (
    id            INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    auth_user_id  VARCHAR(255) NOT NULL UNIQUE,
    email         VARCHAR(256) NOT NULL,
    -- Preferred language, resolved onto every queued email at enqueue time.
    locale        VARCHAR(8)   NULL,
    -- Denormalised onto the queued row as the recipient display name.
    first_name    TEXT         NULL,
    last_name     TEXT         NULL
);

-- ---- user_member_link (auth user ↔ member; for caller-scoped "my invoices") --
-- Mirrors the real CRM table: the link carries no id_community (community scope
-- comes from member.id_community).
CREATE TABLE IF NOT EXISTS user_member_link (
    id          INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_user     INTEGER NOT NULL REFERENCES app_user (id),
    id_member   INTEGER NOT NULL,
    created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_user_member_link_member ON user_member_link (id_member);
CREATE INDEX IF NOT EXISTS idx_user_member_link_user ON user_member_link (id_user);

-- ---- audit_log (written by this service) -----------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_community INTEGER REFERENCES community (id) ON DELETE CASCADE,
    timestamp    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    action       VARCHAR(128) NOT NULL,
    source       VARCHAR(32)  NOT NULL,
    entity_type  VARCHAR(64)  NOT NULL,
    entity_id    VARCHAR(64),
    user_id      INTEGER,
    user_email   VARCHAR(256),
    payload      JSONB        NOT NULL DEFAULT '{}'::jsonb
);

-- ---- community_user (read by this service) ---------------------------------
-- Mirrors crm-backend's community_user join table. Read to narrow a
-- notification fan-out to a community's managers.
CREATE TABLE IF NOT EXISTS community_user (
    id_community INTEGER REFERENCES community (id) ON DELETE CASCADE,
    id_user      INTEGER REFERENCES app_user (id) ON DELETE CASCADE,
    role         VARCHAR(50) NOT NULL,
    PRIMARY KEY (id_community, id_user)
);

-- ---- notification (written by this service) --------------------------------
-- Mirrors crm-backend's production DDL. This service only INSERTs one row per
-- recipient through core/notifications; reads are served by crm-backend.
CREATE TABLE IF NOT EXISTS notification (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_community INTEGER REFERENCES community (id) ON DELETE CASCADE,
    id_user      INTEGER NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    type         VARCHAR(128) NOT NULL,
    data         JSONB        NOT NULL DEFAULT '{}'::jsonb,
    read_at      TIMESTAMPTZ,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- ---- notification delivery layer (written / read by this service) ----------
-- Mirrors crm-backend/database_script/2026-08-03_notification_delivery.sql.
-- `core/notifications` writes one outbound_message per emailable recipient and
-- reads notification_preference to decide what is deliverable. Sending and the
-- suppression check belong to the notification-dispatch worker; email_suppression
-- is mirrored here only so the schema stays a faithful copy.
CREATE TABLE IF NOT EXISTS outbound_message (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    id_notification BIGINT NULL REFERENCES notification (id) ON DELETE SET NULL,
    id_community    INTEGER NULL REFERENCES community (id) ON DELETE CASCADE,
    channel         SMALLINT     NOT NULL CHECK (channel IN (1, 2)),
    recipient       VARCHAR(320) NOT NULL,
    recipient_name  VARCHAR(255) NULL,
    locale          VARCHAR(8)   NOT NULL DEFAULT '',
    type            VARCHAR(128) NOT NULL,
    category        SMALLINT     NOT NULL CHECK (category IN (1, 2)),
    data            JSONB        NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key      VARCHAR(200) NOT NULL,
    status          SMALLINT     NOT NULL DEFAULT 1 CHECK (status IN (1, 2, 3, 4, 5)),
    attempts        SMALLINT     NOT NULL DEFAULT 0,
    last_error      TEXT         NULL,
    scheduled_for   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    claimed_at      TIMESTAMPTZ  NULL,
    sent_at         TIMESTAMPTZ  NULL,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbound_message_dedupe
    ON outbound_message (dedupe_key);
CREATE INDEX IF NOT EXISTS ix_outbound_message_due
    ON outbound_message (scheduled_for) WHERE status = 1;
CREATE INDEX IF NOT EXISTS ix_outbound_message_stale
    ON outbound_message (claimed_at) WHERE status = 5;

CREATE TABLE IF NOT EXISTS email_suppression (
    email      VARCHAR(320) PRIMARY KEY,
    reason     SMALLINT     NOT NULL CHECK (reason IN (1, 2, 3, 4)),
    detail     TEXT         NULL,
    created_at TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS notification_preference (
    id_user     INTEGER      NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    type_prefix VARCHAR(128) NOT NULL,
    channel     SMALLINT     NOT NULL CHECK (channel IN (1, 2)),
    mode        SMALLINT     NOT NULL CHECK (mode IN (1, 3)),

    PRIMARY KEY (id_user, type_prefix, channel)
);
