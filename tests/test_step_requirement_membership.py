"""Strict membership (BLOC): a step requirement (base_field / custom_field /
case_field) may only reference a field DECLARED in the SAME template's
Informations tab. Applies to NEW requests only — existing rows and deep-clone
copies are written directly and never re-checked. document = free label,
exempt."""

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent


@pytest.fixture
def mc(client: AsyncClient, rbac_baseline: None) -> AsyncClient:
    return client


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _template_step(mc: AsyncClient, ah: dict[str, str]) -> tuple[str, str]:
    tid = (await mc.post("/journeys", headers=ah, json={"name": "T"})).json()["id"]
    sid = (await mc.post(f"/journeys/{tid}/steps", headers=ah, json={"name": "S"})).json()["id"]
    return tid, sid


async def test_a_field_declared_in_informations_can_be_requested(
    mc: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _template_step(mc, ah)
    # Declared in the Informations tab first.
    f = await mc.post(
        f"/journeys/{tid}/fields",
        headers=ah,
        json={"kind": "base_field", "reference": "nationality"},
    )
    assert f.status_code == 201, f.text
    r = await mc.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=ah,
        json={"kind": "base_field", "reference": "nationality", "scope": "principal"},
    )
    assert r.status_code == 201, r.text


async def test_b_catalog_field_not_in_informations_is_refused(
    mc: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    ah = agent_headers(admin)
    tid, sid = await _template_step(mc, ah)
    # passport_number IS in the global catalog but NOT in this template's
    # Informations tab → refused with a clear message.
    r = await mc.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=ah,
        json={"kind": "base_field", "reference": "passport_number", "scope": "principal"},
    )
    assert r.status_code == 422
    assert "Informations" in r.json()["detail"]

    # Same rule for case fields.
    rc = await mc.post(
        f"/journeys/{tid}/steps/{sid}/case-requirements",
        headers=ah,
        json={"case_field": "origin_country"},
    )
    assert rc.status_code == 422
    assert "Informations" in rc.json()["detail"]


async def test_b_document_is_exempt(
    mc: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    # A document requirement is a free label with no referential → never gated.
    ah = agent_headers(admin)
    tid, sid = await _template_step(mc, ah)
    r = await mc.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=ah,
        json={"kind": "document", "reference": "Acte notarié", "scope": "principal"},
    )
    assert r.status_code == 201, r.text


async def test_c_existing_non_conforming_requirement_stays_readable(
    mc: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    """A requirement inserted BEFORE the rule (here simulated by a direct DB
    write, referencing an undeclared field) is never re-validated: it remains
    listed and readable."""
    ah = agent_headers(admin)
    tid, sid = await _template_step(mc, ah)
    db_session.add(
        StepRequirement(
            step_id=uuid.UUID(sid),
            kind="base_field",
            reference="passport_number",  # never declared in Informations
            scope="principal",
            position=0,
        )
    )
    await db_session.commit()

    listed = await mc.get(f"/journeys/{tid}/steps/{sid}/requirements", headers=ah)
    assert listed.status_code == 200
    assert [r["reference"] for r in listed.json()] == ["passport_number"]


async def test_d_clone_of_non_conforming_template_does_not_fail(
    mc: AsyncClient, admin: Agent, db_session: AsyncSession, agent_headers: AuthHeaders
) -> None:
    """A deep clone copies requirements directly (not via add_requirement), so
    cloning an 'old' template whose requirement references an undeclared field
    succeeds — the rule only bites on hand edits made afterwards."""
    ah = agent_headers(admin)
    tid, sid = await _template_step(mc, ah)
    db_session.add(
        StepRequirement(
            step_id=uuid.UUID(sid),
            kind="base_field",
            reference="passport_number",
            scope="principal",
            position=0,
        )
    )
    await db_session.commit()

    clone = await mc.post(f"/journeys/{tid}/clone", headers=ah, json={})
    assert clone.status_code == 201, clone.text
    clone_id = clone.json()["id"]
    detail = (await mc.get(f"/journeys/{clone_id}", headers=ah)).json()
    cloned_step = detail["steps"][0]["id"]
    reqs = (
        await mc.get(f"/journeys/{clone_id}/steps/{cloned_step}/requirements", headers=ah)
    ).json()
    assert [r["reference"] for r in reqs] == ["passport_number"]  # copied as-is
