"""Lot export (10/08) — GET /agencies/me/export, « partir avec ses données ».

Covers, against the real app: the ZIP shape + CSV content (the import
symmetry), the billing-wall traversal (a GET leaves with the data even when
the subscription is blocked), the admin-only gate, and the privacy gating
(confidential notes + billed amounts excluded for a role lacking the
dedicated permissions, with the README saying so)."""

import csv
import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.activity import ActivityLog
from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_note import CaseNote
from shared.models.client_profile import ClientProfile
from shared.models.company_profile import CompanyProfile
from shared.models.custom_field import CustomFieldDefinition
from shared.models.rbac import Role
from src.core.rbac.permissions import Permission
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _read_zip(content: bytes) -> dict[str, str]:
    archive = zipfile.ZipFile(io.BytesIO(content))
    return {name: archive.read(name).decode("utf-8-sig") for name in archive.namelist()}


async def test_export_returns_a_zip_of_csvs(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_client_case: MakeClientCase,
) -> None:
    aid = admin.agency_id
    db_session.add(
        CustomFieldDefinition(
            agency_id=aid,
            key="couleur",
            label="Couleur préférée",
            field_type="text",
            scope="person",
            position=0,
        )
    )
    db_session.add(
        ClientProfile(
            agency_id=aid,
            first_name="Jean",
            last_name="Dupont",
            email="jean@example.com",
            custom_fields={"couleur": "bleu"},
        )
    )
    db_session.add(CompanyProfile(agency_id=aid, name="ACME SARL"))
    await db_session.commit()
    case = await make_client_case(agency_id=aid, reference="D-001")
    db_session.add(
        ActivityLog(
            case_id=case.id, actor_type="agent", action_type="case.created", details={"n": 1}
        )
    )
    db_session.add(CaseNote(case_id=case.id, body="Note interne", is_confidential=False))
    await db_session.commit()

    resp = await client.get("/agencies/me/export", headers=agent_headers(admin))
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "application/zip"
    assert ".zip" in resp.headers["content-disposition"]

    files = _read_zip(resp.content)
    assert set(files) == {
        "fiches-personnes.csv",
        "fiches-societes.csv",
        "dossiers.csv",
        "dossiers-activite.csv",
        "dossiers-notes.csv",
        "LISEZ-MOI.txt",
    }
    persons = list(csv.reader(io.StringIO(files["fiches-personnes.csv"])))
    assert "Couleur préférée" in persons[0]  # the custom field label is a header
    assert any("Jean" in row and "bleu" in row for row in persons[1:])  # value on the row
    assert "ACME SARL" in files["fiches-societes.csv"]
    assert "D-001" in files["dossiers.csv"]
    assert "case.created" in files["dossiers-activite.csv"]  # the history/timeline
    assert "Note interne" in files["dossiers-notes.csv"]
    assert "téléchargeables dossier par dossier" in files["LISEZ-MOI.txt"]
    assert "Montant facturé" in files["dossiers.csv"]  # admin holds cost.view


async def test_export_traverses_the_billing_wall(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    """The point of the lot: a blocked agency still leaves with its data.
    A GET is never stopped by the billing lock (it only blocks writes)."""
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None
    agency.trial_ends_at = datetime.now(UTC) - timedelta(days=1)  # trial expired = blocked
    await db_session.commit()

    resp = await client.get("/agencies/me/export", headers=agent_headers(admin))
    assert resp.status_code == 200, resp.text


async def test_export_is_admin_only(
    client: AsyncClient,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    system_roles: dict[str, Role],
) -> None:
    viewer = await make_agent(role=system_roles["viewer"])
    resp = await client.get("/agencies/me/export", headers=agent_headers(viewer))
    assert resp.status_code == 403, resp.text


async def test_confidential_notes_and_costs_are_gated(
    client: AsyncClient,
    db_session: AsyncSession,
    make_agent: MakeAgent,
    agent_headers: AuthHeaders,
    make_role,
    make_client_case: MakeClientCase,
) -> None:
    """A role with agency.manage but NOT cost.view / note.view_confidential:
    the export works, but the billed columns and the confidential notes are
    excluded — the README says so."""
    manager = await make_agent()  # empty role, fresh agency
    role = await make_role(agency_id=manager.agency_id, permissions=[Permission.AGENCY_MANAGE])
    manager.role_id = role.id
    await db_session.commit()

    case = await make_client_case(agency_id=manager.agency_id, reference="D-2")
    db_session.add(CaseNote(case_id=case.id, body="note-secrete-zzz", is_confidential=True))
    db_session.add(CaseNote(case_id=case.id, body="note-publique-zzz", is_confidential=False))
    await db_session.commit()

    resp = await client.get("/agencies/me/export", headers=agent_headers(manager))
    assert resp.status_code == 200, resp.text
    files = _read_zip(resp.content)
    assert "note-publique-zzz" in files["dossiers-notes.csv"]
    assert "note-secrete-zzz" not in files["dossiers-notes.csv"]  # confidential filtered
    assert "Montant facturé" not in files["dossiers.csv"]  # no cost.view
    assert "confidentielles" in files["LISEZ-MOI.txt"].lower()
