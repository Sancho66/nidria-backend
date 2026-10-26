"""The real agency screens read coherent fixtures; capability and write walls hold."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from src.admin.demo_schema import AgencyDemoBatchRequest, AgencyDemoInput
from src.core.config import get_settings
from src.core.database import get_db
from src.demo_sandbox.agency_control import AgencyDemoControlManager
from src.demo_sandbox.application import create_sandbox_app
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.control_manager import DemoControlManager
from src.demo_sandbox.prepare_management import prepare_agency_permissions
from src.demo_sandbox.seed import seed_catalog
from src.demo_sandbox.visits import record_visit


def pilot(name: str = "Willow Practice", email: str = "pilot@example.com") -> AgencyDemoInput:
    return AgencyDemoInput(
        email=email,
        company_name=name,
        language="en",
        profession="accountant",
        country="GB",
        city="Bristol",
        persona="director",
        steps=[
            "Confirm the company details",
            "Collect the accounting records",
            "Review income and expenses",
            "Prepare the annual accounts",
            "Review and sign the return",
            "File and confirm completion",
        ],
    )


@pytest.mark.asyncio
async def test_full_agency_and_old_client_links(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "signatures_enabled", True)
    monkeypatch.setattr(settings, "email_module_enabled", True)
    monkeypatch.setattr(settings, "environment", "demo_sandbox")
    catalog = load_catalog()
    await seed_catalog(db_session, catalog)
    await prepare_agency_permissions(db_session)
    payload = AgencyDemoBatchRequest(
        request_id=uuid.uuid4(), items=[pilot(), pilot("Birch Practice", "second@example.com")]
    )
    manager = AgencyDemoControlManager(db_session, catalog)
    created = await manager.create_batch(uuid.uuid4(), payload)
    repeated = await manager.create_batch(uuid.uuid4(), payload)
    assert [row.code for row in repeated.items] == [row.code for row in created.items]
    assert all(
        row.scope == "agency" and row.total_steps == 6 and row.expires_at for row in created.items
    )
    sandbox = create_sandbox_app(catalog)

    async def readonly_db():  # type: ignore[no-untyped-def]
        await db_session.execute(text("SET TRANSACTION READ ONLY"))
        try:
            yield db_session
        finally:
            await db_session.rollback()

    sandbox.dependency_overrides[get_db] = readonly_db
    async with AsyncClient(
        transport=ASGITransport(app=sandbox), base_url="http://demo.test"
    ) as client:
        opened = await client.post("/demo/session", json={"code": created.items[0].code})
        assert opened.status_code == 200, opened.text
        session = opened.json()
        assert session["face"] == "agent" and session["agency_demo"] is True
        headers = {"Authorization": f"Bearer {session['access_token']}", "Accept-Language": "en"}
        endpoints = [
            "/auth/agent/me",
            "/agencies/me",
            "/dashboard",
            "/dashboard/me",
            "/dashboard/worklist",
            "/dashboard/activity",
            "/cases",
            "/cases/columns",
            "/client-profiles",
            "/journeys",
            "/document-templates",
            "/reminders",
            "/message-templates",
            "/agencies/me/members",
            "/agencies/me/external-members",
            "/agencies/me/external-contacts",
            "/agencies/me/ai-usage",
            "/agencies/me/roles",
            "/agencies/me/custom-fields",
            "/agencies/me/custom-fields?include_archived=true&surface=company",
            "/agencies/me/profile-sections?surface=person",
            "/agencies/me/profile-sections?surface=company",
            "/agencies/me/field-universe?surface=person",
            "/agencies/me/field-universe?surface=company",
            "/views",
        ]
        responses = {}
        for path in endpoints:
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, f"{path}: {response.text}"
            assert "pilot@example.com" not in response.text
            responses[path] = response.json()
        assert responses["/cases"]["total"] == 10
        assert len(responses["/agencies/me/members"]) == 3
        assert len(responses["/agencies/me/external-members"]) == 1
        profiles = responses["/client-profiles"]["items"]
        profile = await client.get(f"/client-profiles/{profiles[0]['id']}", headers=headers)
        assert profile.status_code == 200, profile.text
        today = datetime.now(UTC).date()
        outreach = await client.get(
            "/email/outreach",
            headers=headers,
            params={"start": (today - timedelta(days=7)).isoformat(), "end": today.isoformat()},
        )
        assert outreach.status_code == 404, outreach.text
        assert responses["/agencies/me"]["email_enabled"] is False
        assert not any(key.startswith("sandbox_") for key in responses["/agencies/me"]["settings"])
        preview = await client.get("/demo/ai-preview", headers=headers)
        assert preview.status_code == 200, preview.text
        assert len(preview.json()["steps_created"]) == 6
        assert preview.json()["created"] is False
        case_id = session["case_id"]
        for path in [
            f"/cases/{case_id}",
            f"/cases/{case_id}/steps",
            f"/cases/{case_id}/documents",
            f"/cases/{case_id}/activity",
            f"/cases/{case_id}/costs",
        ]:
            response = await client.get(path, headers=headers)
            assert response.status_code == 200, f"{path}: {response.text}"
        second = (await client.post("/demo/session", json={"code": created.items[1].code})).json()
        assert (await client.get(f"/cases/{second['case_id']}", headers=headers)).status_code == 404
        assert (await client.get("/admin/demos", headers=headers)).status_code == 404
        assert (await client.get(f"/cases/{case_id}/export", headers=headers)).status_code == 404
        for path in [
            "/cases",
            "/reminders",
            "/agencies/me/invitations",
            "/billing/checkout",
            "/journeys/import",
        ]:
            assert (await client.post(path, headers=headers, json={})).status_code == 403
        client_session = await client.post(
            "/demo/session",
            json={"code": created.items[0].code, "face": "expat", "case_id": case_id},
        )
        assert client_session.status_code == 200, client_session.text
        expat_headers = {"Authorization": f"Bearer {client_session.json()['access_token']}"}
        cases = await client.get("/expat/cases", headers=expat_headers)
        assert cases.status_code == 200 and [case["id"] for case in cases.json()] == [case_id]
        assert (await client.get("/cases", headers=expat_headers)).status_code == 401
        assert (
            await client.post(
                "/demo/session",
                json={"code": created.items[0].code, "face": "expat", "case_id": second["case_id"]},
            )
        ).status_code == 404
        legacy = await client.post("/demo/session", json={"code": catalog.records[0].public_code})
        assert legacy.status_code == 200 and legacy.json()["face"] == "expat"
        legacy_headers = {"Authorization": f"Bearer {legacy.json()['access_token']}"}
        assert (await client.get("/cases", headers=legacy_headers)).status_code == 401
    agency = await db_session.scalar(
        select(Agency).where(Agency.slug == f"demo-sandbox-{created.items[0].code}")
    )
    assert agency is not None
    visit_id = uuid.uuid4()
    await record_visit(db_session, created.items[0].code, visit_id)
    await record_visit(db_session, created.items[0].code, visit_id)
    await record_visit(db_session, created.items[0].code, uuid.uuid4())
    await db_session.refresh(agency)
    visits = DemoControlManager(db_session, catalog).read(agency)
    assert visits.visits == 2 and visits.first_opened_at and visits.last_opened_at
    agency.settings = {
        **agency.settings,
        "sandbox_full_demo": {
            **agency.settings["sandbox_full_demo"],
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        },
    }
    await db_session.commit()
    async with AsyncClient(
        transport=ASGITransport(app=sandbox), base_url="http://demo.test"
    ) as client:
        assert (
            await client.post("/demo/session", json={"code": created.items[0].code})
        ).status_code == 410
        assert (await client.get("/cases", headers=headers)).status_code == 401
        assert (await client.get("/expat/cases", headers=expat_headers)).status_code == 401
    await DemoControlManager(db_session, catalog).disable(uuid.uuid4(), created.items[1].code)
    async with AsyncClient(
        transport=ASGITransport(app=sandbox), base_url="http://demo.test"
    ) as client:
        assert (
            await client.post("/demo/session", json={"code": created.items[1].code})
        ).status_code == 404
