"""Invoice PDF surface: generate / download (authz) / delete, proforma & status hygiene.

Covers the on-demand render trigger, the byte-streaming download with manager-vs-owner
authorization, the delete-unless-sent rule, the DRAFT proforma template selection, and the
render-status hygiene (RENDER_FAILED recovery + never fail-marking a draft).
"""

from __future__ import annotations

import datetime

from sqlalchemy import update

import main
from api.billing import service as service_module
from api.billing.deps import get_event_publisher
from core.config import settings
from ports.document_generation import DocgenRequest
from shared.const import InvoiceStatus
from shared.models.local_models import InvoiceModel
from tests.factories import crm_billing_factory as f
from worker import docgen_results, issue, persistence

_AUTH = "pdf-test-org"
_URI = f"s3://{settings.OUTPUT_BUCKET}/billing/invoices/2026/07/inv-1/invoice.pdf"


def _admin_headers(user_id: str = "admin") -> dict[str, str]:
    return {
        "x-user-id": user_id,
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[ADMIN]]",
    }


def _member_headers(user_id: str) -> dict[str, str]:
    return {
        "x-user-id": user_id,
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[MEMBER]]",
    }


class _FakePublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    async def publish(self, subject: str, event: object) -> None:
        self.published.append((subject, event))


class _FakeDocGen:
    def __init__(self) -> None:
        self.requests: list[DocgenRequest] = []

    async def request_render(self, request: DocgenRequest) -> None:
        self.requests.append(request)


async def _seed_single(client, db_session, *, link_user: str | None = None):
    """Community + one member + one DRAFT invoice from a computed run."""
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban="BE68539007547034", legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member = await f.create_member(
        db_session, id_community=cid, name="Alice Dupont", member_type=1, iban="BE71096123456769"
    )
    await f.create_meter(db_session, ean="EAN-C", id_community=cid)
    await f.create_meter_data(
        db_session, ean="EAN-C", id_community=cid, id_sharing_operation=op,
        id_member=member, client_type=1, start_date=datetime.date(2026, 1, 1),
    )
    await f.create_meter_consumption(
        db_session, ean="EAN-C", id_community=cid, id_sharing_operation=op,
        timestamp=f.june(5), shared=30.0,
    )
    if link_user:
        u = await f.create_app_user(db_session, auth_user_id=link_user)
        await f.link_user_to_member(db_session, id_user=u, id_member=member)
    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_admin_headers(),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_admin_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    resp = await client.get(f"/billing-runs/{run_id}/invoices", headers=_admin_headers())
    return cid, member, resp.json()["data"][0]


async def _seed_two_members(client, db_session):
    """Two members (A, B); auth user 'owner' linked to A only. Returns (cid, inv_a, inv_b)."""
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(
        db_session, auth_community_id=_AUTH, iban="BE68539007547034", legal_name="ACME ASBL"
    )
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member_a = await f.create_member(db_session, id_community=cid, name="Alice")
    member_b = await f.create_member(db_session, id_community=cid, name="Bob")
    for ean, member in (("EAN-A", member_a), ("EAN-B", member_b)):
        await f.create_meter(db_session, ean=ean, id_community=cid)
        await f.create_meter_data(
            db_session, ean=ean, id_community=cid, id_sharing_operation=op,
            id_member=member, client_type=1, start_date=datetime.date(2026, 1, 1),
        )
        await f.create_meter_consumption(
            db_session, ean=ean, id_community=cid, id_sharing_operation=op,
            timestamp=f.june(5), shared=20.0,
        )
    u = await f.create_app_user(db_session, auth_user_id="owner")
    await f.link_user_to_member(db_session, id_user=u, id_member=member_a)
    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_admin_headers(),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    resp = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_admin_headers(),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = resp.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    resp = await client.get(f"/billing-runs/{run_id}/invoices", headers=_admin_headers())
    by_member = {inv["id_member"]: inv["id"] for inv in resp.json()["data"]}
    return cid, by_member[member_a], by_member[member_b]


async def _issue(client, invoice_id: int) -> None:
    resp = await client.post(f"/invoices/{invoice_id}/issue", headers=_admin_headers())
    assert resp.status_code == 200, resp.text


async def _set(db_session, invoice_id: int, **values) -> None:
    await db_session.execute(
        update(InvoiceModel).where(InvoiceModel.id == invoice_id).values(**values)
    )
    await db_session.commit()
    db_session.expire_all()


# ---- read model ------------------------------------------------------------


async def test_pdf_ready_flag_in_read_model(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    assert invoice["pdf_ready"] is False

    await _issue(client, invoice_id)
    resp = await client.get(f"/invoices/{invoice_id}", headers=_admin_headers())
    assert resp.json()["data"]["pdf_ready"] is False

    await _set(db_session, invoice_id, artifact_uri=_URI, artifact_sha256="abc")
    resp = await client.get(f"/invoices/{invoice_id}", headers=_admin_headers())
    assert resp.json()["data"]["pdf_ready"] is True


# ---- generate --------------------------------------------------------------


async def test_generate_pdf_requests_render(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)

    resp = await client.post(
        f"/invoices/{invoice_id}/pdf", headers=_admin_headers(), json={"force": False}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["id"] == invoice_id
    assert data["pdf_ready"] is False
    assert data["status"] == int(InvoiceStatus.ISSUED)


async def test_generate_pdf_force_clears_artifact(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)
    await _set(db_session, invoice_id, artifact_uri=_URI, artifact_sha256="abc",
               docgen_request_id="req-old")

    resp = await client.post(
        f"/invoices/{invoice_id}/pdf", headers=_admin_headers(), json={"force": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["pdf_ready"] is False

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert fresh.artifact_uri is None
    assert fresh.docgen_request_id is None


async def test_generate_pdf_on_render_failed_resets_to_issued(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)
    await _set(db_session, invoice_id, status=InvoiceStatus.RENDER_FAILED)

    resp = await client.post(
        f"/invoices/{invoice_id}/pdf", headers=_admin_headers(), json={"force": False}
    )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert fresh.status == InvoiceStatus.ISSUED


async def test_generate_pdf_cancelled_is_409(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _set(db_session, invoice_id, status=InvoiceStatus.CANCELLED)

    resp = await client.post(
        f"/invoices/{invoice_id}/pdf", headers=_admin_headers(), json={"force": False}
    )
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2228  # INVOICE_NOT_RENDERABLE


async def test_generate_pdf_requires_manager(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    resp = await client.post(
        f"/invoices/{invoice['id']}/pdf", headers=_member_headers("someone"), json={"force": False}
    )
    assert resp.json()["error_code"] == 2  # AUTH.FORBIDDEN (min-role gate)


# ---- download --------------------------------------------------------------


async def test_download_pdf_manager_streams_bytes(client, db_session, monkeypatch):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)
    await _set(db_session, invoice_id, artifact_uri=_URI, artifact_sha256="abc")

    async def _fake_download(key: str) -> bytes:
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(service_module.storage, "download_output", _fake_download)

    resp = await client.get(f"/invoices/{invoice_id}/pdf", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert "attachment; filename=" in resp.headers["content-disposition"]
    assert resp.content == b"%PDF-1.4 fake"


async def test_download_pdf_not_ready_is_422(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)

    resp = await client.get(f"/invoices/{invoice_id}/pdf", headers=_admin_headers())
    assert resp.status_code == 422
    assert resp.json()["error_code"] == 2227  # INVOICE_PDF_NOT_READY


async def test_download_pdf_member_owner_vs_stranger(client, db_session, monkeypatch):
    _cid, inv_a, inv_b = await _seed_two_members(client, db_session)
    await _set(db_session, inv_a, artifact_uri=_URI, artifact_sha256="a")
    await _set(db_session, inv_b, artifact_uri=_URI, artifact_sha256="b")

    async def _fake_download(key: str) -> bytes:
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(service_module.storage, "download_output", _fake_download)

    # Owner (linked to A) can fetch their own invoice...
    resp = await client.get(f"/invoices/{inv_a}/pdf", headers=_member_headers("owner"))
    assert resp.status_code == 200, resp.text
    # ...but not another member's invoice.
    resp = await client.get(f"/invoices/{inv_b}/pdf", headers=_member_headers("owner"))
    assert resp.status_code == 403
    # A manager can fetch any invoice in the community.
    resp = await client.get(f"/invoices/{inv_b}/pdf", headers=_admin_headers())
    assert resp.status_code == 200, resp.text


# ---- delete ----------------------------------------------------------------


async def test_delete_pdf_removes_artifact(client, db_session, monkeypatch):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)
    await _set(db_session, invoice_id, artifact_uri=_URI, artifact_sha256="abc")

    deleted: list[str] = []

    async def _fake_delete(key: str) -> None:
        deleted.append(key)

    monkeypatch.setattr(service_module.storage, "delete_output", _fake_delete)

    resp = await client.delete(f"/invoices/{invoice_id}/pdf", headers=_admin_headers())
    assert resp.status_code == 200, resp.text
    assert deleted == ["billing/invoices/2026/07/inv-1/invoice.pdf"]

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert fresh.artifact_uri is None


async def test_delete_pdf_blocked_when_sent(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)
    await _set(db_session, invoice_id, artifact_uri=_URI, artifact_sha256="abc",
               status=InvoiceStatus.SENT)

    resp = await client.delete(f"/invoices/{invoice_id}/pdf", headers=_admin_headers())
    assert resp.status_code == 409
    assert resp.json()["error_code"] == 2229  # INVOICE_PDF_DELETE_FORBIDDEN


async def test_delete_pdf_requires_manager(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    resp = await client.delete(
        f"/invoices/{invoice['id']}/pdf", headers=_member_headers("someone")
    )
    assert resp.json()["error_code"] == 2  # AUTH.FORBIDDEN (min-role gate)


# ---- proforma + worker status hygiene --------------------------------------


async def test_process_issue_draft_renders_proforma(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]  # still DRAFT (never issued)

    fake = _FakeDocGen()
    await issue.process_issue(
        invoice_id, doc_port=fake, local_session=db_session, crm_session=db_session
    )
    assert len(fake.requests) == 1
    req = fake.requests[0]
    assert req.template_uri.endswith("/invoice_proforma/v1/")
    assert "proforma" in req.key_prefix
    assert req.data["invoice"]["number"] is None


async def test_issue_clears_prior_proforma_artifact(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    # A proforma PDF was rendered while the invoice was a draft.
    await _set(db_session, invoice_id, artifact_uri=_URI, artifact_sha256="draft",
               docgen_request_id="proforma-req")

    await _issue(client, invoice_id)

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert fresh.status == InvoiceStatus.ISSUED
    assert fresh.artifact_uri is None  # cleared so the legal render regenerates
    assert fresh.docgen_request_id is None

    # process_issue now re-renders (not a no-op) using the legal invoice template.
    fake = _FakeDocGen()
    await issue.process_issue(
        invoice_id, doc_port=fake, local_session=db_session, crm_session=db_session
    )
    assert len(fake.requests) == 1
    assert fake.requests[0].template_uri.endswith("/invoice/v1/")
    assert fake.requests[0].data["invoice"]["number"] is not None


async def test_docgen_draft_permanent_error_stays_draft(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]  # DRAFT

    fake = _FakeDocGen()
    await issue.process_issue(
        invoice_id, doc_port=fake, local_session=db_session, crm_session=db_session
    )
    request_id = fake.requests[0].request_id

    outcome = await docgen_results.process_docgen_result(
        {
            "request_id": request_id,
            "tenant_id": str(_cid),
            "status": "failed",  # matches document-generation's GenerationStatus.FAILED
            "error": {"code": "VALIDATION_ERROR", "message": "bad", "permanent": True},
        },
        local_session=db_session,
        crm_session=db_session,
    )
    assert outcome == "render_failed"

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert fresh.status == InvoiceStatus.DRAFT  # NOT pushed to RENDER_FAILED
    assert fresh.artifact_uri is None


async def test_docgen_success_recovers_render_failed(client, db_session):
    _cid, _member, invoice = await _seed_single(client, db_session)
    invoice_id = invoice["id"]
    await _issue(client, invoice_id)
    # Simulate a prior permanent failure that has since been re-requested.
    await _set(db_session, invoice_id, status=InvoiceStatus.RENDER_FAILED,
               docgen_request_id="retry-req")

    outcome = await docgen_results.process_docgen_result(
        {
            "request_id": "retry-req",
            "tenant_id": str(_cid),
            "status": "success",
            "artifacts": [{"format": "pdf", "uri": _URI, "sha256": "ok"}],
        },
        local_session=db_session,
        crm_session=db_session,
    )
    assert outcome == "attached"

    db_session.expire_all()
    fresh = await db_session.get(InvoiceModel, invoice_id)
    assert fresh.status == InvoiceStatus.ISSUED  # recovered from RENDER_FAILED
    assert fresh.artifact_uri == _URI
