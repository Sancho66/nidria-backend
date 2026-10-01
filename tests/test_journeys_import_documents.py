"""AI-journey import: documents to provide and to sign (01/10, constat du
29/09). The format carried only information to collect: a « document » field
type rejected the step, a « documents » key vanished silently, and the AI put
« P60 » in a text field — a box to fill instead of a place to drop the file.
Covers: `documents_a_fournir` becomes DOCUMENT requirements (label in the
journey language, principal or each person); a document marked to sign is
created as a deposit and listed in `signatures_to_configure` (a signable
requirement needs a template); an optional document is skipped WITH a
warning (every requirement blocks its step); every unknown key is reported
with its path; invalid values reject their step with a named code."""

import copy
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.journey import JourneyTemplateStep
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


PAYLOAD: dict[str, Any] = {
    "version": 1,
    "parcours": {
        "nom": {"en": "UK tax residency"},
        "langue_par_defaut": "en",
        "secteur": "tax",  # unknown at the journey level
        "etapes": [
            {
                "ref": "collect",
                "nom": {"en": "Document collection"},
                "delai_jours": 10,
                "validee_par": "agence",
                "participants": [{"acteur": "client", "role": "fournit_documents"}],
                "prerequis": [],
                "informations_a_collecter": [
                    {
                        "cle": "arrival_date",
                        "type": "date",
                        "requis": True,
                        "libelle": "Arrival date",
                    }
                ],
                "documents_a_fournir": [
                    {"libelle": {"en": "P60"}},
                    {"libelle": {"en": "Passport copy"}, "pour": "chaque_personne"},
                    "Bank statement",  # a plain string is accepted as the label
                    {"libelle": {"en": "Engagement letter"}, "signature": "ses"},
                    {"libelle": {"en": "Tax power of attorney"}, "signature": "qes"},
                    {"libelle": {"en": "Utility bill"}, "requis": False},
                    {"libelle": {"en": "P60"}},  # the same document twice: one ask
                    {"libelle": {"en": "Payslips"}, "format": "pdf"},  # unknown key
                ],
                "documents": ["Payslip"],  # the key the AI invents: ignored, SAID
            }
        ],
    },
}


def _codes(items: list[dict[str, Any]]) -> list[str]:
    return [item["code"] for item in items]


async def test_preview_lists_documents_signatures_and_ignored_keys(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await client.post(
        "/journeys/import?preview=true", headers=agent_headers(admin), json=PAYLOAD
    )
    assert response.status_code == 200, response.text
    report = response.json()
    [step] = report["steps_created"]
    assert step["fields"] == 1
    assert step["documents"] == 6  # optional skipped, duplicate merged

    assert report["signatures_to_configure"] == [
        {
            "step_ref": "collect",
            "step_name": "Document collection",
            "label": "Engagement letter",
            "level": "ses",
        },
        {
            "step_ref": "collect",
            "step_name": "Document collection",
            "label": "Tax power of attorney",
            "level": "qes",
        },
    ]
    warnings = report["warnings"]
    unknown = {w["chemin"] for w in warnings if w["code"] == "import_ai.unknown_key_ignored"}
    assert unknown == {
        "parcours.secteur",
        "parcours.etapes[0].documents",
        "parcours.etapes[0].documents_a_fournir[7].format",
    }
    assert {
        "code": "import_ai.optional_document_skipped",
        "chemin": "parcours.etapes[0].documents_a_fournir[5]",
        "valeur": "Utility bill",
    } in warnings
    assert {
        "code": "import_ai.signature_level_not_available",
        "chemin": "parcours.etapes[0].documents_a_fournir[4].signature",
        "valeur": "qes",
    } in warnings


async def test_creation_makes_document_requirements_and_signables_stay_deposits(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=PAYLOAD)
    assert response.status_code == 200, response.text
    assert response.json()["created"] is True
    template_id = response.json()["template_id"]
    step = (
        await db_session.execute(
            select(JourneyTemplateStep).where(JourneyTemplateStep.template_id == template_id)
        )
    ).scalar_one()
    rows = (
        (
            await db_session.execute(
                select(StepRequirement)
                .where(StepRequirement.step_id == step.id)
                .order_by(StepRequirement.position)
            )
        )
        .scalars()
        .all()
    )
    docs = [r for r in rows if r.kind == "document"]
    assert [(d.reference, d.scope) for d in docs] == [
        ("P60", "principal"),
        ("Passport copy", "each_person"),
        ("Bank statement", "principal"),
        ("Engagement letter", "principal"),
        ("Tax power of attorney", "principal"),
        ("Payslips", "principal"),
    ]
    # A signable requirement is born WITH its template: none can be invented.
    assert not any(d.signature_required for d in docs)
    assert all(d.document_template_id is None for d in docs)
    # Positions continue after the collected field.
    assert [r.position for r in rows] == list(range(len(rows)))


@pytest.mark.parametrize(
    ("document", "code", "chemin"),
    [
        (
            {"libelle": "ID", "pour": "everyone"},
            "import_ai.invalid_document_scope",
            "parcours.etapes[0].documents_a_fournir[0].pour",
        ),
        (
            {"libelle": "ID", "signature": "wet_ink"},
            "import_ai.invalid_signature_level",
            "parcours.etapes[0].documents_a_fournir[0].signature",
        ),
        (
            {"pour": "client"},
            "import_ai.label_invalid",
            "parcours.etapes[0].documents_a_fournir[0].libelle",
        ),
    ],
)
async def test_invalid_document_rejects_its_step_with_a_named_code(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    document: dict[str, Any],
    code: str,
    chemin: str,
) -> None:
    payload = copy.deepcopy(PAYLOAD)
    payload["parcours"]["etapes"][0]["documents_a_fournir"] = [document]
    payload["parcours"]["etapes"].append(
        {"ref": "other", "nom": {"en": "Other"}, "validee_par": "agence"}
    )
    response = await client.post(
        "/journeys/import?preview=true", headers=agent_headers(admin), json=payload
    )
    assert response.status_code == 200, response.text
    [ignored] = response.json()["steps_ignored"]
    assert (ignored["code"], ignored["chemin"]) == (code, chemin)


async def test_too_long_label_is_skipped_with_a_warning(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    payload = copy.deepcopy(PAYLOAD)
    long_label = "Certified copy " * 10  # 150 characters
    payload["parcours"]["etapes"][0]["documents_a_fournir"] = [{"libelle": long_label}]
    report = (
        await client.post(
            "/journeys/import?preview=true", headers=agent_headers(admin), json=payload
        )
    ).json()
    assert report["steps_created"][0]["documents"] == 0
    assert "import_ai.document_label_too_long" in _codes(report["warnings"])
