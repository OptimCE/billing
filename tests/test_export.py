"""The billing part of a data export (api/data_export.py).

Through the kit's own contract check (`run_contract_check`): the answer matches the contract,
every file listed was written, and each JSON file is checked here against its item schema
(core/export/schemas/files/billing/).
"""

from __future__ import annotations

import datetime
import json
import pathlib
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import main
from api.billing.deps import get_event_publisher
from api.data_export import TABLES, export_handler
from core.config import settings
from core.export import ExportHandler, run_contract_check
from tests.factories import crm_billing_factory as f
from worker import persistence

SERVICE = "billing"
SCHEMAS = pathlib.Path(__file__).resolve().parent.parent / f"core/export/schemas/files/{SERVICE}"
AUTH = "export-test-org"
BUCKET = settings.OUTPUT_BUCKET


class _FakePublisher:
    async def publish(self, subject: str, event: Any) -> None:
        return None


def _headers(user_id: str, role: str = "MEMBER") -> dict[str, str]:
    return {
        "x-user-id": user_id,
        "x-community-id": AUTH,
        "x-user-orgs": f"[orgId:{AUTH} orgPath:/x roles:[{role}]]",
    }


async def seed(client: Any, db_session: AsyncSession) -> dict[str, int]:
    """Two members A and B, each with a DRAFT invoice from one June run; 'user-a' linked to A."""
    main.app.dependency_overrides[get_event_publisher] = lambda: _FakePublisher()
    cid = await f.create_community(db_session, auth_community_id=AUTH)
    await f.create_subscription(db_session, id_community=cid, feature="billing", is_active=True)
    op = await f.create_sharing_operation(db_session, id_community=cid)
    member_a = await f.create_member(db_session, id_community=cid, name="Alice")
    member_b = await f.create_member(db_session, id_community=cid, name="Bob")
    for ean, member in (("EAN-A", member_a), ("EAN-B", member_b)):
        await f.create_meter(db_session, ean=ean, id_community=cid)
        await f.create_meter_data(
            db_session,
            ean=ean,
            id_community=cid,
            id_sharing_operation=op,
            id_member=member,
            client_type=1,
            start_date=datetime.date(2026, 1, 1),
        )
        await f.create_meter_consumption(
            db_session,
            ean=ean,
            id_community=cid,
            id_sharing_operation=op,
            timestamp=f.june(5),
            shared=20.0,
        )
    user_a = await f.create_app_user(db_session, auth_user_id="user-a")
    await f.link_user_to_member(db_session, id_user=user_a, id_member=member_a)
    admin = await f.create_app_user(db_session, auth_user_id="user-admin")
    await f.add_community_member(db_session, id_community=cid, id_user=admin, role="ADMIN")
    manager = await f.create_app_user(db_session, auth_user_id="user-manager")
    await f.add_community_member(db_session, id_community=cid, id_user=manager, role="MANAGER")

    await client.post(
        f"/sharing-operations/{op}/tariffs",
        headers=_headers("user-admin", "ADMIN"),
        json={"kind": 1, "scope": 1, "price_per_kwh": "0.15", "valid_from": "2026-01-01"},
    )
    response = await client.post(
        f"/sharing-operations/{op}/billing-runs",
        headers=_headers("user-admin", "ADMIN"),
        json={"period_start": "2026-06-01", "period_end": "2026-06-30"},
    )
    run_id = response.json()["data"]["id"]
    await persistence.process_billing_run(run_id, local_session=db_session, crm_session=db_session)
    invoices = {
        int(row[0]): int(row[1])
        for row in await db_session.execute(
            text("SELECT id_member, id FROM invoice WHERE id_community = :c"), {"c": cid}
        )
    }
    return {
        "cid": cid,
        "a": member_a,
        "b": member_b,
        "inv_a": invoices[member_a],
        "inv_b": invoices[member_b],
    }


def handler_for(session: AsyncSession, objects: dict[tuple[str, str], int]) -> ExportHandler:
    @asynccontextmanager
    async def shared() -> AsyncIterator[AsyncSession]:
        yield session

    async def size_of(bucket: str, key: str) -> int | None:
        return objects.get((bucket, key))

    return export_handler(local=shared, crm=shared, size_of=size_of, bucket=BUCKET)


def _validators() -> dict[str, Draft202012Validator]:
    schemas = [json.loads(p.read_text(encoding="utf-8")) for p in SCHEMAS.glob("*.schema.json")]
    registry: Registry = Registry().with_resources(
        (schema["$id"], Resource.from_contents(schema)) for schema in schemas
    )
    return {
        schema["title"].split("/")[1].removesuffix(".json item"): Draft202012Validator(
            schema, registry=registry, format_checker=FormatChecker()
        )
        for schema in schemas
        if schema.get("title", "").endswith(" item")
    }


async def export(
    session: AsyncSession,
    *,
    scope: dict[str, str],
    requester: str,
    objects: dict[tuple[str, str], int] | None = None,
) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """Runs one part; returns part.done and each JSON file's items, checked against its schema."""
    result = await run_contract_check(
        handler_for(session, objects or {}), service=SERVICE, scope=scope, requester_id=requester
    )
    assert result.outcome == "done", result.message
    validators = _validators()
    files: dict[str, list[dict[str, Any]]] = {}
    for file in result.message["files"]:
        document = json.loads(result.store.text(file["key"]))
        entity = document["entity"]
        errors = [
            f"{entity}[{i}]: {error.message}"
            for i, item in enumerate(document["items"])
            for error in validators[entity].iter_errors(item)
        ]
        assert not errors, errors
        files[entity] = document["items"]
    return result.message, files


def counters(done: dict[str, Any]) -> dict[str, int]:
    return {
        item["labelKey"].removeprefix(f"export.{SERVICE}."): item["count"]
        for item in done["summary"]
    }


def user(sub: str) -> dict[str, Any]:
    return {"scope": {"type": "user", "id": sub}, "requester": sub}


def community(sub: str) -> dict[str, Any]:
    return {"scope": {"type": "community", "id": AUTH}, "requester": sub}


async def attach_pdf(db_session: AsyncSession, invoice_id: int, uri: str) -> None:
    await db_session.execute(
        text("UPDATE invoice SET artifact_uri = :u WHERE id = :i"), {"u": uri, "i": invoice_id}
    )
    await db_session.flush()


async def add_payment(db_session: AsyncSession, invoice_id: int, cid: int) -> None:
    await db_session.execute(
        text(
            "INSERT INTO payment (id_community, id_invoice, amount, currency, method, reference, "
            "paid_at) VALUES (:c, :i, 3.63, 'EUR', 1, 'virement', now())"
        ),
        {"c": cid, "i": invoice_id},
    )
    await db_session.flush()


class TestUserExport:
    async def test_her_invoices_as_the_app_shows_them_with_payments_and_pdf(
        self, client, db_session
    ):
        world = await seed(client, db_session)
        await add_payment(db_session, world["inv_a"], world["cid"])
        await attach_pdf(db_session, world["inv_a"], f"s3://{BUCKET}/billing/a.pdf")
        shown = await client.get(f"/invoices/{world['inv_a']}", headers=_headers("user-a"))
        paid = await client.get(f"/invoices/{world['inv_a']}/payments", headers=_headers("user-a"))

        done, files = await export(
            db_session, **user("user-a"), objects={(BUCKET, "billing/a.pdf"): 4321}
        )

        (invoice,) = files["my-invoices"]
        assert invoice["id_member"] == world["a"] and invoice["id_community"] == world["cid"]
        own = {
            k: v
            for k, v in invoice.items()
            if k not in ("id_community", "community", "payments", "file")
        }
        assert own == shown.json()["data"]
        assert invoice["lines"], "the lines GET /invoices/{id} shows"
        assert invoice["payments"] == paid.json()["data"]
        path = f"files/invoices/{world['inv_a']}-proforma.pdf"
        assert invoice["file"] == {"path": f"{SERVICE}/{path}", "file_missing": False}
        assert done["references"] == [
            {
                "bucket": BUCKET,
                "key": "billing/a.pdf",
                "targetPath": f"{SERVICE}/{path}",
                "size": 4321,
                "contentType": "application/pdf",
            }
        ]
        assert counters(done) == {"invoices": 1, "files": 1}

    async def test_nobody_else_s_bill_and_nothing_for_someone_unlinked(self, client, db_session):
        world = await seed(client, db_session)
        _done, files = await export(db_session, **user("user-a"))
        assert {i["id_member"] for i in files["my-invoices"]} == {world["a"]}
        await f.create_app_user(db_session, auth_user_id="user-nobody")
        done, files = await export(db_session, **user("user-nobody"))
        assert files == {"my-invoices": []} and done["references"] == []

    async def test_the_module_off_hides_nothing(self, client, db_session):
        world = await seed(client, db_session)
        await db_session.execute(
            text("UPDATE community_subscription SET is_active = false WHERE id_community = :c"),
            {"c": world["cid"]},
        )
        _done, files = await export(db_session, **user("user-a"))
        assert len(files["my-invoices"]) == 1


class TestCommunityExport:
    async def test_every_table_codes_by_name_amounts_as_written(self, client, db_session):
        world = await seed(client, db_session)
        await add_payment(db_session, world["inv_a"], world["cid"])
        await attach_pdf(db_session, world["inv_a"], f"s3://{BUCKET}/billing/a.pdf")
        await attach_pdf(db_session, world["inv_b"], f"s3://{BUCKET}/billing/gone.pdf")

        done, files = await export(
            db_session, **community("user-admin"), objects={(BUCKET, "billing/a.pdf"): 10}
        )

        assert set(files) == {table.entity for table in TABLES}
        for table, entity in (
            ("invoice", "invoices"),
            ("invoice_line", "invoice-lines"),
            ("settlement_snapshot", "settlement-snapshots"),
            ("payment", "payments"),
        ):
            stored = (
                await db_session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE id_community = :c"),  # noqa: S608
                    {"c": world["cid"]},
                )
            ).scalar_one()
            assert len(files[entity]) == stored > 0, entity
        assert {i["status"] for i in files["invoices"]} == {"DRAFT"}
        assert {i["type"] for i in files["invoices"]} == {"INVOICE"}
        assert files["tariffs"][0]["kind"] == "CONSUMER_SELLING"
        assert files["tariffs"][0]["price_per_kwh"].startswith("0.15")
        assert all("E" not in line["quantity_kwh"] for line in files["invoice-lines"])
        assert all("id_community" not in item for items in files.values() for item in items)
        by_id = {i["id"]: i for i in files["invoices"]}
        assert by_id[world["inv_a"]]["file"]["file_missing"] is False
        assert by_id[world["inv_b"]]["file"] == {"path": None, "file_missing": True}
        assert counters(done)["files"] == 1 and counters(done)["files_missing"] == 1

    async def test_a_pdf_outside_the_documents_bucket_is_missing_not_fatal(
        self, client, db_session
    ):
        world = await seed(client, db_session)
        await attach_pdf(db_session, world["inv_a"], "s3://elsewhere/a.pdf")
        done, files = await export(
            db_session, **community("user-admin"), objects={("elsewhere", "a.pdf"): 1}
        )
        assert done["references"] == []
        by_id = {i["id"]: i for i in files["invoices"]}
        assert by_id[world["inv_a"]]["file"] == {"path": None, "file_missing": True}

    @pytest.mark.parametrize("sub", ["user-manager", "user-a"])
    async def test_only_an_admin_exports_the_community(self, client, db_session, sub):
        await seed(client, db_session)
        spec = community(sub)
        result = await run_contract_check(
            handler_for(db_session, {}),
            service=SERVICE,
            scope=spec["scope"],
            requester_id=spec["requester"],
        )
        assert result.outcome == "failed"
        assert result.message["errorCode"] == "forbidden"

    async def test_an_unknown_community_is_scope_not_found(self, client, db_session):
        await seed(client, db_session)
        result = await run_contract_check(
            handler_for(db_session, {}),
            service=SERVICE,
            scope={"type": "community", "id": str(uuid.uuid4())},
            requester_id="user-admin",
        )
        assert result.outcome == "failed"
        assert result.message["errorCode"] == "scope_not_found"
