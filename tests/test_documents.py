"""Documents battery: mock-aware storage, strict key sanitization with
faithful display name, polymorphic uploader, validation, delete rules
(agent: all / expat: own non-OK uploads), expat ownership 404-never-403."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agent import Agent
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.rbac import Role
from src.core import storage
from src.core.config import get_settings
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

PDF_BYTES = b"%PDF-1.4 fake content"


@pytest.fixture
def docs_client(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def member(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["member"])


@pytest_asyncio.fixture
async def expat(make_expat_user: MakeExpatUser) -> ExpatUser:
    return await make_expat_user(email="owner@example.com")


@pytest_asyncio.fixture
async def case(member: Agent, expat: ExpatUser, make_client_case: MakeClientCase) -> ClientCase:
    return await make_client_case(agency_id=member.agency_id, principal_expat_user_id=expat.id)


def _file(name: str = "passport.pdf", content: bytes = PDF_BYTES) -> dict[str, tuple[str, bytes]]:
    return {"file": (name, content)}


# --- agent upload --------------------------------------------------------------------


async def test_agent_upload_full_flow(
    docs_client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    response = await docs_client.post(
        f"/cases/{case.id}/documents", headers=agent_headers(member), files=_file()
    )
    assert response.status_code == 201
    body = response.json()
    assert body["filename"] == "passport.pdf"
    assert body["uploaded_by_type"] == "agent"
    assert body["uploaded_by_id"] == str(member.id)
    assert body["validation_status"] is None  # not reviewed yet
    assert "storage_path" not in body  # technical, never exposed

    # Stored in the (mock) storage under the case-prefixed key.
    [path] = storage.mock_store.keys()
    assert path.startswith(f"{case.id}/")
    assert storage.mock_store[path] == PDF_BYTES

    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "document.uploaded")
        )
    ).scalar_one()
    assert log.actor_type == "agent"
    assert log.details["filename"] == "passport.pdf"


async def test_filename_sanitized_in_key_faithful_in_db(
    docs_client: AsyncClient,
    member: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    evil_name = "../../données du client/паспорт évil.pdf"
    response = await docs_client.post(
        f"/cases/{case.id}/documents",
        headers=agent_headers(member),
        files=_file(name=evil_name),
    )
    assert response.status_code == 201
    # Displayed faithfully…
    assert response.json()["filename"] == evil_name
    # …stored safely: no traversal, ASCII-only key, case prefix intact.
    [path] = storage.mock_store.keys()
    assert ".." not in path
    key_filename = path.split("/")[-1]
    assert key_filename.isascii()
    assert "/" not in key_filename
    assert key_filename.endswith(".pdf")
    assert path.startswith(f"{case.id}/")


async def test_disallowed_extension_422(
    docs_client: AsyncClient, member: Agent, case: ClientCase, agent_headers: AuthHeaders
) -> None:
    response = await docs_client.post(
        f"/cases/{case.id}/documents",
        headers=agent_headers(member),
        files=_file(name="malware.exe"),
    )
    assert response.status_code == 422
    assert storage.mock_store == {}


async def test_case_documents_still_reject_txt_and_md(
    docs_client: AsyncClient, member: Agent, case: ClientCase, agent_headers: AuthHeaders
) -> None:
    """Proves the task-attachment whitelist (which DOES take txt/md) is
    NOT shared with case documents: a .txt/.md on a dossier stays 422."""
    for name in ("notes.txt", "brief.md"):
        response = await docs_client.post(
            f"/cases/{case.id}/documents",
            headers=agent_headers(member),
            files=_file(name=name, content=b"x"),
        )
        assert response.status_code == 422, response.text
    assert storage.mock_store == {}


async def test_size_limit_413(
    docs_client: AsyncClient,
    member: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "max_document_size_mb", 0)
    response = await docs_client.post(
        f"/cases/{case.id}/documents", headers=agent_headers(member), files=_file()
    )
    assert response.status_code == 413


async def test_step_progress_attachment_validated(
    docs_client: AsyncClient,
    member: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    response = await docs_client.post(
        f"/cases/{case.id}/documents",
        headers=agent_headers(member),
        files=_file(),
        data={"step_progress_id": str(uuid.uuid4())},  # not of this case
    )
    assert response.status_code == 422


# --- expat side: ownership ----------------------------------------------------------------


async def test_expat_upload_on_own_case(
    docs_client: AsyncClient,
    db_session: AsyncSession,
    expat: ExpatUser,
    case: ClientCase,
    expat_headers: AuthHeaders,
) -> None:
    response = await docs_client.post(
        f"/expat/cases/{case.id}/documents", headers=expat_headers(expat), files=_file()
    )
    assert response.status_code == 201
    body = response.json()
    assert body["uploaded_by_type"] == "expat"
    assert body["uploaded_by_id"] == str(expat.id)
    # First EXPAT actor in the audit trail.
    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "document.uploaded")
        )
    ).scalar_one()
    assert log.actor_type == "expat"
    assert log.actor_id == expat.id


async def test_expat_foreign_case_is_404_never_403(
    docs_client: AsyncClient,
    case: ClientCase,
    make_expat_user: MakeExpatUser,
    expat_headers: AuthHeaders,
) -> None:
    stranger = await make_expat_user(email="stranger@example.com")
    headers = expat_headers(stranger)
    upload = await docs_client.post(
        f"/expat/cases/{case.id}/documents", headers=headers, files=_file()
    )
    listing = await docs_client.get(f"/expat/cases/{case.id}/documents", headers=headers)
    assert upload.status_code == 404
    assert listing.status_code == 404


async def test_expat_endpoints_require_expat_token(
    docs_client: AsyncClient, member: Agent, case: ClientCase, agent_headers: AuthHeaders
) -> None:
    response = await docs_client.get(
        f"/expat/cases/{case.id}/documents", headers=agent_headers(member)
    )
    assert response.status_code == 401


async def test_expat_sees_all_case_documents(
    docs_client: AsyncClient,
    member: Agent,
    expat: ExpatUser,
    case: ClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    # Agency deposits a piece FOR the client…
    await docs_client.post(
        f"/cases/{case.id}/documents",
        headers=agent_headers(member),
        files=_file(name="contract.pdf"),
    )
    # …and the expat sees it alongside their own upload.
    await docs_client.post(
        f"/expat/cases/{case.id}/documents",
        headers=expat_headers(expat),
        files=_file(name="id-card.jpg"),
    )
    listing = await docs_client.get(
        f"/expat/cases/{case.id}/documents", headers=expat_headers(expat)
    )
    assert {d["filename"] for d in listing.json()} == {"contract.pdf", "id-card.jpg"}


# --- download (streamed proxy) ----------------------------------------------------------------


async def test_download_both_audiences(
    docs_client: AsyncClient,
    member: Agent,
    expat: ExpatUser,
    case: ClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    uploaded = (
        await docs_client.post(
            f"/cases/{case.id}/documents", headers=agent_headers(member), files=_file()
        )
    ).json()
    agent_dl = await docs_client.get(
        f"/cases/{case.id}/documents/{uploaded['id']}/download",
        headers=agent_headers(member),
    )
    assert agent_dl.status_code == 200
    assert agent_dl.content == PDF_BYTES
    assert agent_dl.headers["content-type"] == "application/pdf"

    expat_dl = await docs_client.get(
        f"/expat/cases/{case.id}/documents/{uploaded['id']}/download",
        headers=expat_headers(expat),
    )
    assert expat_dl.status_code == 200
    assert expat_dl.content == PDF_BYTES


async def test_download_non_ascii_filename(
    docs_client: AsyncClient,
    member: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Regression: a filename with a curly apostrophe / accents (HTTP headers
    are latin-1) must NOT 500. Content-Disposition carries an ASCII fallback +
    a UTF-8 filename* (RFC 6266)."""
    name = "Capture d’écran à 16h.png"
    uploaded = (
        await docs_client.post(
            f"/cases/{case.id}/documents",
            headers=agent_headers(member),
            files=_file(name=name),
        )
    ).json()
    dl = await docs_client.get(
        f"/cases/{case.id}/documents/{uploaded['id']}/download",
        headers=agent_headers(member),
    )
    assert dl.status_code == 200  # not 500
    cd = dl.headers["content-disposition"]
    cd.encode("latin-1")  # the header is wire-encodable
    assert "filename*=UTF-8''" in cd  # the real (accented) name is preserved


# --- validation -----------------------------------------------------------------------------------


async def test_validation_flow_with_log(
    docs_client: AsyncClient,
    db_session: AsyncSession,
    member: Agent,
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    uploaded = (
        await docs_client.post(
            f"/cases/{case.id}/documents", headers=agent_headers(member), files=_file()
        )
    ).json()
    response = await docs_client.patch(
        f"/cases/{case.id}/documents/{uploaded['id']}/validation",
        headers=agent_headers(member),  # member holds document.validate
        json={"validation_status": "ok", "expires_at": "2030-01-01T00:00:00Z"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["validation_status"] == "ok"
    assert body["expires_at"].startswith("2030-01-01")

    log = (
        await db_session.execute(
            select(ActivityLog).where(ActivityLog.action_type == "document.validated")
        )
    ).scalar_one()
    assert log.details["old"] is None
    assert log.details["new"] == "ok"


async def test_validation_requires_permission(
    docs_client: AsyncClient,
    member: Agent,
    case: ClientCase,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    uploaded = (
        await docs_client.post(
            f"/cases/{case.id}/documents", headers=agent_headers(member), files=_file()
        )
    ).json()
    viewer = await make_agent(agency_id=member.agency_id, role=system_roles["viewer"])
    response = await docs_client.patch(
        f"/cases/{case.id}/documents/{uploaded['id']}/validation",
        headers=agent_headers(viewer),
        json={"validation_status": "ok"},
    )
    assert response.status_code == 403


# --- delete rules ---------------------------------------------------------------------------------


async def test_agent_deletes_any_document(
    docs_client: AsyncClient,
    member: Agent,
    expat: ExpatUser,
    case: ClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    uploaded = (
        await docs_client.post(
            f"/expat/cases/{case.id}/documents", headers=expat_headers(expat), files=_file()
        )
    ).json()
    response = await docs_client.delete(
        f"/cases/{case.id}/documents/{uploaded['id']}", headers=agent_headers(member)
    )
    assert response.status_code == 200
    assert storage.mock_store == {}  # blob gone too
    listing = await docs_client.get(f"/cases/{case.id}/documents", headers=agent_headers(member))
    assert listing.json() == []


async def test_expat_delete_rules(
    docs_client: AsyncClient,
    member: Agent,
    expat: ExpatUser,
    case: ClientCase,
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    headers = expat_headers(expat)
    # Own upload, to_fix → deletable (replace flow).
    own = (
        await docs_client.post(f"/expat/cases/{case.id}/documents", headers=headers, files=_file())
    ).json()
    await docs_client.patch(
        f"/cases/{case.id}/documents/{own['id']}/validation",
        headers=agent_headers(member),
        json={"validation_status": "to_fix"},
    )
    assert (
        await docs_client.delete(f"/expat/cases/{case.id}/documents/{own['id']}", headers=headers)
    ).status_code == 200

    # Own upload validated OK → frozen.
    frozen = (
        await docs_client.post(f"/expat/cases/{case.id}/documents", headers=headers, files=_file())
    ).json()
    await docs_client.patch(
        f"/cases/{case.id}/documents/{frozen['id']}/validation",
        headers=agent_headers(member),
        json={"validation_status": "ok"},
    )
    assert (
        await docs_client.delete(
            f"/expat/cases/{case.id}/documents/{frozen['id']}", headers=headers
        )
    ).status_code == 403

    # Agent-uploaded piece → not theirs to delete.
    agency_doc = (
        await docs_client.post(
            f"/cases/{case.id}/documents", headers=agent_headers(member), files=_file()
        )
    ).json()
    assert (
        await docs_client.delete(
            f"/expat/cases/{case.id}/documents/{agency_doc['id']}", headers=headers
        )
    ).status_code == 403


async def test_agent_documents_scoped_to_agency(
    docs_client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    # Foreign agent WITH case.view: the matrix passes, tenant scoping
    # in the Manager must still seal the case (404).
    foreign_agent = await make_agent(role=system_roles["member"])
    response = await docs_client.get(
        f"/cases/{case.id}/documents", headers=agent_headers(foreign_agent)
    )
    assert response.status_code == 404
