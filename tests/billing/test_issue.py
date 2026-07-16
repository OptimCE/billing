"""Issue flow: gapless number + OGM + docgen render request + artifact attach."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

import main
from api.billing.deps import get_event_publisher
from ports.document_generation import DocgenRequest
from tests.factories import crm_billing_factory as f
from utils import ogm
from worker import docgen_results, issue, persistence

_AUTH = "issue-test-org"


def _headers() -> dict[str, str]:
    return {
        "x-user-id": "u1",
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[ADMIN]]",
    }


class _FakePublisher:
    async def publish(self, subject: str, event) -> None:
        return None


class _FakeDocGen:
    def __init__(self) -> None:
        self.requests: list[DocgenRequest] = []

    async def request_render(self, request: DocgenRequest) -> None:
        self.requests.append(request)


async def _seed_run_and_draft(client, db_session) -> tuple[int, dict]:
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban="BE68539007547034", legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member = await f.create_member(
        db_session, id_community=cid, name="Alice Dupont", member_type=1, iban="BE71096123456769"
    )
    await f.create_individual(db_session, id_member=member, email="alice@example.be")
    await f.create_meter(db_session, ean="EAN-C", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-C", id_community=cid, id_sharing_operation=op,
        id_member=member, client_type=1, start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_consumption(
        db_session, ean="EAN-C", id_community=cid, id_sharing_operation=op,
        timestamp=f.june(5), shared=30.0,
    )
    resp = await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_headers(),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    resp = await client.get(f"/billing-runs/{run_id}/invoices", headers=_headers())
    return cid, resp.json()["data"][0]


async def test_issue_assigns_number_ogm_then_renders(client, db_session):
    cid, invoice = await _seed_run_and_draft(client, db_session)
    invoice_id = invoice["id"]
    assert invoice["number"] is None
    assert invoice["status"] == 0  # DRAFT

    resp = await client.post(f"/invoices/{invoice_id}/issue", headers=_headers())
    assert resp.status_code == 200, resp.text
    issued = resp.json()["data"]

    year = datetime.datetime.now(datetime.UTC).astimezone(ZoneInfo("Europe/Brussels")).year
    assert issued["number"] == f"F-{year}-00001"
    assert issued["status"] == 1  # ISSUED
    assert issued["due_date"] is not None
    assert ogm.validate(issued["structured_comm"])

    # Immutable once issued.
    resp = await client.post(f"/invoices/{invoice_id}/issue", headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2222  # INVOICE_NOT_DRAFT

    # Worker builds the docgen request from the issued invoice.
    fake = _FakeDocGen()
    await issue.process_issue(
        invoice_id, doc_port=fake, local_session=db_session, crm_session=db_session
    )
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req.tenant_id == str(cid)
    assert req.template_uri.endswith("/invoice/v1/")
    assert req.reply_to == "docgen.result.billing"
    assert req.data["invoice"]["number"] == f"F-{year}-00001"
    assert req.data["seller"]["iban"] == "BE68539007547034"
    assert req.data["buyer"]["name"] == "Alice Dupont"
    assert req.data["lines"][0]["ean"] == "EAN-C"
    assert req.metadata["invoice_id"] == invoice_id
    assert req.request_id

    # docgen result → PDF attached to the invoice.
    result_body = {
        "request_id": req.request_id,
        "tenant_id": str(cid),
        "status": "success",
        "artifacts": [
            {
                "format": "pdf",
                "uri": "s3://optimce-documents/billing/invoices/2026/07/inv/invoice.pdf",
                "sha256": "deadbeef",
                "size_bytes": 8421,
            }
        ],
    }
    outcome = await docgen_results.process_docgen_result(
        result_body, local_session=db_session, crm_session=db_session
    )
    assert outcome == "attached"

    from shared.models.local_models import InvoiceModel

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert fresh.artifact_uri.endswith("invoice.pdf")
    assert fresh.artifact_sha256 == "deadbeef"

    # Idempotent: re-delivering the result and re-running issue are no-ops.
    assert (
        await docgen_results.process_docgen_result(
            result_body, local_session=db_session, crm_session=db_session
        )
        == "attached"
    )
    await issue.process_issue(
        invoice_id, doc_port=fake, local_session=db_session, crm_session=db_session
    )
    assert len(fake.requests) == 1  # no second render requested


async def test_docgen_permanent_error_marks_render_failed(client, db_session):
    cid, invoice = await _seed_run_and_draft(client, db_session)
    invoice_id = invoice["id"]
    await client.post(f"/invoices/{invoice_id}/issue", headers=_headers())

    fake = _FakeDocGen()
    await issue.process_issue(
        invoice_id, doc_port=fake, local_session=db_session, crm_session=db_session
    )
    request_id = fake.requests[0].request_id

    outcome = await docgen_results.process_docgen_result(
        {
            "request_id": request_id,
            "tenant_id": str(cid),
            "status": "error",
            "error": {"code": "TEMPLATE_NOT_FOUND", "message": "nope", "permanent": True},
        },
        local_session=db_session,
        crm_session=db_session,
    )
    assert outcome == "render_failed"

    from shared.models.local_models import InvoiceModel

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert fresh.status == 6  # RENDER_FAILED
    assert fresh.artifact_uri is None
