"""A case cited in an email notification must read as a HUMAN label — the
client name then the journey name — never the technical UUID (which means
nothing to an adviser). The UUID may still live in the button/link URL.

The 'ready to validate' email (owner agent recipient) was the only template
citing the raw UUID; this battery pins the human label end-to-end plus the
pure `case_label_for_notif` composition (client+journey, i18n, fallbacks).
"""

from httpx import AsyncClient

from shared.models.rbac import Role
from src.core import email
from src.core.i18n import case_label_for_notif
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

# --- the pure label composer ---------------------------------------------------------


def test_label_is_client_then_journey() -> None:
    assert (
        case_label_for_notif("Camille", "Martin", "Création d'un Autónomo en Espagne", None, "fr")
        == "Camille Martin - Création d'un Autónomo en Espagne"
    )


def test_label_resolves_journey_i18n_in_recipient_lang() -> None:
    assert (
        case_label_for_notif("Jean", "Dupont", "Autónomo", {"en": "Self-employed"}, "en")
        == "Jean Dupont - Self-employed"
    )


def test_label_no_journey_falls_back_to_client_alone() -> None:
    assert case_label_for_notif("Camille", "Martin", None, None, "fr") == "Camille Martin"


def test_label_no_client_falls_back_to_journey_alone() -> None:
    assert case_label_for_notif(None, None, "Autónomo", None, "fr") == "Autónomo"


# --- end-to-end: the 'ready to validate' email reads human, not UUID -----------------


async def test_ready_to_validate_email_names_client_and_journey_not_uuid(
    client: AsyncClient,
    rbac_baseline: None,
    make_agent: MakeAgent,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
    expat_headers: AuthHeaders,
) -> None:
    admin = await make_agent(role=system_roles["admin"])
    ah = agent_headers(admin)
    camille = await make_expat_user(
        first_name="Camille", last_name="Martin", email="camille@example.com"
    )

    journey_name = "Création d'un Autónomo en Espagne"
    tid = (await client.post("/journeys", headers=ah, json={"name": journey_name})).json()["id"]
    sid = (
        await client.post(
            f"/journeys/{tid}/steps",
            headers=ah,
            json={"name": "Collecte", "completion_mode": "agency_validation"},
        )
    ).json()["id"]
    # A base-field requirement the client fills → the agency_validation step
    # becomes met → the owner agent gets the 'ready to validate' mail.
    await client.post(
        f"/journeys/{tid}/fields",
        headers=ah,
        json={"kind": "base_field", "reference": "passport_number"},
    )
    await client.post(
        f"/journeys/{tid}/steps/{sid}/requirements",
        headers=ah,
        json={"kind": "base_field", "reference": "passport_number", "scope": "principal"},
    )
    case = await make_client_case(
        agency_id=admin.agency_id, principal_expat_user_id=camille.id, owner_agent_id=admin.id
    )
    steps = (
        await client.post(
            f"/cases/{case.id}/journey", headers=ah, json={"journey_template_id": tid}
        )
    ).json()
    pid = steps[0]["id"]
    await client.patch(f"/cases/{case.id}/steps/{pid}", headers=ah, json={"status": "in_progress"})
    detail = (await client.get(f"/expat/cases/{case.id}", headers=expat_headers(camille))).json()
    rid = next(
        r["id"]
        for s in detail["timeline"]
        for r in s["requirements"]
        if r["reference"] == "passport_number"
    )

    email.outbox.clear()  # drop the activation mail; focus on 'ready to validate'
    await client.put(
        f"/expat/cases/{case.id}/requirements/{rid}",
        headers=expat_headers(camille),
        json={"value": "AB12345"},
    )

    ready = [m for m in email.outbox if "prêt à valider" in m.subject]
    assert len(ready) == 1, [m.subject for m in email.outbox]
    body = ready[0].body
    # The readable sentence now names the client AND the journey...
    assert f"du dossier Camille Martin - {journey_name} ont été" in body
    # ...and NO LONGER the technical UUID in that readable text (the old bug).
    # The UUID may still appear in the button link — that is allowed.
    assert f"du dossier {case.id}" not in body
