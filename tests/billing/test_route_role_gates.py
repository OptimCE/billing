"""Role gates on the billing routes: all manager-only except a member's own reads.

The UI shows the billing console to managers only, but that is not a gate. This
test walks EVERY route the billing router serves, so a new route fails here until
it is either gated with ``_MANAGER_ONLY`` or added to ``MEMBER_ROUTES`` on purpose.
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute

from api.billing.routes import billing_routes
from tests.factories import crm_billing_factory as f

_AUTH = "role-gate-test-org"
_FORBIDDEN = 2  # AUTH.FORBIDDEN, what require_min_role answers

# A member's own reads. The service limits each one to the caller's own member
# records (test_my_invoices.py, test_invoice_read_access.py, test_invoice_pdf.py).
MEMBER_ROUTES = {
    ("GET", "/invoices/mine"),
    ("GET", "/invoices/{invoice_id}"),
    ("GET", "/invoices/{invoice_id}/payments"),
    ("GET", "/invoices/{invoice_id}/pdf"),
}


def _served_routes() -> list[tuple[str, str]]:
    served: list[tuple[str, str]] = []
    for route in billing_routes.routes:
        assert isinstance(route, APIRoute), route
        served.extend((method, route.path) for method in sorted(route.methods))
    return served


MANAGER_ROUTES = [r for r in _served_routes() if r not in MEMBER_ROUTES]


def _headers(role: str) -> dict[str, str]:
    return {
        "x-user-id": f"user-{role.lower()}",
        "x-community-id": _AUTH,
        "x-user-orgs": f"[orgId:{_AUTH} orgPath:/x roles:[{role}]]",
    }


async def _call(client, method: str, path: str, role: str):
    url = re.sub(r"\{[^}]+\}", "1", path)
    body: dict[str, object] | None = {} if method in ("POST", "PUT", "PATCH") else None
    return await client.request(method, url, headers=_headers(role), json=body)


async def _seed(db_session) -> None:
    cid = await f.create_community(db_session, auth_community_id=_AUTH)
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)


def test_member_routes_are_still_served():
    # A renamed member route must not leave a stale entry that silently passes.
    assert set(_served_routes()) >= MEMBER_ROUTES
    assert len(MANAGER_ROUTES) >= 15


@pytest.mark.parametrize(("method", "path"), MANAGER_ROUTES, ids=lambda v: str(v))
async def test_member_is_refused_on_every_manager_route(client, db_session, method, path):
    await _seed(db_session)
    resp = await _call(client, method, path, "MEMBER")
    # The shared require_min_role raises FORBIDDEN with ErrorException's default 400,
    # as the PDF routes always have; the error code is the contract, not the status.
    assert resp.status_code in (400, 403), resp.text
    assert resp.json()["error_code"] == _FORBIDDEN


@pytest.mark.parametrize(("method", "path"), MANAGER_ROUTES, ids=lambda v: str(v))
async def test_manager_passes_the_gate_on_every_manager_route(client, db_session, method, path):
    # Ids are dummies, so a 404 or a 422 is expected here: only the role refusal is not.
    await _seed(db_session)
    resp = await _call(client, method, path, "MANAGER")
    assert resp.json().get("error_code") != _FORBIDDEN, resp.text
