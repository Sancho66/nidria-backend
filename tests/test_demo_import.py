"""Packet imports survive interruptions and isolate data errors and access."""

import asyncio
import csv
import io
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pycountry
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.demo_import import jobs, rows
from shared.models.expat_user import ExpatUser
from shared.models.message_template import MessageTemplate
from src.admin.demo_import_schema import DemoImportRequest
from src.admin.demo_schema import AgencyDemoInput
from src.admin.demo_transport import DemoTransport
from src.core.config import get_settings
from src.core.enums import Audience
from src.core.exceptions import ConflictError, ServiceUnavailableError, ValidationError
from src.core.security import create_access_token
from src.demo_sandbox import agency_control, agency_seed
from src.demo_sandbox.agency_repository import AgencyDemoRepository
from src.demo_sandbox.application import create_sandbox_app
from src.demo_sandbox.catalog import load_catalog
from src.demo_sandbox.control import control_db
from src.demo_sandbox.import_csv import parse_import
from src.demo_sandbox.import_manager import DemoImportManager, import_tick
from src.demo_sandbox.import_repository import prepare_imports
from src.demo_sandbox.people import fictional_people
from src.demo_sandbox.prepare_management import prepare_agency_permissions
from tests.test_demo_admin import signed

HEADER = [
    "email",
    "company_name",
    "language",
    "profession",
    "country",
    "city",
    "persona",
    *(f"m2_s{i}" for i in range(1, 7)),
]


def row(email: str = "one@example.com", **overrides: str) -> dict[str, str]:
    return (
        dict(
            zip(
                HEADER,
                [
                    email,
                    "Fictional Practice",
                    "en",
                    "accountant",
                    "GB",
                    "",
                    "employee",
                    *[f"Description of step {i}" for i in range(1, 7)],
                ],
                strict=True,
            )
        )
        | overrides
    )


def csv_text(*items: dict[str, str]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, HEADER, delimiter=";", lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(items)
    return "\ufeff" + stream.getvalue()


def request(*items: dict[str, str], batch: str = "QA-PACKETS") -> DemoImportRequest:
    return DemoImportRequest(
        request_id=uuid.uuid4(), batch=batch, csv_text=csv_text(*items), expected_count=len(items)
    )


def test_csv_counts_quoted_records_blank_cities_and_row_errors() -> None:
    parsed = parse_import(
        csv_text(
            row(m2_s2='A quoted "document";\nnext line'),
            row("bad", language="xx"),
            row("three@example.com", city="Paris"),
        )
    )
    assert [item.number for item in parsed] == [2, 4, 5]
    assert parsed[0].data["city"] == "" and parsed[0].data["persona"] == "employee"
    assert parsed[0].data["steps"][1] == 'A quoted "document";\nnext line'
    assert {issue.column for issue in parsed[1].errors} == {"email", "language"}
    assert parsed[2].data["city"] == "Paris"
    assert not parsed[0].errors and not parsed[2].errors
    duplicate = parse_import(csv_text(row(), row()))
    assert len(duplicate) == 2 and not any(item.errors for item in duplicate)


def test_packet_bounds_headers_and_invalid_row_column_count() -> None:
    assert len(parse_import(csv_text(*[row() for _ in range(500)]))) == 500
    with pytest.raises(ValidationError, match="500"):
        parse_import(csv_text(*[row() for _ in range(501)]))
    with pytest.raises(PydanticValidationError):
        DemoImportRequest(request_id=uuid.uuid4(), batch="QA", csv_text="x", expected_count=501)
    with pytest.raises(PydanticValidationError):
        request(row(), batch="  ")
    for value in ("", "email;email\na;b", ";".join(HEADER) + '\n"unterminated'):
        with pytest.raises(ValidationError):
            parse_import(value)
    parsed = parse_import(csv_text(row()) + "short;row\n")
    assert parsed[-1].errors[0].column == "CSV"


def test_packet_countries_use_the_iso_catalog_and_keep_errors_on_their_row() -> None:
    countries = sorted(country.alpha_2 for country in pycountry.countries)
    parsed = parse_import(csv_text(*(row(country=code.lower()) for code in countries)))
    assert [item.data["country"] for item in parsed] == countries
    for item in parsed:
        assert not item.errors
        people = fictional_people(AgencyDemoInput.model_validate(item.data))
        assert len(set(people.clients + people.team)) == 14
    invalid = parse_import(csv_text(*(row(country=code) for code in ("ZZ", "XX", "UK", "ESP", ""))))
    assert all([issue.column for issue in item.errors] == ["country"] for item in invalid)


@pytest.mark.asyncio
async def test_international_packet_creates_localized_demos_and_reuses_the_links(queue):
    catalog = load_catalog()
    cases = [("es", "CO"), ("en", "AE"), ("fr", "CH")]
    items = [
        row(f"{country.lower()}@example.com", language=lang, country=country, persona="family")
        for lang, country in cases
    ]
    job = await start(queue, request(*items))
    for _ in items:
        assert await import_tick(catalog, queue)
    result = await progress(queue, job.id)
    assert (result.status, result.created, result.failed) == ("completed", 3, 0)
    async with queue() as db:
        agencies = list(await db.scalars(select(Agency)))
        assert {(a.default_language, a.country) for a in agencies} == set(cases)
        for agency in agencies:
            assert agency.settings["sandbox_full_demo"]["country"] == agency.country
            countries = set(
                await db.scalars(
                    select(ClientCase.dest_country).where(ClientCase.agency_id == agency.id)
                )
            )
            assert countries == {agency.country}
        spanish = next(a for a in agencies if a.default_language == "es")
        template = (
            await db.scalars(select(MessageTemplate).where(MessageTemplate.agency_id == spanish.id))
        ).one()
        assert (template.language, template.name) == ("es", "Documentos que faltan")
        links = await DemoImportManager(db).export(job.id, "links")
        assert links.count == 3
    retry = await start(queue, request(*items))
    for _ in items:
        assert await import_tick(catalog, queue)
    second = await progress(queue, retry.id)
    assert (second.created, second.reused, second.failed) == (0, 3, 0)
    async with queue() as db:
        assert (await DemoImportManager(db).export(retry.id, "links")).csv == links.csv


@pytest.fixture
async def queue(db_session, async_engine, monkeypatch, rbac_baseline):
    await prepare_imports(db_session)
    await prepare_agency_permissions(db_session)
    await db_session.commit()
    monkeypatch.setattr(get_settings(), "environment", "demo_sandbox")
    monkeypatch.setattr(agency_control, "hash_password", lambda _: "test-only-password-hash")
    monkeypatch.setattr(agency_seed, "hash_password", lambda _: "test-only-password-hash")

    @asynccontextmanager
    async def transaction():
        async with (
            async_engine.connect() as connection,
            connection.begin(),
            AsyncSession(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ) as db,
        ):
            yield db
            await db.commit()

    yield transaction
    async with transaction() as db:
        await db.execute(delete(rows))
        await db.execute(delete(jobs))


async def start(transaction, payload):
    async with transaction() as db:
        return await DemoImportManager(db).start(uuid.uuid4(), payload)


async def progress(transaction, identifier):
    async with transaction() as db:
        return await DemoImportManager(db).read(identifier)


@pytest.mark.asyncio
async def test_persistent_progress_retry_multiple_packets_and_exports(
    queue, db_session, monkeypatch
):
    catalog = load_catalog()
    payload = request(row(), row("bad"), row("two@example.com"))
    job = await start(queue, payload)
    assert job.total == 3 and job.failed == 1 and job.processed == 1
    assert (await start(queue, payload)).id == job.id
    with pytest.raises(ConflictError):
        await start(queue, payload.model_copy(update={"batch": "OTHER"}))
    with pytest.raises(ConflictError):
        await start(queue, request(row()).model_copy(update={"expected_count": 2}))
    async with queue() as db:
        with pytest.raises(ConflictError):
            await DemoImportManager(db).export(job.id, "links")
        with pytest.raises(ConflictError):
            await DemoImportManager(db).export_batch(payload.batch)
    assert await import_tick(catalog, queue)
    assert (await progress(queue, job.id)).processed == 2
    # A stop after business managers committed savepoints must not leave a
    # partially visible agency. A new worker resumes the SAME pending row.
    original = agency_control.populate_agency

    async def cancelled(*args, **kwargs):
        await original(*args, **kwargs)
        raise asyncio.CancelledError

    monkeypatch.setattr(agency_control, "populate_agency", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await import_tick(catalog, queue)
    assert await db_session.scalar(select(func.count()).select_from(Agency)) == 1
    assert (await progress(queue, job.id)).processed == 2
    monkeypatch.setattr(agency_control, "populate_agency", original)
    assert await import_tick(catalog, queue)
    completed = await progress(queue, job.id)
    assert completed.status == "completed" and completed.created == 2 and completed.failed == 1
    assert not await import_tick(catalog, queue)
    # New request IDs, changed text, mixed case and a duplicate within one
    # packet all reuse the original batch/email identity without modifying it.
    second = await start(
        queue,
        request(
            row("ONE@example.com", company_name="Changed name"),
            row("three@example.com"),
            row("three@example.com"),
        ),
    )
    for _ in range(3):
        assert await import_tick(catalog, queue)
    state = await progress(queue, second.id)
    assert (state.total, state.created, state.reused, state.failed) == (3, 1, 2, 0)
    assert await db_session.scalar(select(func.count()).select_from(Agency)) == 3
    async with queue() as db:
        manager = DemoImportManager(db)
        first_csv = await manager.export(job.id, "links")
        second_csv = await manager.export(second.id, "links")
        errors = await manager.export(job.id, "errors")
        all_csv = await manager.export_batch(payload.batch)
        recent = await manager.recent()
    assert first_csv.count == 2 and second_csv.count == 3 and all_csv.count == 3
    fields = ["email", "company_name", "demo_url", "lot", "expiration"]
    exported = list(csv.DictReader(io.StringIO(second_csv.csv.lstrip("\ufeff")), delimiter=";"))
    assert list(exported[0]) == fields
    assert exported[0]["company_name"] == "Fictional Practice"
    assert exported[1]["demo_url"] == exported[2]["demo_url"]
    assert "email" in errors.csv and errors.count == 1
    assert len(recent.items) == 2
    first_url = next(csv.DictReader(io.StringIO(first_csv.csv.lstrip("\ufeff")), delimiter=";"))[
        "demo_url"
    ]
    assert exported[0]["demo_url"] == first_url
    # The same address in a different named batch is a distinct demo.
    third = await start(queue, request(row(), batch="QA-OTHER"))
    assert await import_tick(catalog, queue)
    assert (await progress(queue, third.id)).created == 1
    assert await db_session.scalar(select(func.count()).select_from(Agency)) == 4
    agencies = list(await db_session.scalars(select(Agency)))
    assert all(agency.country == "GB" and agency.city is None for agency in agencies)
    repository = AgencyDemoRepository(db_session)
    assert await repository.valid_population(agencies)
    assert await repository.valid_population(agencies[:1])
    agent = await db_session.scalar(select(Agent).where(Agent.agency_id == agencies[0].id))
    agent.is_external = True
    await db_session.flush()
    assert not await repository.valid_population(agencies)
    agent.is_external = False
    case = await db_session.scalar(select(ClientCase).where(ClientCase.agency_id == agencies[0].id))
    client = await db_session.get(ExpatUser, case.principal_expat_user_id)
    client.preferred_lang = "fr"
    await db_session.flush()
    assert not await repository.valid_population(agencies)
    client.preferred_lang = "en"
    other_case = await db_session.scalar(
        select(ClientCase).where(ClientCase.agency_id == agencies[1].id)
    )
    previous = other_case.principal_expat_user_id
    other_case.principal_expat_user_id = client.id
    await db_session.flush()
    assert not await repository.valid_population(agencies[:1])
    other_case.principal_expat_user_id = previous
    await db_session.flush()
    assert await repository.valid_population(agencies)


@pytest.mark.asyncio
async def test_creation_error_rolls_back_one_row_and_other_rows_continue(
    queue, db_session, monkeypatch
):
    catalog = load_catalog()
    job = await start(queue, request(row(), row("two@example.com")))
    original = agency_control.populate_agency

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("Private internal error must never enter the export")

    monkeypatch.setattr(agency_control, "populate_agency", fail)
    assert await import_tick(catalog, queue)
    assert await db_session.scalar(select(func.count()).select_from(Agency)) == 0
    monkeypatch.setattr(agency_control, "populate_agency", original)
    assert await import_tick(catalog, queue)
    result = await progress(queue, job.id)
    assert result.status == "completed" and result.created == 1 and result.failed == 1
    assert result.errors[0].row == 2 and result.errors[0].column == "creation"
    assert "Private" not in result.errors[0].reason


@pytest.mark.asyncio
async def test_500_row_packet_is_persisted_without_creating_any_agency(
    queue, db_session, monkeypatch
):
    payload = request(
        *[
            row(f"demo{i}@example.com", **{f"m2_s{j}": "A" * 400 for j in range(1, 7)})
            for i in range(500)
        ]
    )
    body = json.dumps(
        {"operation": "import_start", "payload": payload.model_dump(mode="json")}
    ).encode()
    assert len(body) > 750_000
    monkeypatch.setattr(get_settings(), "demo_admin_enabled", True)
    monkeypatch.setattr(get_settings(), "demo_control_secret", "x" * 64)
    app = create_sandbox_app(load_catalog())
    control = next(route.app for route in app.routes if getattr(route, "path", None) == "/_control")

    async def writer():
        async with queue() as db:
            yield db

    control.dependency_overrides[control_db] = writer
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://demo.test"
    ) as client:
        assert (
            await client.post(
                "/_control/demos", content=body, headers={"Content-Type": "application/json"}
            )
        ).status_code == 403
        response = await client.post(
            "/_control/demos", content=body, headers=signed(body, uuid.uuid4())
        )
    assert response.status_code == 200
    job = await progress(queue, uuid.UUID(response.json()["id"]))
    assert job.total == 500 and job.processed == 0 and job.status == "queued"
    assert (
        await db_session.scalar(
            select(func.count()).select_from(rows).where(rows.c.job_id == job.id)
        )
        == 500
    )
    assert await db_session.scalar(select(func.count()).select_from(Agency)) == 0
    assert (await progress(queue, job.id)).total == 500


@pytest.mark.asyncio
async def test_pause_during_creation_and_resume_only_pending_rows(queue, monkeypatch):
    catalog = load_catalog()
    job = await start(queue, request(row(), row("two@example.com")))
    entered, finish = asyncio.Event(), asyncio.Event()
    original = agency_control.populate_agency

    async def waiting(*args, **kwargs):
        entered.set()
        await finish.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(agency_control, "populate_agency", waiting)
    worker = asyncio.create_task(import_tick(catalog, queue))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        async with queue() as db:
            manager = DemoImportManager(db)
            assert (await manager.set_paused(job.id, True)).status == "paused"
            assert (await manager.set_paused(job.id, True)).status == "paused"
    finally:
        finish.set()
        await worker
    state = await progress(queue, job.id)
    assert (state.status, state.created, state.processed) == ("paused", 1, 1)
    assert not await import_tick(catalog, queue)
    async with queue() as db:
        manager = DemoImportManager(db)
        partial = await manager.export(job.id, "links")
        assert partial.count == 1 and (await manager.export_batch(job.batch)).count == 1
        assert (await manager.set_paused(job.id, False)).status == "queued"
        assert (await manager.set_paused(job.id, False)).status == "queued"
        assert (await manager.export(job.id, "links", partial=True)).csv == partial.csv
        with pytest.raises(ConflictError):
            await manager.export(job.id, "links")
        with pytest.raises(ConflictError):
            await manager.export(job.id, "errors", partial=True)
    assert await import_tick(catalog, queue)
    assert not await import_tick(catalog, queue)
    async with queue() as db:
        manager = DemoImportManager(db)
        state = await manager.set_paused(job.id, True)
        assert (state.status, state.created, state.reused, state.failed) == ("completed", 2, 0, 0)
        full = await manager.export(job.id, "links")
        assert full.count == 2 and full.csv.startswith(partial.csv)


@pytest.mark.asyncio
async def test_read_only_database_returns_retryable_error_and_recovers(queue, async_engine):
    payload = request(row())
    async with async_engine.connect() as con, con.begin():
        await con.execute(text("SET TRANSACTION READ ONLY"))
        async with AsyncSession(bind=con, join_transaction_mode="create_savepoint") as db:
            with pytest.raises(ServiceUnavailableError) as caught:
                await DemoImportManager(db).start(uuid.uuid4(), payload)
            assert caught.value.status_code == 503
            assert caught.value.code == "demo.import_storage_unavailable"
    job = await start(queue, payload)
    assert (await start(queue, payload)).id == job.id
    assert await import_tick(load_catalog(), queue)
    assert (await progress(queue, job.id)).created == 1


@pytest.mark.asyncio
async def test_admin_reports_storage_outage_and_accepts_the_same_request_after_recovery(
    client, make_agent, system_roles, agent_headers, queue, async_engine, monkeypatch
):
    import httpx

    settings = get_settings()
    monkeypatch.setattr(settings, "demo_admin_enabled", True)
    monkeypatch.setattr(settings, "demo_control_secret", "s" * 64)
    monkeypatch.setattr(settings, "demo_control_url", "https://nidria-demo-api.fly.dev")
    sandbox = create_sandbox_app(load_catalog())
    control = next(
        route.app for route in sandbox.routes if getattr(route, "path", None) == "/_control"
    )
    read_only = True

    async def writer():
        async with async_engine.connect() as connection, connection.begin():
            if read_only:
                await connection.execute(text("SET TRANSACTION READ ONLY"))
            async with AsyncSession(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            ) as db:
                yield db
                await db.commit()

    control.dependency_overrides[control_db] = writer
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=ASGITransport(app=sandbox), **kwargs),
    )
    operator = await make_agent(role=system_roles["superadmin"])
    headers = agent_headers(operator)
    path = "/admin/demos/imports"
    input_data = request(row()).model_dump(mode="json")
    previous = await start(queue, request(row("existing@example.com")))
    state = await client.get(f"{path}/{previous.id}", headers=headers)
    assert state.status_code == 200 and state.json()["storage_read_only"] is True
    failure = await client.post(path, json=input_data, headers=headers)
    assert (
        failure.status_code == 503 and failure.json()["code"] == "demo.import_storage_unavailable"
    )
    read_only = False
    created = await client.post(path, json=input_data, headers=headers)
    assert created.status_code == 202 and created.json()["storage_read_only"] is False
    assert (await client.post(path, json=input_data, headers=headers)).json()[
        "id"
    ] == created.json()["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sqlstate", ["25006", "53100"])
async def test_storage_interruption_during_fixture_creation_leaves_work_pending(
    queue, monkeypatch, sqlstate
):
    job = await start(queue, request(row()))

    class StorageError(Exception):
        pass

    cause = StorageError("Storage temporarily unavailable")
    cause.sqlstate = sqlstate

    async def unavailable(*args, **kwargs):
        raise DBAPIError(None, None, cause, False)

    monkeypatch.setattr(agency_control, "populate_agency", unavailable)
    with pytest.raises(DBAPIError):
        await import_tick(load_catalog(), queue)
    state = await progress(queue, job.id)
    assert (state.processed, state.failed, state.created) == (0, 0, 0)


@pytest.mark.asyncio
async def test_worker_lock_prevents_concurrent_creation(queue, db_session, monkeypatch):
    catalog = load_catalog()
    await start(queue, request(row()))
    await start(queue, request(row()))
    entered, release = asyncio.Event(), asyncio.Event()
    original = agency_control.populate_agency

    async def paused(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(agency_control, "populate_agency", paused)
    first = asyncio.create_task(import_tick(catalog, queue))
    try:
        await asyncio.wait_for(entered.wait(), 15)
        assert not await import_tick(catalog, queue)
    finally:
        release.set()
        await first
    assert await import_tick(catalog, queue)
    assert await db_session.scalar(select(func.count()).select_from(Agency)) == 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("rbac_baseline")
async def test_import_endpoints_are_superadmin_only(
    client: AsyncClient, make_agent, system_roles, agent_headers, monkeypatch
):
    identifier = uuid.uuid4()
    now = datetime.now(UTC).isoformat()
    sample = {
        "id": str(identifier),
        "batch": "QA",
        "lifetime_days": 60,
        "status": "completed",
        "total": 1,
        "processed": 1,
        "created": 1,
        "reused": 0,
        "failed": 0,
        "created_at": now,
        "updated_at": now,
        "errors": [],
    }
    calls = []

    async def transport(self, actor_id, operation, payload):
        calls.append((actor_id, operation))
        if operation == "import_list":
            return {"items": [sample]}
        if operation in {"import_export", "import_export_batch"}:
            return {"csv": "email;company_name;demo_url;lot;expiration\r\n", "count": 0}
        return sample

    monkeypatch.setattr(DemoTransport, "call", transport)
    root = await make_agent(role=system_roles["superadmin"])
    admin = await make_agent(role=system_roles["admin"])
    endpoints = [
        ("POST", "/admin/demos/imports", request(row()).model_dump(mode="json")),
        ("GET", "/admin/demos/imports", None),
        ("GET", f"/admin/demos/imports/{identifier}", None),
        ("PATCH", f"/admin/demos/imports/{identifier}", {"paused": True}),
        ("GET", f"/admin/demos/imports/{identifier}/export?kind=errors", None),
        ("GET", "/admin/demos/imports/batch-export?batch=QA", None),
    ]
    expat = {"Authorization": "Bearer " + create_access_token(str(uuid.uuid4()), Audience.EXPAT)}
    for method, path, body in endpoints:
        for headers, status in [({}, 401), (agent_headers(admin), 403), (expat, 401)]:
            assert (
                await client.request(method, path, json=body, headers=headers)
            ).status_code == status
    assert not calls
    for method, path, body in endpoints:
        response = await client.request(method, path, json=body, headers=agent_headers(root))
        assert response.status_code == (202 if method == "POST" else 200), response.text
    assert len(calls) == len(endpoints) and all(actor == root.id for actor, _ in calls)
