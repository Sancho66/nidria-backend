"""The management tool cannot open a writer to public visitors or agency users."""

import base64
import copy
import json
import time
import uuid
from io import BytesIO
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from shared.models.agency import Agency
from shared.models.rbac import Role
from src.admin.demo_transport import DemoTransport, signature
from src.core.config import get_settings
from src.core.database import get_db
from src.core.enums import Audience
from src.core.rbac.baseline import seed_rbac_baseline
from src.core.security import create_access_token
from src.demo_sandbox.application import create_sandbox_app
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.control import control_db
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


def signed(body: bytes, actor: uuid.UUID, timestamp: int | None = None) -> dict[str, str]:
    stamp = str(timestamp or int(time.time()))
    return {
        "Content-Type": "application/json",
        "X-Demo-Time": stamp,
        "X-Demo-Actor": str(actor),
        "X-Demo-Signature": signature(get_settings().demo_control_secret, stamp, str(actor), body),
    }


async def control_call(
    client: AsyncClient, operation: str, payload: dict[str, Any], actor: uuid.UUID
):
    body = json.dumps({"operation": operation, "payload": payload}).encode()
    return await client.post("/_control/demos", content=body, headers=signed(body, actor))


@pytest.mark.asyncio
@pytest.mark.usefixtures("rbac_baseline")
async def test_only_superadmin_can_manage_demos(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = []

    async def transport(
        self: DemoTransport, actor_id: uuid.UUID, operation: str, payload: dict[str, Any]
    ) -> Any:
        calls.append((actor_id, operation))
        if operation == "templates":
            return []
        if operation == "list":
            return {"items": [], "total": 0, "page": 1, "page_size": 30}
        if operation == "agency_batch":
            return {"items": []}
        return {
            "code": "a" * 32,
            "name": "Example",
            "language": "en",
            "journey_name": "Example",
            "completed_steps": 0,
            "total_steps": 1,
            "public_url": "https://app.nidria.com/demo/" + "a" * 32,
            "enabled": operation != "disable",
            "created_at": "2026-10-22T00:00:00Z",
            "has_logo": False,
        }

    monkeypatch.setattr(DemoTransport, "call", transport)
    root = await make_agent(role=system_roles["superadmin"])
    admin = await make_agent(role=system_roles["admin"])
    endpoints = [
        ("GET", "/admin/demos", None),
        ("GET", "/admin/demos/templates", None),
        (
            "POST",
            "/admin/demos",
            {"request_id": str(uuid.uuid4()), "name": "Example", "template_id": "example"},
        ),
        ("POST", "/admin/demos/preview", {"template_id": "example"}),
        ("POST", "/admin/demos/" + "a" * 32 + "/disable", None),
        (
            "POST",
            "/admin/demos/agency-batch",
            {
                "request_id": str(uuid.uuid4()),
                "items": [
                    {
                        "email": "pilot@example.com",
                        "company_name": "Willow Practice",
                        "language": "en",
                        "profession": "accountant",
                        "country": "GB",
                        "city": "Bristol",
                        "persona": "director",
                        "steps": ["One", "Two", "Three", "Four", "Five", "Six"],
                    }
                ],
            },
        ),
    ]
    for method, path, body in endpoints:
        assert (await client.request(method, path, json=body)).status_code == 401
        assert (
            await client.request(method, path, json=body, headers=agent_headers(admin))
        ).status_code == 403
        expat = create_access_token(str(uuid.uuid4()), Audience.EXPAT)
        assert (
            await client.request(
                method, path, json=body, headers={"Authorization": "Bearer " + expat}
            )
        ).status_code == 401
    assert not calls
    assert (await client.get("/admin/demos", headers=agent_headers(root))).status_code == 200
    assert (
        await client.get("/admin/demos/templates", headers=agent_headers(root))
    ).status_code == 200
    created = await client.post("/admin/demos", json=endpoints[2][2], headers=agent_headers(root))
    assert created.status_code == 201
    disabled = await client.post(
        "/admin/demos/" + "a" * 32 + "/disable", headers=agent_headers(root)
    )
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    assert (
        await client.post(
            "/admin/demos/agency-batch", json=endpoints[-1][2], headers=agent_headers(root)
        )
    ).status_code == 201
    assert all(actor == root.id for actor, _ in calls)


@pytest.mark.asyncio
async def test_creation_public_scope_restart_assets_disable_and_atomic_failure(
    db_session: AsyncSession, async_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_rbac_baseline(db_session)
    from src.core.rbac.baseline import collect_bindings
    from src.demo_sandbox.bindings import BINDINGS

    await seed_rbac_baseline(db_session, bindings=[*collect_bindings(), *BINDINGS])
    await db_session.commit()
    await db_session.execute(
        text("CREATE ROLE nidria_demo_writer NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
    )
    await db_session.execute(
        text("CREATE ROLE nidria_demo_reader NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS")
    )
    from src.demo_sandbox.prepare_management import grant_management

    await grant_management(db_session)
    for table in ("demo_measurement", "demo_telemetry_visit"):
        assert await db_session.scalar(
            text("SELECT has_table_privilege('nidria_demo_reader', :table, 'SELECT')"),
            {"table": table},
        )
        assert not await db_session.scalar(
            text(
                "SELECT has_table_privilege('nidria_demo_reader', :table, "
                "'INSERT,UPDATE,DELETE,TRUNCATE')"
            ),
            {"table": table},
        )
        assert not await db_session.scalar(
            text("SELECT has_table_privilege('nidria_demo_writer', :table, 'DELETE,TRUNCATE')"),
            {"table": table},
        )
    assert await db_session.scalar(
        text("SELECT has_table_privilege('nidria_demo_writer', 'demo_telemetry_visit', 'INSERT')")
    )
    assert not await db_session.scalar(
        text(
            "SELECT has_table_privilege('nidria_demo_writer', 'demo_measurement', 'INSERT,UPDATE')"
        )
    )
    assert not await db_session.scalar(
        text("SELECT has_table_privilege('nidria_demo_writer', 'role', 'INSERT,UPDATE,DELETE')")
    )
    assert not await db_session.scalar(
        text("SELECT has_table_privilege('nidria_demo_writer', 'agency', 'TRUNCATE')")
    )
    assert await db_session.scalar(
        text("SELECT has_table_privilege('nidria_demo_writer', 'agency', 'DELETE')")
    )
    for table in ("refresh_token", "password_reset_token", "mfa_totp", "mfa_challenge"):
        assert not await db_session.scalar(
            text("SELECT has_table_privilege('nidria_demo_writer', :table, 'INSERT,UPDATE')"),
            {"table": table},
        )
    assert not await db_session.scalar(
        text(
            "SELECT has_table_privilege('nidria_demo_writer', 'email_campaign', "
            "'INSERT,UPDATE,DELETE')"
        )
    )
    assert not await db_session.scalar(
        text(
            "SELECT has_table_privilege('nidria_demo_reader', 'demo_history', "
            "'INSERT,UPDATE,DELETE')"
        )
    )
    for table in ("demo_import_job", "demo_import_row"):
        assert not await db_session.scalar(
            text(
                "SELECT has_table_privilege('nidria_demo_reader', :table, "
                "'SELECT,INSERT,UPDATE,DELETE')"
            ),
            {"table": table},
        )
        assert await db_session.scalar(
            text("SELECT has_table_privilege('nidria_demo_writer', :table, 'INSERT,UPDATE')"),
            {"table": table},
        )
    monkeypatch.setattr(get_settings(), "demo_admin_enabled", True)
    monkeypatch.setattr(get_settings(), "demo_control_secret", "x" * 64)
    monkeypatch.setattr(get_settings(), "signatures_enabled", True)
    catalog = load_catalog()
    app = create_sandbox_app(catalog)
    control = next(route.app for route in app.routes if getattr(route, "path", None) == "/_control")

    async def isolated_transaction():
        async with (
            async_engine.connect() as connection,
            connection.begin(),
            AsyncSession(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ) as db,
        ):
            await db.execute(text("SET LOCAL ROLE nidria_demo_writer"))
            yield db
            await db.commit()

    async def reader():
        async with AsyncSession(bind=async_engine, expire_on_commit=False) as db:
            yield db

    control.dependency_overrides[control_db] = isolated_transaction
    app.dependency_overrides[get_db] = reader
    actor = uuid.uuid4()
    logo = BytesIO()
    Image.new("RGB", (20, 20), "purple").save(logo, "PNG")
    source = copy.deepcopy(catalog.records[0].journey_import)
    body = {
        "request_id": str(uuid.uuid4()),
        "name": "Cabinet fictif de recette",
        "language": "fr",
        "journey_import": source,
        "completed_steps": 1,
        "provided_documents": ["step_3/0"],
        "logo_data_url": "data:image/png;base64," + base64.b64encode(logo.getvalue()).decode(),
    }
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://demo.test"
    ) as client:
        unsigned = json.dumps({"operation": "create", "payload": body}).encode()
        for headers in (
            {"Content-Type": "application/json"},
            signed(unsigned, actor, int(time.time()) - 120),
            {**signed(unsigned, actor), "X-Demo-Actor": str(uuid.uuid4())},
        ):
            assert (
                await client.post("/_control/demos", content=unsigned, headers=headers)
            ).status_code == 403
        assert await db_session.scalar(select(func.count()).select_from(Agency)) == 0
        malformed = await control_call(client, "create", {"name": "Private pasted input"}, actor)
        assert malformed.status_code == 422
        assert "Private pasted input" not in malformed.text
        preview = await control_call(
            client, "preview", {"language": "fr", "journey_import": source}, actor
        )
        assert preview.status_code == 200 and len(preview.json()["steps"]) == 7
        assert await db_session.scalar(select(func.count()).select_from(Agency)) == 0
        created = await control_call(client, "create", body, actor)
        assert created.status_code == 200, created.text
        record = created.json()
        retry = await control_call(client, "create", body, actor)
        assert retry.json() == record
        changed = await control_call(client, "create", {**body, "name": "Different"}, actor)
        assert changed.status_code == 409
        assert await db_session.scalar(select(func.count()).select_from(Agency)) == 1
        assert record["code"] not in {r.public_code for r in catalog.records}
        opened = await client.post("/demo/session", json={"code": record["code"]})
        assert opened.status_code == 200, opened.text
        session = opened.json()
        headers = {"Authorization": "Bearer " + session["access_token"]}
        cases = await client.get("/expat/cases", headers=headers)
        assert len(cases.json()) == 1
        case = cases.json()[0]
        assert case["steps_done"] == 1 and case["steps_total"] == 7
        assert case["agency"]["has_logo"] is True
        from src.core.storage import mock_store

        mock_store.clear()  # Generated PDFs/logo must survive a service restart.
        docs = (
            await client.get(f"/expat/cases/{session['case_id']}/documents", headers=headers)
        ).json()
        assert len(docs) == 2
        for doc in docs:
            downloaded = await client.get(
                f"/expat/cases/{session['case_id']}/documents/{doc['id']}/download", headers=headers
            )
            assert downloaded.status_code == 200 and downloaded.content.startswith(b"%PDF-")
        logo_response = await client.get(
            f"/expat/agencies/{case['agency']['id']}/logo", headers=headers
        )
        assert logo_response.status_code == 200
        assert (
            await client.get(f"/expat/agencies/{uuid.uuid4()}/logo", headers=headers)
        ).status_code == 404
        assert (
            await client.post(f"/expat/cases/{session['case_id']}/documents", headers=headers)
        ).status_code == 403
        disabled = await control_call(client, "disable", {"code": record["code"]}, actor)
        assert disabled.status_code == 200 and not disabled.json()["enabled"]
        assert (
            await client.post("/demo/session", json={"code": record["code"]})
        ).status_code == 404
        assert (await client.get("/expat/cases", headers=headers)).status_code == 401
        listing = await control_call(client, "list", {"search": "fictif"}, actor)
        assert listing.json()["total"] == 1 and not listing.json()["items"][0]["enabled"]
        # A failure after the reused managers have committed must still roll
        # back the entire outer transaction, with no orphan agency or dossier.
        from src.demo_sandbox import control_manager

        real_seed = control_manager.seed_record

        async def fail_after_seed(*args: Any, **kwargs: Any):
            await real_seed(*args, **kwargs)
            raise RuntimeError("test failure after business commits")

        monkeypatch.setattr(control_manager, "seed_record", fail_after_seed)
        with pytest.raises(RuntimeError, match="test failure"):
            await control_call(client, "create", {**body, "request_id": str(uuid.uuid4())}, actor)
        assert await db_session.scalar(select(func.count()).select_from(Agency)) == 1

        from src.demo_sandbox import lifecycle

        monkeypatch.setattr(
            lifecycle, "SANDBOX_DB_NAME", await db_session.scalar(text("SELECT current_database()"))
        )
        exported = await control_call(client, "export", {"codes": [record["code"]]}, actor)
        assert exported.status_code == 200 and exported.json()["count"] == 1
        deleted = await control_call(
            client,
            "bulk",
            {
                "selection": {"codes": [record["code"]]},
                "action": "delete",
                "expected_count": 1,
                "selection_token": exported.json()["selection_token"],
            },
            actor,
        )
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["affected"] == 1
        assert (await control_call(client, "archive", {}, actor)).json()["count"] == 1
        assert (
            await client.post("/demo/session", json={"code": record["code"]})
        ).status_code == 404
        # The standalone sandbox table is not part of production metadata.
        await db_session.execute(lifecycle.history.delete())
        await db_session.commit()


@pytest.mark.asyncio
async def test_transport_signs_only_the_demo_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from src.core.exceptions import UpstreamError

    settings = get_settings()
    monkeypatch.setattr(settings, "demo_admin_enabled", True)
    monkeypatch.setattr(settings, "demo_control_secret", "s" * 64)
    monkeypatch.setattr(settings, "demo_control_url", "https://nidria-demo-api.fly.dev")
    actor = uuid.uuid4()
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "https://nidria-demo-api.fly.dev/_control/demos"
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert request.headers["X-Demo-Signature"] == signature(
            "s" * 64, request.headers["X-Demo-Time"], str(actor), request.content
        )
        assert json.loads(request.content) == {"operation": "list", "payload": {"page": 1}}
        return httpx.Response(200, json={"items": []})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    assert await DemoTransport().call(actor, "list", {"page": 1}) == {"items": []}
    monkeypatch.setattr(settings, "demo_control_url", "https://api.nidria.com")
    with pytest.raises(UpstreamError):
        await DemoTransport().call(actor, "list", {"page": 1})
    assert len(calls) == 1


@pytest.mark.parametrize("language", ["fr", "en", "es", "it", "pt", "hu", "ru"])
def test_demo_import_language_and_unicode_documents(language: str) -> None:
    from src.admin.demo_schema import DemoCreateRequest
    from src.core.storage import mock_store
    from src.demo_sandbox.draft import build_record
    from src.demo_sandbox.storage import document_path, load_record_documents

    payload = DemoCreateRequest.model_validate(
        {
            "request_id": str(uuid.uuid4()),
            "name": "Cabinet Águila & Партнёры",
            "language": language,
            "completed_steps": 0,
            "provided_documents": ["start/0"],
            "include_signature": False,
            "include_reminder": False,
            "journey_import": {
                "version": 1,
                "parcours": {
                    "langue_par_defaut": language,
                    "nom": {language: "Parcours de recette"},
                    "etapes": [
                        {
                            "ref": "start",
                            "nom": {language: "Accueil"},
                            "documents_a_fournir": [{"libelle": {language: "Паспорт"}}],
                        }
                    ],
                },
            },
        }
    )
    record = build_record(payload, uuid.uuid4().hex, load_catalog())
    load_record_documents(record)
    assert mock_store[document_path(record.id, 0)].startswith(b"%PDF-")
    assert record.language == language
    assert record.state["progress"] == {"done": 0, "total": 1}
    assert record.state["reminders"] == [] and record.state["signature"]["enabled"] is False
