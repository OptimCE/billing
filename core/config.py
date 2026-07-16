import os
from enum import StrEnum

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# CORS contract:
# - Local/test: ALLOW_ORIGIN may default to "*".
# - Staging/production: ALLOW_ORIGIN is REQUIRED, must not contain "*", and may
#   be a comma-separated list (e.g. "https://app.example.com,https://admin.example.com").
# Enforced in Settings.validate_env_config below; example values live in
# .env.staging.exemple and .env.production.exemple.
class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    STAGING = "staging"
    PRODUCTION = "production"


def _get_env_file() -> str:
    env = os.getenv("ENV", "local").strip()
    return f".env.{env}"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_get_env_file(),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ---- Core ----
    ENV: Environment = Environment.LOCAL

    # ---- CRM Database ----
    CRM_DATABASE_URL: str  # postgresql+asyncpg://...
    CRM_DB_POOL_SIZE: int = 20
    CRM_DB_MAX_OVERFLOW: int = 10
    CRM_DB_POOL_RECYCLE: int = 3600  # seconds — recycle connections after 1 hour
    CRM_DB_POOL_TIMEOUT: int = 30  # seconds — wait for available connection
    CRM_DB_SSL: bool = False  # enable SSL/TLS for database connection

    # ---- LOCAL Database ----
    LOCAL_DATABASE_URL: str  # postgresql+asyncpg://...
    LOCAL_DB_POOL_SIZE: int = 20
    LOCAL_DB_MAX_OVERFLOW: int = 10
    LOCAL_DB_POOL_RECYCLE: int = 3600  # seconds — recycle connections after 1 hour
    LOCAL_DB_POOL_TIMEOUT: int = 30  # seconds — wait for available connection
    LOCAL_DB_SSL: bool = False  # enable SSL/TLS for database connection
    # ---- NATS ----
    NATS_URL: str = ""

    # ---- Storage (S3-compatible, MinIO in dev) ----
    # Endpoint URL of the S3-compatible storage server (e.g. http://minio:9000).
    # Empty in local/test where the storage module is mocked.
    STORAGE_ENDPOINT: str = ""
    STORAGE_BUCKET: str = "crm-files"
    STORAGE_ACCESS_KEY: str = ""
    STORAGE_SECRET_KEY: str = ""
    # MinIO ignores region but botocore still requires it to sign requests.
    STORAGE_REGION: str = "us-east-1"

    # ---- CORS ----
    ALLOW_ORIGIN: str = "*"

    LOGGING_TOKEN: str = ""
    LOGGING_TRACES_URL: str = ""
    LOGGING_LOGS_URL: str = ""
    LOGGING_METRICS_URL: str = ""

    # ---- Regulator registry / billing regimes ----
    # REGULATORS_CONFIG_PATH points at the shared reference/regulators.json
    # (the cross-service registry of which regulators exist / are active), read
    # read-only for the startup parity assertion. BILLING_REGIMES_CONFIG_PATH
    # points at the billing-local declarative config (VAT, due days, number
    # format, legal mentions) keyed by the same regulator code. Both fall back
    # to bundled defaults resolved by regime/config_loader.py when left empty.
    REGULATORS_CONFIG_PATH: str = ""
    BILLING_REGIMES_CONFIG_PATH: str = ""

    # ---- Settlement units ----
    # meter_consumption.shared / inj_shared are stored in kWh. KWH_SCALE lets a
    # deployment correct the unit (e.g. 0.001 if values ever arrive in Wh)
    # without a code change; the value is recorded in each run's audit payload.
    KWH_SCALE: float = 1.0

    # ---- Document generation (async request/reply over NATS) ----
    DOCGEN_REQUEST_SUBJECT: str = "docgen.request"
    DOCGEN_RESULT_SUBJECT: str = "docgen.result.billing"
    DOCGEN_PRESIGN_TTL: int = 3600
    TEMPLATES_BUCKET: str = "optimce-templates"
    OUTPUT_BUCKET: str = "optimce-documents"
    INVOICE_TEMPLATE_URI: str = "s3://optimce-templates/billing/invoice/v1/"
    PRODUCER_STATEMENT_TEMPLATE_URI: str = "s3://optimce-templates/billing/producer_statement/v1/"
    # Watermarked proforma variant rendered for DRAFT invoices (no legal number).
    INVOICE_PROFORMA_TEMPLATE_URI: str = "s3://optimce-templates/billing/invoice_proforma/v1/"

    # ---- Localization ----
    DEFAULT_LOCALE: str = "fr-BE"

    @model_validator(mode="after")
    def validate_env_config(self) -> "Settings":
        if self.ENV != Environment.LOCAL:
            origins = [o.strip() for o in self.ALLOW_ORIGIN.split(",") if o.strip()]
            if not origins:
                raise ValueError(
                    "ALLOW_ORIGIN is required when ENV is not local; "
                    "set it explicitly in .env.{env} (no implicit fallback to '*')"
                )
            if "*" in self.ALLOW_ORIGIN:
                raise ValueError("Wildcard CORS not allowed in staging/production")
            if not self.CRM_DATABASE_URL.strip():
                raise ValueError("CRM_DATABASE_URL is required when ENV is not local")
            if not self.LOCAL_DATABASE_URL.strip():
                raise ValueError("LOCAL_DATABASE_URL is required when ENV is not local")
        if self.ENV in (Environment.STAGING, Environment.PRODUCTION):
            if not self.NATS_URL.strip():
                raise ValueError("NATS_URL is required in staging/production")
            if not self.STORAGE_ENDPOINT.strip():
                raise ValueError("STORAGE_ENDPOINT is required in staging/production")
            if not self.STORAGE_ACCESS_KEY.strip():
                raise ValueError("STORAGE_ACCESS_KEY is required in staging/production")
            if not self.STORAGE_SECRET_KEY.strip():
                raise ValueError("STORAGE_SECRET_KEY is required in staging/production")
        if self.ENV == Environment.PRODUCTION:
            if not self.LOGGING_TOKEN:
                raise ValueError("LOGGING_TOKEN required for staging/production")
            if not self.LOGGING_LOGS_URL:
                raise ValueError("LOGGING_LOGS_URL required for staging/production")
            if not self.LOGGING_METRICS_URL:
                raise ValueError("LOGGING_METRICS_URL required for staging/production")
        return self


settings = Settings()
