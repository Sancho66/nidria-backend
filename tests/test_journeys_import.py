"""POST /journeys/import - the AI-JSON journey import (Eric's spec,
arbitrated v1: steps only).

Covers: (a) the spec's example JSON creates a conforming journey
(steps, prerequisites, participants, requirements + declared fields,
content note) with the arbitrated mentions (volet A ignored, attachments
ignored, agency participants to assign, provider slot to name);
(b) prerequisite cycle -> 422 dedicated code; (c) unknown field type /
select without options -> 422 with the faulty path; (d) volet A ignored,
no section created; (e) an email in a label -> CREATED with a warning;
(f) preview writes nothing; (g) partial import: one invalid step is
rejected (and its dependents cascade) while the coherent ones are
created, exact report; (h) prestataire:avocat -> a typed slot in the
report, no invented member, no participant row."""

import copy
import json
import pathlib
import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from shared.models.journey import (
    JourneySection,
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateField,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.rbac import Role
from shared.models.step_requirement import StepRequirement
from shared.models.usage import UsageEvent
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

# The spec's example JSON (Eric, 2026-07-07), verbatim structure.
SPEC_EXAMPLE: dict[str, Any] = {
    "version": 1,
    "parcours": {
        "nom": {"fr": "Création de société + résidence", "en": "Company setup + residency"},
        "langue_par_defaut": "fr",
        "informations_creation": {
            "sections": [
                {
                    "titre": {"fr": "État civil"},
                    "champs": [
                        {
                            "cle": "nom_naissance",
                            "type": "text",
                            "requis": True,
                            "libelle": {"fr": "Nom de naissance"},
                        }
                    ],
                }
            ]
        },
        "etapes": [
            {
                "ref": "depot_dossier",
                "nom": {"fr": "Dépôt du dossier initial"},
                "delai_jours": 5,
                "validee_par": "agence",
                "participants": [
                    {"acteur": "client", "role": "fournit_documents"},
                    {"acteur": "agence", "role": "executant"},
                ],
                "prerequis": [],
                "informations_a_collecter": [
                    {
                        "cle": "passeport",
                        "type": "text",
                        "requis": True,
                        "libelle": {"fr": "Numéro de passeport"},
                    }
                ],
                "informations_fournies": {
                    "note": {"fr": "Merci de fournir une copie couleur de votre passeport."},
                    "pieces_jointes": [{"nom": {"fr": "Modèle de mandat"}}],
                },
            },
            {
                "ref": "traduction",
                "nom": {"fr": "Traduction certifiée"},
                "delai_jours": 10,
                "validee_par": "personne",
                "participants": [{"acteur": "prestataire:traducteur", "role": "executant"}],
                "prerequis": ["depot_dossier"],
                "informations_a_collecter": [],
                "informations_fournies": {
                    "note": {"fr": "La traduction certifiée sera déposée ici."}
                },
            },
        ],
    },
}


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


def _codes(items: list[dict[str, Any]]) -> set[str]:
    return {w["code"] for w in items}


# --- (a) + (d) + (h): the spec example creates a conforming journey -----------------


async def test_spec_example_creates_a_conforming_journey(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    agents_before = (await db_session.execute(select(func.count(Agent.id)))).scalar_one()

    response = await client.post("/journeys/import", headers=headers, json=SPEC_EXAMPLE)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["created"] is True and report["template_id"]
    assert report["name"] == "Création de société + résidence"
    assert [(s["ref"], s["position"], s["fields"]) for s in report["steps_created"]] == [
        ("depot_dossier", 0, 1),
        ("traduction", 1, 0),
    ]
    assert report["steps_ignored"] == []
    # Participants summary + the typed provider slot (h): nothing invented.
    assert report["participants"]["client"] == 1
    assert report["participants"]["agency"] == 1
    assert report["participants"]["external_slots"] == [
        {"job": "traducteur", "steps": ["traduction"]}
    ]
    # The arbitrated mentions.
    codes = _codes(report["warnings"])
    assert "import_ai.sections_ignored" in codes  # volet A ignored (d)
    assert "import_ai.attachments_ignored" in codes  # a JSON carries no file
    assert "import_ai.agency_participants_to_assign" in codes
    assert "import_ai.external_provider_to_name" in codes
    assert "import_ai.personal_data_suspected" not in codes
    assert "import_ai.labels_normalized" not in codes  # strict JSON: no coercion (d)

    tid = uuid.UUID(report["template_id"])
    template = await db_session.get(JourneyTemplate, tid)
    assert template is not None and template.agency_id == admin.agency_id
    assert template.name_i18n["en"] == "Company setup + residency"

    steps = list(
        (
            await db_session.execute(
                select(JourneyTemplateStep)
                .where(JourneyTemplateStep.template_id == tid)
                .order_by(JourneyTemplateStep.position)
            )
        ).scalars()
    )
    assert [s.name for s in steps] == ["Dépôt du dossier initial", "Traduction certifiée"]
    assert [s.estimated_days for s in steps] == [5, 10]
    assert [s.default_validated_by_type for s in steps] == ["agent", "none"]
    assert [s.completion_mode for s in steps] == ["agency_validation", "auto"]
    assert steps[0].content_note == "Merci de fournir une copie couleur de votre passeport."
    assert steps[0].content_note_i18n["fr"] == steps[0].content_note

    prereqs = list(
        (
            await db_session.execute(
                select(StepPrerequisite).where(StepPrerequisite.step_id.in_([s.id for s in steps]))
            )
        ).scalars()
    )
    assert [(p.step_id, p.prerequisite_step_id) for p in prereqs] == [(steps[1].id, steps[0].id)]

    participants = list(
        (
            await db_session.execute(
                select(JourneyStepParticipant).where(
                    JourneyStepParticipant.step_id.in_([s.id for s in steps])
                )
            )
        ).scalars()
    )
    by_step = {steps[0].id: [], steps[1].id: []}
    for p in participants:
        by_step[p.step_id].append((p.type, p.agent_id, p.role))
    assert sorted(by_step[steps[0].id]) == [
        ("agent", None, "executant"),  # the agency in general, to assign
        ("expat", None, "provides_documents"),  # the client
    ]
    assert by_step[steps[1].id] == []  # provider slot: NO participant row (h)
    assert (
        await db_session.execute(select(func.count(Agent.id)))
    ).scalar_one() == agents_before  # no member invented (h)

    # Collected field: custom definition + declared on the template +
    # step requirement (the membership rule).
    definition = (
        await db_session.execute(
            select(CustomFieldDefinition).where(
                CustomFieldDefinition.agency_id == admin.agency_id,
                CustomFieldDefinition.key == "passeport",
            )
        )
    ).scalar_one()
    assert definition.field_type == "text" and definition.required is True
    declared = (
        await db_session.execute(
            select(JourneyTemplateField).where(JourneyTemplateField.template_id == tid)
        )
    ).scalar_one()
    assert (declared.kind, declared.reference) == ("custom_field", "passeport")
    requirement = (
        await db_session.execute(
            select(StepRequirement).where(StepRequirement.step_id == steps[0].id)
        )
    ).scalar_one()
    assert (requirement.kind, requirement.reference, requirement.scope) == (
        "custom_field",
        "passeport",
        "principal",
    )

    # No section is created in v1 (volet A ignored).
    sections = (
        await db_session.execute(
            select(func.count(JourneySection.id)).where(JourneySection.template_id == tid)
        )
    ).scalar_one()
    assert sections == 0

    event = (
        await db_session.execute(
            select(UsageEvent).where(UsageEvent.event_type == "journey.imported_from_ai")
        )
    ).scalar_one()
    assert event.details["steps"] == 2 and event.details["warnings"] >= 3


# --- Postel tolerance: plain-string labels are normalized ---------------------------


async def test_plain_string_labels_are_normalized(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """(a) + (b): strings and objects mix freely wherever a label is
    expected; everything lands under langue_par_defaut and the report
    carries the soft mention."""
    payload = {
        "version": 1,
        "parcours": {
            "nom": "Parcours en chaines simples",  # string
            "langue_par_defaut": "fr",
            "etapes": [
                {
                    "ref": "e1",
                    "nom": "Etape en chaine",  # string
                    "informations_a_collecter": [
                        {
                            "cle": "situation",
                            "type": "select_single",
                            "libelle": "Situation familiale",  # string
                            "options": [
                                "Célibataire",  # string
                                {"fr": "Marié(e)", "en": "Married"},  # object (mixed)
                            ],
                        }
                    ],
                    "informations_fournies": {"note": "Une note en chaine simple."},
                }
            ],
        },
    }
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["created"] is True
    normalized = next(w for w in report["warnings"] if w["code"] == "import_ai.labels_normalized")
    assert normalized["valeur"] == "5"  # nom + etape.nom + libelle + 1 option + note

    tid = uuid.UUID(report["template_id"])
    template = await db_session.get(JourneyTemplate, tid)
    assert template is not None and template.name == "Parcours en chaines simples"
    step = (
        await db_session.execute(
            select(JourneyTemplateStep).where(JourneyTemplateStep.template_id == tid)
        )
    ).scalar_one()
    assert step.name_i18n == {"fr": "Etape en chaine"}
    assert step.content_note == "Une note en chaine simple."
    definition = (
        await db_session.execute(
            select(CustomFieldDefinition).where(
                CustomFieldDefinition.agency_id == admin.agency_id,
                CustomFieldDefinition.key == "situation",
            )
        )
    ).scalar_one()
    assert definition.options == ["Célibataire", "Marié(e)"]


async def test_string_label_without_fr_pivot_still_rejected(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """The fr rule applies AFTER normalization: langue_par_defaut=en
    puts the string under en, and the missing fr pivot still rejects."""
    payload = {
        "version": 1,
        "parcours": {
            "nom": "English only journey",
            "langue_par_defaut": "en",
            "etapes": [{"ref": "e1", "nom": {"fr": "Etape"}}],
        },
    }
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "import_ai.name_missing"


async def test_non_string_label_keeps_the_exact_path_rejection(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """(c): a number where a label is expected is NOT coerced - the
    exact-path error is unchanged."""
    payload = {
        "version": 1,
        "parcours": {
            "nom": {"fr": "X"},
            "etapes": [
                {
                    "ref": "e1",
                    "nom": {"fr": "Etape"},
                    "informations_a_collecter": [{"cle": "age", "type": "number", "libelle": 42}],
                }
            ],
        },
    }
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "import_ai.label_invalid"
    assert body["params"]["chemin"] == "parcours.etapes[0].informations_a_collecter[0].libelle"
    assert body["params"]["valeur"] == "42"


# --- Rich options {valeur, libelle}: normalized to their label ----------------------


async def test_alexandre_real_json_imports_fully(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """(a): the REAL 12-step JSON (Alexandre's AI test, 2026-07-07, 21
    rich {valeur, libelle} options) imports with ZERO rejected step."""
    payload = json.loads(
        (pathlib.Path(__file__).parent / "assets" / "ai_import_bulgarie_real.json").read_text()
    )
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["created"] is True
    assert len(report["steps_created"]) == 12
    assert report["steps_ignored"] == []
    normalized = next(w for w in report["warnings"] if w["code"] == "import_ai.labels_normalized")
    assert int(normalized["valeur"]) >= 21  # every rich option counted

    # The rich options landed as their FR labels (key dropped cleanly).
    definition = (
        await db_session.execute(
            select(CustomFieldDefinition).where(
                CustomFieldDefinition.agency_id == admin.agency_id,
                CustomFieldDefinition.key == "type_siege",
            )
        )
    ).scalar_one()
    assert definition.options is not None and len(definition.options) == 3
    assert definition.options[0] == "Service de domiciliation (recommandé)"
    assert all(isinstance(option, str) for option in definition.options)


async def test_rich_option_with_plain_string_label(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """(b): rich form whose label is itself a plain string."""
    payload = {
        "version": 1,
        "parcours": {
            "nom": {"fr": "Riche"},
            "etapes": [
                {
                    "ref": "e1",
                    "nom": {"fr": "Etape"},
                    "informations_a_collecter": [
                        {
                            "cle": "forme",
                            "type": "select_single",
                            "libelle": {"fr": "Forme"},
                            "options": [
                                {"valeur": "eood", "libelle": "EOOD"},
                                {"value": "ood", "label": {"fr": "OOD"}},
                            ],
                        }
                    ],
                }
            ],
        },
    }
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["created"] is True
    definition = (
        await db_session.execute(
            select(CustomFieldDefinition).where(
                CustomFieldDefinition.agency_id == admin.agency_id,
                CustomFieldDefinition.key == "forme",
            )
        )
    ).scalar_one()
    assert definition.options == ["EOOD", "OOD"]


async def test_rich_option_without_any_label_keeps_exact_path(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """(c): a rich option carrying NO label at all keeps the exact-path
    rejection."""
    payload = {
        "version": 1,
        "parcours": {
            "nom": {"fr": "X"},
            "etapes": [
                {
                    "ref": "e1",
                    "nom": {"fr": "Etape"},
                    "informations_a_collecter": [
                        {
                            "cle": "forme",
                            "type": "select_single",
                            "libelle": {"fr": "Forme"},
                            "options": [{"valeur": "eood"}],
                        }
                    ],
                }
            ],
        },
    }
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "import_ai.label_fr_missing"
    assert body["params"]["chemin"] == (
        "parcours.etapes[0].informations_a_collecter[0].options[0].fr"
    )


# --- (b) prerequisite cycle -> 422 --------------------------------------------------


async def test_prerequisite_cycle_is_a_422(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    payload = {
        "version": 1,
        "parcours": {
            "nom": {"fr": "Cycle"},
            "etapes": [
                {"ref": "a", "nom": {"fr": "A"}, "prerequis": ["b"]},
                {"ref": "b", "nom": {"fr": "B"}, "prerequis": ["a"]},
            ],
        },
    }
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "import_ai.prerequisite_cycle"
    assert "a" in response.json()["params"]["valeur"]


# --- (c) unknown type / select without options -> 422 with the path -----------------


async def test_invalid_field_type_and_empty_select_are_422_with_path(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    bad_type = {
        "version": 1,
        "parcours": {
            "nom": {"fr": "X"},
            "etapes": [
                {
                    "ref": "e1",
                    "nom": {"fr": "Etape"},
                    "informations_a_collecter": [
                        {"cle": "sig", "type": "signature", "libelle": {"fr": "Signature"}}
                    ],
                }
            ],
        },
    }
    response = await client.post("/journeys/import", headers=headers, json=bad_type)
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "import_ai.invalid_field_type"
    assert body["params"]["chemin"] == "parcours.etapes[0].informations_a_collecter[0].type"
    assert body["params"]["valeur"] == "signature"

    empty_select = copy.deepcopy(bad_type)
    empty_select["parcours"]["etapes"][0]["informations_a_collecter"][0] = {
        "cle": "statut",
        "type": "select_single",
        "libelle": {"fr": "Statut"},
        "options": [],
    }
    response = await client.post("/journeys/import", headers=headers, json=empty_select)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "import_ai.select_options_missing"
    assert body["params"]["chemin"] == "parcours.etapes[0].informations_a_collecter[0].options"


# --- (e) a label with an email -> created WITH a warning ----------------------------


async def test_personal_data_warns_but_never_blocks(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    payload = copy.deepcopy(SPEC_EXAMPLE)
    payload["parcours"]["etapes"][0]["informations_a_collecter"][0]["libelle"]["fr"] = (
        "Passeport de jean.dupont@gmail.com"
    )
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["created"] is True
    warning = next(
        w for w in report["warnings"] if w["code"] == "import_ai.personal_data_suspected"
    )
    assert warning["valeur"] == "jean.dupont@gmail.com"
    assert warning["chemin"].endswith("informations_a_collecter[0].libelle.fr")


# --- (f) preview -> zero write -------------------------------------------------------


async def test_preview_writes_nothing(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    counts = {}
    for name, model in (
        ("templates", JourneyTemplate),
        ("defs", CustomFieldDefinition),
        ("events", UsageEvent),
    ):
        counts[name] = (await db_session.execute(select(func.count(model.id)))).scalar_one()

    response = await client.post(
        "/journeys/import?preview=true", headers=agent_headers(admin), json=SPEC_EXAMPLE
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["created"] is False and report["template_id"] is None
    assert len(report["steps_created"]) == 2  # the honest preview summary
    assert report["participants"]["external_slots"] == [
        {"job": "traducteur", "steps": ["traduction"]}
    ]

    db_session.expire_all()
    for name, model in (
        ("templates", JourneyTemplate),
        ("defs", CustomFieldDefinition),
        ("events", UsageEvent),
    ):
        after = (await db_session.execute(select(func.count(model.id)))).scalar_one()
        assert after == counts[name], name


# --- (g) partial import: exact report ------------------------------------------------


async def test_partial_import_keeps_the_coherent_steps(
    client: AsyncClient, db_session: AsyncSession, admin: Agent, agent_headers: AuthHeaders
) -> None:
    payload = {
        "version": 1,
        "parcours": {
            "nom": {"fr": "Partiel"},
            "etapes": [
                {"ref": "ok1", "nom": {"fr": "Etape valide"}, "delai_jours": 3},
                {
                    "ref": "bad",
                    "nom": {"fr": "Etape invalide"},
                    "participants": [{"acteur": "client", "role": "superviseur"}],
                },
                {"ref": "ok2", "nom": {"fr": "Dependante"}, "prerequis": ["bad"]},
            ],
        },
    }
    response = await client.post("/journeys/import", headers=agent_headers(admin), json=payload)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["created"] is True
    assert [s["ref"] for s in report["steps_created"]] == ["ok1"]
    assert [(i["ref"], i["code"], i["valeur"]) for i in report["steps_ignored"]] == [
        ("bad", "import_ai.invalid_role", "superviseur"),
        ("ok2", "import_ai.prerequisite_rejected", "bad"),  # cascade, with mention
    ]

    tid = uuid.UUID(report["template_id"])
    created = list(
        (
            await db_session.execute(
                select(JourneyTemplateStep.name).where(JourneyTemplateStep.template_id == tid)
            )
        ).scalars()
    )
    assert created == ["Etape valide"]


# --- globally invalid JSONs -> clean 422 ---------------------------------------------


async def test_globally_invalid_json_is_a_clean_422(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    no_name = {"version": 1, "parcours": {"nom": {"en": "No french"}, "etapes": []}}
    response = await client.post("/journeys/import", headers=headers, json=no_name)
    assert response.status_code == 422
    assert response.json()["code"] == "import_ai.name_missing"

    no_steps = {"version": 1, "parcours": {"nom": {"fr": "Vide"}, "etapes": []}}
    response = await client.post("/journeys/import", headers=headers, json=no_steps)
    assert response.status_code == 422
    assert response.json()["code"] == "import_ai.no_steps"

    # A single invalid step = zero valid step: the step's own error
    # surfaces as THE 422 (not a generic wrapper).
    only_bad = {
        "version": 1,
        "parcours": {
            "nom": {"fr": "X"},
            "etapes": [{"ref": "e", "nom": {"fr": "E"}, "delai_jours": -2}],
        },
    }
    response = await client.post("/journeys/import", headers=headers, json=only_bad)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "import_ai.invalid_delay"
    assert body["params"]["valeur"] == "-2"
