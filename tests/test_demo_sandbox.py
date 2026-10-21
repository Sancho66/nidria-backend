"""Prove that the actual portal reads forty isolated, fictional dossiers."""

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.client_case import ClientCase
from src.core.config import Settings, get_settings
from src.core.database import get_db
from src.core.enums import Audience
from src.core.security import create_access_token
from src.demo_sandbox.application import create_sandbox_app
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.guards import assert_isolated_settings
from src.demo_sandbox.seed import seed_catalog
from src.main import app as production_app


def isolated_settings() -> Settings:
    return Settings(
        _env_file=None,
        environment="demo_sandbox",
        bcrypt_rounds=12,
        database_url="postgresql+asyncpg://nidria_demo_reader:fake@localhost/nidria_demo_sandbox",
        database_url_sync="postgresql+psycopg2://nidria_demo_reader:fake@localhost/nidria_demo_sandbox",
        jwt_agent_secret="a" * 40,
        jwt_expat_secret="b" * 40,
        jwt_refresh_secret="c" * 40,
        mock_services=True,
        mock_email=True,
        mock_storage=True,
        mock_lemlist=True,
        scheduler_enabled=False,
        billing_checkout_enabled=False,
        resend_monitor_enabled=False,
        resend_api_key=None,
        resend_webhook_secret=None,
        supabase_url=None,
        supabase_service_role_key=None,
        paddle_api_key=None,
        paddle_webhook_secret=None,
        ai_translation_api_key="",
        docuseal_api_key=None,
        docuseal_webhook_secret=None,
    )


@pytest.mark.parametrize(
    "change",
    [
        {"environment": "production"},
        {
            "database_url": "postgresql+asyncpg://nidria_demo_reader:fake@db.supabase.co/nidria_demo_sandbox"
        },
        {"database_url_sync": "postgresql+psycopg2://nidria_demo_reader:fake@localhost/postgres"},
        {"database_url": "postgresql+asyncpg://postgres:fake@localhost/nidria_demo_sandbox"},
        {"mock_email": False},
        {"mock_storage": False},
        {"mock_services": False},
        {"mock_lemlist": False},
        {"scheduler_enabled": True},
        {"billing_checkout_enabled": True},
        {"resend_monitor_enabled": True},
        {"resend_api_key": "forbidden"},
        {"paddle_api_key": "forbidden"},
        {"supabase_service_role_key": "forbidden"},
        {"docuseal_api_key": "forbidden"},
        {"ai_translation_api_key": "forbidden"},
        {"jwt_expat_secret": "short"},
    ],
)
def test_refuses_unsafe_runtime_configuration(change: dict[str, object]) -> None:
    with pytest.raises(RuntimeError):
        assert_isolated_settings(isolated_settings().model_copy(update=change))


def test_safe_config_and_no_production_auth_change() -> None:
    assert_isolated_settings(isolated_settings())
    assert all(getattr(route, "path", None) != "/demo/session" for route in production_app.routes)


@pytest.mark.asyncio
async def test_forty_real_portals_and_access_boundaries(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = load_catalog()
    monkeypatch.setattr(get_settings(), "signatures_enabled", True)
    assert await seed_catalog(db_session, catalog) == {"created": 40, "already_present": 0}
    assert await seed_catalog(db_session, catalog) == {"created": 0, "already_present": 40}
    sandbox = create_sandbox_app(catalog)

    async def test_db():  # type: ignore[no-untyped-def]
        yield db_session

    sandbox.dependency_overrides[get_db] = test_db
    async with AsyncClient(
        transport=ASGITransport(app=sandbox), base_url="http://demo.test"
    ) as client:
        sessions = []
        for record in catalog.records:
            opened = await client.post("/demo/session", json={"code": record.public_code})
            assert opened.status_code == 200, opened.text
            session = opened.json()
            assert session["language"] == record.language
            assert session["read_only"] is True
            assert "refresh_token" not in session
            sessions.append(session)
        assert len({session["case_id"] for session in sessions}) == 40

        for index, (record, session) in enumerate(zip(catalog.records, sessions, strict=True)):
            headers = {
                "Authorization": f"Bearer {session['access_token']}",
                "Accept-Language": record.language,
            }
            case_id = session["case_id"]
            me = await client.get("/auth/expat/me", headers=headers)
            assert me.status_code == 200, me.text
            assert me.json()["preferred_lang"] == record.language
            listing = await client.get("/expat/cases", headers=headers)
            assert listing.status_code == 200, listing.text
            assert [row["id"] for row in listing.json()] == [case_id]
            assert listing.json()[0]["agency"]["name"] == record.name
            assert listing.json()[0]["steps_done"] == 2
            assert listing.json()[0]["steps_total"] == 7
            detail = await client.get(f"/expat/cases/{case_id}", headers=headers)
            assert detail.status_code == 200, detail.text
            documents = await client.get(f"/expat/cases/{case_id}/documents", headers=headers)
            assert documents.status_code == 200, documents.text
            assert len(documents.json()) == 2
            for document in documents.json():
                download = await client.get(
                    f"/expat/cases/{case_id}/documents/{document['id']}/download", headers=headers
                )
                assert download.status_code == 200, download.text
                assert download.content.startswith(b"%PDF-")
            signatures = await client.get(f"/expat/cases/{case_id}/signatures", headers=headers)
            assert signatures.status_code == 200, signatures.text
            assert len(signatures.json()) == 1
            assert signatures.json()[0]["status"] == "signed"
            assert signatures.json()[0]["embed_slug"] is None
            reminders = await client.get(f"/expat/cases/{case_id}/notifications", headers=headers)
            assert reminders.status_code == 200 and len(reminders.json()) == 1
            foreign_id = sessions[(index + 1) % 40]["case_id"]
            for path in (f"/expat/cases/{foreign_id}", f"/expat/cases/{foreign_id}/documents"):
                denied = await client.get(path, headers=headers)
                assert denied.status_code == 404, denied.text
            assert (
                await client.post(f"/expat/cases/{case_id}/documents", headers=headers)
            ).status_code == 403
            assert (await client.get("/admin/tasks", headers=headers)).status_code == 404
            assert (await client.post("/billing/checkout", headers=headers)).status_code == 403
            assert (await client.post("/auth/agent/login", headers=headers)).status_code == 403
            assert listing.headers["Cache-Control"] == "no-store"
            assert "noindex" in listing.headers["X-Robots-Tag"]
        assert (await client.post("/demo/session", json={"code": "0" * 32})).status_code == 404
        assert (await client.post("/demo/session", json={"code": "../"})).status_code == 422
        assert (await client.get("/expat/cases")).status_code == 401
        regular_token = create_access_token(str(uuid.uuid4()), Audience.EXPAT)
        assert (
            await client.get("/expat/cases", headers={"Authorization": f"Bearer {regular_token}"})
        ).status_code == 401
        # Even an accidental second case assigned to a demo user disables its link.
        existing = (await db_session.scalars(select(ClientCase).limit(1))).one()
        second = ClientCase(
            agency_id=existing.agency_id, principal_expat_user_id=existing.principal_expat_user_id
        )
        db_session.add(second)
        await db_session.commit()
        agency = await db_session.get(Agency, existing.agency_id)
        assert agency is not None
        record = catalog.by_id(agency.settings["sandbox_record"])
        assert record is not None
        assert (
            await client.post("/demo/session", json={"code": record.public_code})
        ).status_code == 404
        assert await db_session.scalar(select(func.count()).select_from(Agency)) == 40
