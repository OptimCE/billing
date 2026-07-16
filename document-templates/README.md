# Billing document templates

Jinja-HTML template bundles for the `document-generation` service. Each version
directory (`.../v1/`) is uploaded as-is to the **`optimce-templates`** S3 bucket
under the same path, and referenced by the billing service via
`INVOICE_TEMPLATE_URI` / `PRODUCER_STATEMENT_TEMPLATE_URI` /
`INVOICE_PROFORMA_TEMPLATE_URI` (`s3://optimce-templates/billing/<kind>/v1/`).

Bundles:

- `billing/invoice/v1/` — consumer invoice (`billing.invoice`).
- `billing/producer_statement/v1/` — producer remuneration statement (`billing.producer_statement`).
- `billing/invoice_proforma/v1/` — watermarked proforma for DRAFT consumer invoices
  (`billing.invoice_proforma`); same payload as `billing.invoice` but `number`/`issue_date`
  are optional (a draft has no legal number yet).

Each bundle has a `manifest.json` (engine `jinja-html`, `entrypoint`,
`supported_formats`, and a `required_fields` JSON Schema) and a `template.html`.

**Contract:** the `required_fields` schema MUST stay in lockstep with
`api/billing/mappers.py::build_docgen_data` — a mismatch is a permanent
`VALIDATION_ERROR` at render time. When you change the payload shape, bump the
version directory (`v2/`) and the corresponding `*_TEMPLATE_URI` setting.

To deploy: upload the version directory to the bucket, then point the setting at
the new prefix. The billing service never reads these files at runtime.
