"""Billing HTTP routes (spec §9).

Every route is gated by ``resolve_internal_community`` (caches the internal
community id in a ContextVar) + ``require_feature(BILLING)`` and wrapped by
``with_default_error`` so an unexpected failure surfaces as a domain error.
Tariff/invoice/credit-note write flows beyond run creation land in Phase 5-6.

NOTE: this module intentionally does NOT `from __future__ import annotations`.
The ``with_default_error`` wrapper resolves string annotations against its own
module globals, so stringified body/dependency types would be invisible to
FastAPI (bodies get demoted to query params). Real annotation objects avoid that.
"""

import datetime
import math

from fastapi import APIRouter, Depends, Query, Response

from api.billing.deps import get_billing_service
from api.billing.schemas import (
    BillingRunOut,
    BillingRunRequest,
    CreditNoteIn,
    InvoiceOut,
    IssueOut,
    OverdueSweepOut,
    PaymentIn,
    PaymentOut,
    PdfRenderIn,
    RenderRequestOut,
    TariffIn,
    TariffOut,
)
from api.billing.service import BillingService
from core.api_response import ApiResponse, ApiResponsePaginated, Pagination
from core.errors.with_default_error import with_default_error
from core.security.community_scope import resolve_internal_community
from core.security.dependencies import require_feature, require_min_role
from core.security.user_context import Role
from shared.const import FeatureName
from shared.custom_errors import errors

billing_routes = APIRouter(
    dependencies=[
        Depends(resolve_internal_community),
        Depends(require_feature(FeatureName.BILLING)),
    ]
)


# ---- tariffs ---------------------------------------------------------------
@billing_routes.post("/sharing-operations/{operation_id}/tariffs")
@with_default_error(errors.billing.CREATE_TARIFF)
async def create_tariff(
    operation_id: int,
    body: TariffIn,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[TariffOut]:
    tariff = await service.create_tariff(id_sharing_operation=operation_id, body=body)
    return ApiResponse(data=tariff)


@billing_routes.get("/sharing-operations/{operation_id}/tariffs")
@with_default_error(errors.billing.GET_TARIFFS)
async def list_tariffs(
    operation_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[list[TariffOut]]:
    tariffs = await service.list_tariffs(id_sharing_operation=operation_id)
    return ApiResponse(data=tariffs)


@billing_routes.delete("/tariffs/{tariff_id}")
@with_default_error(errors.billing.DELETE_TARIFF)
async def delete_tariff(
    tariff_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[bool]:
    await service.delete_tariff(tariff_id=tariff_id)
    return ApiResponse(data=True)


# ---- billing runs ----------------------------------------------------------
@billing_routes.post("/sharing-operations/{operation_id}/billing-runs")
@with_default_error(errors.billing.START_BILLING_RUN)
async def create_billing_run(
    operation_id: int,
    body: BillingRunRequest,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[BillingRunOut]:
    run = await service.create_billing_run(id_sharing_operation=operation_id, body=body)
    return ApiResponse(data=run)


@billing_routes.get("/sharing-operations/{operation_id}/billing-runs")
@with_default_error(errors.billing.GET_BILLING_RUNS)
async def list_billing_runs(
    operation_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[list[BillingRunOut]]:
    runs = await service.list_runs(id_sharing_operation=operation_id)
    return ApiResponse(data=runs)


@billing_routes.get("/billing-runs/{run_id}")
@with_default_error(errors.billing.GET_BILLING_RUNS)
async def get_billing_run(
    run_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[BillingRunOut]:
    run = await service.get_run(run_id=run_id)
    return ApiResponse(data=run)


@billing_routes.get("/billing-runs/{run_id}/invoices")
@with_default_error(errors.billing.GET_INVOICES)
async def get_billing_run_invoices(
    run_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[list[InvoiceOut]]:
    invoices = await service.get_run_invoices(run_id=run_id)
    return ApiResponse(data=invoices)


@billing_routes.post("/billing-runs/overdue-sweep")
@with_default_error(errors.billing.GET_BILLING_RUNS)
async def overdue_sweep(
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[OverdueSweepOut]:
    return ApiResponse(data=await service.sweep_overdue())


# ---- invoices --------------------------------------------------------------
# NOTE: /invoices/mine MUST be declared before /invoices/{invoice_id}, else
# FastAPI matches "mine" against the int path param and 422s.
@billing_routes.get("/invoices/mine")
@with_default_error(errors.billing.GET_INVOICES)
async def list_my_invoices(
    service: BillingService = Depends(get_billing_service),
    status: int | None = Query(default=None),
    issued_from: datetime.date | None = Query(default=None),
    issued_to: datetime.date | None = Query(default=None),
    sort: str | None = Query(default=None),
    order: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> ApiResponsePaginated[list[InvoiceOut]]:
    invoices, total = await service.list_my_invoices(
        status=status,
        issued_from=issued_from,
        issued_to=issued_to,
        sort=sort,
        order=order,
        limit=limit,
        offset=(page - 1) * limit,
    )
    total_pages = math.ceil(total / limit) if total else 0
    return ApiResponsePaginated(
        data=invoices,
        pagination=Pagination(page=page, limit=limit, total=total, total_pages=total_pages),
    )


@billing_routes.get("/invoices/{invoice_id}")
@with_default_error(errors.billing.GET_INVOICES)
async def get_invoice(
    invoice_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[InvoiceOut]:
    invoice = await service.get_invoice(invoice_id=invoice_id)
    return ApiResponse(data=invoice)


@billing_routes.post("/invoices/{invoice_id}/issue")
@with_default_error(errors.billing.ISSUE_INVOICE)
async def issue_invoice(
    invoice_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[IssueOut]:
    result = await service.issue_invoice(invoice_id=invoice_id)
    return ApiResponse(data=result)


@billing_routes.post("/invoices/{invoice_id}/send")
@with_default_error(errors.billing.INVOICE_NOT_ISSUED)
async def send_invoice(
    invoice_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[InvoiceOut]:
    return ApiResponse(data=await service.send_invoice(invoice_id=invoice_id))


@billing_routes.post("/invoices/{invoice_id}/payments")
@with_default_error(errors.billing.REGISTER_PAYMENT)
async def register_payment(
    invoice_id: int,
    body: PaymentIn,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[InvoiceOut]:
    return ApiResponse(data=await service.register_payment(invoice_id=invoice_id, body=body))


@billing_routes.get("/invoices/{invoice_id}/payments")
@with_default_error(errors.billing.GET_INVOICES)
async def list_payments(
    invoice_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[list[PaymentOut]]:
    return ApiResponse(data=await service.list_payments(invoice_id=invoice_id))


@billing_routes.post("/invoices/{invoice_id}/credit-note")
@with_default_error(errors.billing.CREDIT_NOTE_TARGET_INVALID)
async def create_credit_note(
    invoice_id: int,
    body: CreditNoteIn,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[InvoiceOut]:
    return ApiResponse(data=await service.create_credit_note(invoice_id=invoice_id, body=body))


# ---- invoice PDF (generate / download / delete) ----------------------------
# Generation is async (docgen over NATS): this returns immediately; poll the
# invoice's ``pdf_ready`` until the render lands. Manager-only.
@billing_routes.post(
    "/invoices/{invoice_id}/pdf",
    dependencies=[Depends(require_min_role(Role.MANAGER))],
)
@with_default_error(errors.billing.ISSUE_INVOICE)
async def generate_invoice_pdf(
    invoice_id: int,
    body: PdfRenderIn | None = None,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[RenderRequestOut]:
    force = body.force if body else False
    result = await service.request_render(invoice_id=invoice_id, force=force)
    return ApiResponse(data=result)


# Download the rendered PDF bytes. Managers get any invoice in the community;
# a plain member only their own (authorised in the service). The application/pdf
# response makes swagger2krakend emit ``no-op`` so KrakenD streams raw bytes.
@billing_routes.get(
    "/invoices/{invoice_id}/pdf",
    responses={200: {"content": {"application/pdf": {}}, "description": "Invoice PDF"}},
)
@with_default_error(errors.billing.GET_INVOICES)
async def download_invoice_pdf(
    invoice_id: int,
    service: BillingService = Depends(get_billing_service),
) -> Response:
    content, filename = await service.download_invoice_pdf(invoice_id=invoice_id)
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Remove the generated PDF (file + reference); the invoice record is untouched.
# Blocked once the invoice has been sent. Manager-only.
@billing_routes.delete(
    "/invoices/{invoice_id}/pdf",
    dependencies=[Depends(require_min_role(Role.MANAGER))],
)
@with_default_error(errors.billing.INVOICE_PDF_DELETE_FORBIDDEN)
async def delete_invoice_pdf(
    invoice_id: int,
    service: BillingService = Depends(get_billing_service),
) -> ApiResponse[bool]:
    await service.delete_invoice_pdf(invoice_id=invoice_id)
    return ApiResponse(data=True)


@billing_routes.get("/invoices")
@with_default_error(errors.billing.GET_INVOICES)
async def list_invoices(
    service: BillingService = Depends(get_billing_service),
    status: int | None = Query(default=None),
    participant: int | None = Query(default=None),
    issued_from: datetime.date | None = Query(default=None),
    issued_to: datetime.date | None = Query(default=None),
    sort: str | None = Query(default=None),
    order: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=50, ge=1, le=200),
) -> ApiResponsePaginated[list[InvoiceOut]]:
    invoices, total = await service.list_invoices(
        status=status,
        id_member=participant,
        issued_from=issued_from,
        issued_to=issued_to,
        sort=sort,
        order=order,
        limit=limit,
        offset=(page - 1) * limit,
    )
    total_pages = math.ceil(total / limit) if total else 0
    return ApiResponsePaginated(
        data=invoices,
        pagination=Pagination(page=page, limit=limit, total=total, total_pages=total_pages),
    )
