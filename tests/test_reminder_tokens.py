"""Lot 14/08 — LES VARIABLES DE RELANCE, DÉCOUVRABLES.

Trois jetons étaient résolus ({client_name}, {step_name}, {days_left}) et
RIEN ne les nommait : l'agence qui écrit dans la modale « Programmer un
rappel » ne pouvait pas savoir qu'ils existent.

LA LOGIQUE EST L'INVERSE de celle des conditions générales, et les tests
ci-dessous la gardent : une relance est interpolée AU FIGEAGE (création /
édition) et part une fois — un contrat, lui, résout à la LECTURE. D'où
l'EXEMPLE et non la valeur courante au catalogue, et d'où l'aperçu, qui doit
dire AVANT ce que le figeage ferait."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.journey import JourneyTemplateStep
from shared.models.rbac import Role
from shared.models.reminder import Reminder
from src.core.config import get_settings
from src.reminders.reminder_tokens import CATALOGUE, GROUPS
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

_FUTURE = datetime.now(UTC) + timedelta(days=3)


@pytest_asyncio.fixture
async def manager_agent(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["case_manager"])


@pytest_asyncio.fixture
async def case_with_client(
    manager_agent: Agent, make_client_case: MakeClientCase, make_expat_user: MakeExpatUser
) -> ClientCase:
    expat = await make_expat_user(first_name="Jean", last_name="Martin")
    return await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )


async def _post(client: AsyncClient, agent: Agent, headers: AuthHeaders, case_id, body: str):
    return await client.post(
        f"/cases/{case_id}/reminders",
        headers=headers(agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "message_body": body,
        },
    )


# --- le catalogue servi ------------------------------------------------------------


async def test_the_catalogue_is_served_with_labels_and_EXAMPLES(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    """Le contrat sert les jetons — le front n'en devine aucun. Et il sert un
    EXEMPLE, pas la valeur courante : quand l'agence écrit, il n'y a pas de
    client en face, et il y en aura un différent à chaque envoi."""
    served = (await client.get("/agencies/me", headers=agent_headers(manager_agent))).json()
    tokens = {t["name"]: t for t in served["reminder_tokens"]}

    assert set(tokens) == {
        # QUI LIT — les jetons des formules d'adresse.
        "recipient_name",
        "recipient_first_name",
        # LE TITULAIRE du dossier, quel que soit le lecteur.
        "case_client_name",
        "case_client_first_name",
        "step_name",
        "days_left",
        # Les jetons d'AGENCE : une relance signée par l'agence doit pouvoir
        # la nommer.
        "agency_name",
        "contact_email",
        "contact_phone",
        "step_due_date",
        "client_space_link",
    }
    # Les DÉPRÉCIÉS ne sont plus proposés — ils se résolvent encore (le test
    # dédié plus bas le grave), mais on cesse de pousser un nom ambigu.
    assert "client_name" not in tokens
    assert "client_first_name" not in tokens
    # Insérable tel quel, accolades comprises (un jeton retapé est un jeton perdu).
    assert tokens["recipient_name"]["token"] == "{recipient_name}"
    assert tokens["recipient_name"]["label"] == "le nom de la personne qui reçoit ce message"
    # Deux spécimens DIFFÉRENTS : c'est ce qui rend la distinction visible à
    # l'écriture. Marie lit le message, Jean est le titulaire du dossier.
    assert tokens["recipient_name"]["example"] == "Marie Dupont"
    assert tokens["case_client_name"]["example"] == "Jean Dupont"
    # …VALEUR RÉELLE pour ce qui n'en dépend pas : l'agence lit son propre nom.
    agency = await db_session.get(Agency, manager_agent.agency_id)
    assert agency is not None
    assert tokens["agency_name"]["example"] == agency.name
    # Un champ d'agence vide retombe sur un spécimen — jamais un exemple vide.
    assert tokens["contact_email"]["example"] == "contact@votre-agence.fr"


async def test_a_filled_agency_field_becomes_its_own_example(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Rempli, le champ d'agence EST son exemple : l'agence voit ce que ses
    clients liront, pas un spécimen inventé. Le spécimen n'est qu'un repli."""
    admin = await make_agent(role=system_roles["admin"])
    headers = agent_headers(admin)

    before = {
        t["name"]: t["example"]
        for t in (await client.get("/agencies/me", headers=headers)).json()["reminder_tokens"]
    }
    assert before["contact_phone"] == "+33 1 23 45 67 89"  # repli, le champ est vide

    patched = await client.patch(
        "/agencies/me", headers=headers, json={"contact_phone": "+595 21 123 456"}
    )
    assert patched.status_code == 200, patched.text
    after = {t["name"]: t["example"] for t in patched.json()["reminder_tokens"]}
    assert after["contact_phone"] == "+595 21 123 456"  # le vrai, servi dans la réponse même


# --- le figeage --------------------------------------------------------------------


async def test_agency_name_is_frozen_into_the_message(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    """Le jeton d'agence, résolu au figeage comme ses voisins de dossier."""
    agency = await db_session.get(Agency, manager_agent.agency_id)
    assert agency is not None
    response = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "Bonjour {client_name}, l'équipe {agency_name} vous écrit.",
    )
    assert response.status_code == 201, response.text
    assert (
        response.json()["message_body"]
        == f"Bonjour Jean Martin, l'équipe {agency.name} vous écrit."
    )


async def test_an_empty_agency_field_is_a_named_422_not_a_hole(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Un champ d'agence vide scellerait un TROU dans un message qui ne part
    qu'une fois : même refus qu'une variable de dossier non résoluble, et le
    422 est NOMMÉ (il ne l'était pas — il tombait sur `internal_error`)."""
    refused = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "Écrivez-nous à {contact_email}.",
    )
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert body["code"] == "reminder.variable_unresolvable"
    assert body["params"] == {"variable": "contact_email", "reason": "agency_field_empty"}


async def test_the_unresolvable_422_is_named_for_case_variables_too(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Non-régression du constat : ce 422 portait `internal_error` (la
    catégorie par défaut de NidriaError), donc le front ne pouvait pas
    s'y accrocher. Il porte maintenant son nom et sa raison."""
    refused = await _post(
        client, manager_agent, agent_headers, case_with_client.id, "Étape {step_name} en attente."
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "reminder.variable_unresolvable"
    assert refused.json()["params"] == {"variable": "step_name", "reason": "step_required"}


async def test_an_unknown_token_freezes_VERBATIM_and_does_not_refuse(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """LA RÈGLE RÉELLE, gravée : un jeton inconnu ne lève RIEN — il traverse
    et se fige tel quel dans le message. C'est pourquoi il doit être signalé
    à l'ÉDITION (l'aperçu ci-dessous) : après le figeage, il est trop tard,
    le client lira « {tva} »."""
    created = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "TVA {tva} — bonjour {client_name}.",
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "TVA {tva} — bonjour Jean Martin."


# --- l'aperçu ----------------------------------------------------------------------


async def test_preview_without_a_case_renders_the_examples(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    """Écrire un modèle de message n'exige pas d'avoir un client sous la
    main : sans dossier, l'aperçu montre les spécimens."""
    agency = await db_session.get(Agency, manager_agent.agency_id)
    assert agency is not None
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={"content": "Bonjour {client_name}, de la part de {agency_name}."},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["rendered"] == f"Bonjour Jean Dupont, de la part de {agency.name}."
    assert preview.json()["unknown_tokens"] == []
    assert preview.json()["unresolvable_tokens"] == []


async def test_preview_names_the_unknown_tokens_before_the_freeze(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """La coquille est dite À L'ÉDITION, dans l'ordre de lecture, sans
    doublon — et le texte la garde verbatim, exactement comme le figeage."""
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={"content": "TVA {tva}, {client_nam}, encore {tva}, et {client_name}."},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["unknown_tokens"] == ["{tva}", "{client_nam}"]
    assert "{tva}" in body["rendered"] and "Jean Dupont" in body["rendered"]


async def test_preview_with_a_case_renders_the_REAL_values(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Avec un dossier en face, l'aperçu montre le vrai client — c'est la
    MÊME résolution que le figeage, donc il ne peut pas le flatter."""
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={"content": "Bonjour {client_name}.", "case_id": str(case_with_client.id)},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["rendered"] == "Bonjour Jean Martin."


async def test_preview_announces_what_the_freeze_would_refuse(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """LE POINT DU LOT : le 422 ne doit jamais surprendre au moment
    d'enregistrer. L'aperçu nomme le jeton ET la raison, avec la même
    résolution — et rend quand même une phrase entière (spécimen en repli),
    pour que l'agence lise son message au lieu d'un trou."""
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={
            "content": "Étape {step_name}, écrivez à {contact_email}.",
            "case_id": str(case_with_client.id),
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    reasons = {t["name"]: t["reason"] for t in body["unresolvable_tokens"]}
    assert reasons == {"step_name": "step_required", "contact_email": "agency_field_empty"}
    assert all(t["token"] == "{" + t["name"] + "}" for t in body["unresolvable_tokens"])
    # Le refus annoncé est EXACTEMENT celui que le figeage produit.
    refused = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "Étape {step_name}, écrivez à {contact_email}.",
    )
    assert refused.status_code == 422
    assert refused.json()["params"]["variable"] in reasons

    # L'aperçu reste lisible malgré tout : spécimens en repli, pas de trou.
    assert "Dépôt du dossier" in body["rendered"]
    assert "contact@votre-agence.fr" in body["rendered"]


async def test_preview_refuses_a_case_of_another_agency(
    client: AsyncClient,
    manager_agent: Agent,
    make_agency,
    make_client_case: MakeClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Scopé par tenant comme tout le reste : un dossier d'une autre agence
    n'existe pas pour cet agent."""
    other = await make_agency(slug="autre-agence")
    foreign = await make_client_case(agency_id=other.id)
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={"content": "Bonjour {client_name}.", "case_id": str(foreign.id)},
    )
    assert preview.status_code == 404, preview.text


async def test_preview_is_gated_like_the_write_it_previews(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Même permission que la création de rappel dont il est l'aperçu — un
    rôle sans reminder.create ne l'atteint pas (deny par défaut)."""
    viewer = await make_agent(role=system_roles["viewer"])
    denied = await client.post(
        "/reminders/preview",
        headers=agent_headers(viewer),
        json={"content": "Bonjour {client_name}."},
    )
    assert denied.status_code == 403, denied.text


# ── LES MODÈLES DE MESSAGE (lot 14/08) ────────────────────────────────────────
#
# APPLIQUER UN MODÈLE COPIE SON TEXTE. La règle était déjà vraie — le corps du
# rappel est figé à la création — mais RIEN ne la protégeait. C'est exactement
# le genre d'invariant qu'un refactor casse en silence : il suffirait qu'une
# lecture aille chercher `template.body` au lieu de `reminder.message_body`
# pour qu'un texte approuvé par un humain change sous lui, sans erreur, sans
# trace. Gravé ici.


async def test_editing_a_template_never_rewrites_a_reminder_already_created(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    """Le modèle est une SOURCE, pas un lien. Une relance approuvée est un
    texte qu'un humain a validé : il ne doit pas changer sous lui."""
    headers = agent_headers(manager_agent)

    created_template = await client.post(
        "/message-templates",
        headers=headers,
        json={"name": "Relance douce", "body": "Bonjour {client_name}, des nouvelles ?"},
    )
    assert created_template.status_code == 201, created_template.text
    template_id = created_template.json()["id"]

    reminder = await client.post(
        f"/cases/{case_with_client.id}/reminders",
        headers=headers,
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "message_template_id": template_id,
        },
    )
    assert reminder.status_code == 201, reminder.text
    reminder_id = reminder.json()["id"]
    # Le jeton est résolu au FIGEAGE, pas laissé au modèle.
    assert reminder.json()["message_body"] == "Bonjour Jean Martin, des nouvelles ?"
    # Le lien reste comme PROVENANCE — il ne propage rien (la suite le prouve).
    assert reminder.json()["message_template_id"] == template_id

    # On approuve : le texte est désormais validé par un humain.
    approved = await client.post(f"/reminders/{reminder_id}/approve", headers=headers)
    assert approved.status_code == 200, approved.text

    # PUIS on réécrit le modèle de fond en comble.
    edited = await client.patch(
        f"/message-templates/{template_id}",
        headers=headers,
        json={"body": "TEXTE ENTIÈREMENT DIFFÉRENT, jamais approuvé par personne."},
    )
    assert edited.status_code == 200, edited.text

    # La relance n'a pas bougé — ni son texte, ni son approbation.
    after = await client.get(f"/reminders/{reminder_id}", headers=headers)
    assert after.status_code == 200
    assert after.json()["message_body"] == "Bonjour Jean Martin, des nouvelles ?"
    assert after.json()["status"] == "approved"

    # Et la lecture en base dit la même chose que l'API (aucune résolution
    # tardive qui irait rechercher le modèle).
    db_session.expire_all()
    row = await db_session.get(Reminder, uuid.UUID(reminder_id))
    assert row is not None
    assert row.message_body == "Bonjour Jean Martin, des nouvelles ?"


async def test_deleting_a_template_leaves_the_reminder_text_intact(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Le cas extrême de la même règle : le modèle DISPARAÎT, le texte reste.
    La FK est SET NULL, donc la provenance s'efface — pas le message."""
    headers = agent_headers(manager_agent)
    template_id = (
        await client.post(
            "/message-templates",
            headers=headers,
            json={"name": "Jetable", "body": "Bonjour {client_name}."},
        )
    ).json()["id"]
    reminder_id = (
        await client.post(
            f"/cases/{case_with_client.id}/reminders",
            headers=headers,
            json={
                "channel": "mail",
                "scheduled_at": _FUTURE.isoformat(),
                "recipient_type": "expat",
                "message_template_id": template_id,
            },
        )
    ).json()["id"]

    assert (
        await client.delete(f"/message-templates/{template_id}", headers=headers)
    ).status_code == 200

    after = (await client.get(f"/reminders/{reminder_id}", headers=headers)).json()
    assert after["message_body"] == "Bonjour Jean Martin."
    assert after["message_template_id"] is None  # provenance effacée, texte intact


async def test_the_two_labels_are_stored_served_and_removable(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """Langue et canal : des ÉTIQUETTES. Elles se posent, se servent, se
    retirent (`null` explicite) — et ne contraignent rien."""
    headers = agent_headers(manager_agent)
    created = await client.post(
        "/message-templates",
        headers=headers,
        json={
            "name": "Recordatorio",
            "body": "Hola {client_name}.",
            "language": "es",
            "channel": "whatsapp",
        },
    )
    assert created.status_code == 201, created.text
    assert (created.json()["language"], created.json()["channel"]) == ("es", "whatsapp")
    template_id = created.json()["id"]

    listed = (await client.get("/message-templates", headers=headers)).json()
    mine = next(t for t in listed if t["id"] == template_id)
    assert (mine["language"], mine["channel"]) == ("es", "whatsapp")
    assert mine["created_at"] and mine["updated_at"]  # l'écran de gestion date les lignes

    # On RETIRE les étiquettes : `null` explicite, pas une absence.
    cleared = await client.patch(
        f"/message-templates/{template_id}",
        headers=headers,
        json={"language": None, "channel": None},
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["language"] is None and cleared.json()["channel"] is None
    assert cleared.json()["body"] == "Hola {client_name}."  # le corps n'a pas bougé


async def test_an_explicit_null_never_blanks_a_required_field(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """`name` et `body` sont NOT NULL. Un `null` explicite passait la
    validation et tombait en 500 sur l'insert — il est désormais ignoré,
    comme une absence. (Trou préexistant, rendu atteignable par le PATCH
    partiel des étiquettes.)"""
    headers = agent_headers(manager_agent)
    template_id = (
        await client.post(
            "/message-templates",
            headers=headers,
            json={"name": "Intact", "body": "Bonjour."},
        )
    ).json()["id"]

    patched = await client.patch(
        f"/message-templates/{template_id}",
        headers=headers,
        json={"name": None, "body": None, "language": "fr"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Intact"
    assert patched.json()["body"] == "Bonjour."
    assert patched.json()["language"] == "fr"


async def test_a_template_is_never_resolved_as_such(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    """Un modèle STOCKE le jeton, il ne le résout pas — la résolution est
    l'affaire du figeage. Sauf l'aperçu, qui montre les valeurs d'exemple."""
    headers = agent_headers(manager_agent)
    body = "Bonjour {client_name}, de la part de {agency_name}."
    created = await client.post(
        "/message-templates", headers=headers, json={"name": "Type", "body": body}
    )
    assert created.status_code == 201, created.text
    assert created.json()["body"] == body  # servi VERBATIM, jetons compris

    agency = await db_session.get(Agency, manager_agent.agency_id)
    assert agency is not None
    preview = await client.post(
        "/reminders/preview", headers=headers, json={"content": created.json()["body"]}
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["rendered"] == f"Bonjour Jean Dupont, de la part de {agency.name}."


# ── LES TROIS JETONS DU SECOND PASSAGE (14/08) ────────────────────────────────
#
# Un gratuit et deux SOUS CONDITION. La condition n'est pas un ornement : le
# figeage sait QUI lira (une lecture, elle, ne le saurait pas), donc il peut
# refuser AVANT qu'un message parte — vers une date muette ou vers un mur.
#
#   {client_first_name}  gratuit  — le principal est déjà lu, son prénom est NOT NULL.
#   {step_due_date}      condition DONNÉE — `due_at` est nullable → 422 nommé.
#   {client_space_link}  condition ÉTAT   — espace non activé → 422 nommé.


@pytest_asyncio.fixture
async def dormant_case(
    manager_agent: Agent, make_client_case: MakeClientCase, make_expat_user: MakeExpatUser
) -> ClientCase:
    """Un dossier dont le client n'a JAMAIS activé son espace — l'état que le
    lien doit refuser (`make_client_case` en crée un par défaut, on le nomme
    ici pour que le test dise ce qu'il teste)."""
    expat = await make_expat_user(first_name="Paul", last_name="Durand", activated=False)
    return await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )


async def _step_with_due_date(
    client: AsyncClient,
    agent: Agent,
    headers: AuthHeaders,
    case_id: uuid.UUID,
    due_at: datetime | None = None,
) -> str:
    """Un parcours d'une étape posé sur le dossier, avec (ou sans) son
    échéance FERME. Retourne l'id du CaseStepProgress."""
    h = headers(agent)
    template = (await client.post("/journeys", headers=h, json={"name": "Parcours"})).json()
    added = await client.post(f"/journeys/{template['id']}/steps", headers=h, json={"name": "Visa"})
    assert added.status_code == 201, added.text
    timeline = (
        await client.post(
            f"/cases/{case_id}/journey",
            headers=h,
            json={"journey_template_id": template["id"]},
        )
    ).json()
    progress_id = str(timeline[0]["id"])
    if due_at is not None:
        patched = await client.patch(
            f"/cases/{case_id}/steps/{progress_id}",
            headers=h,
            json={"due_at": due_at.isoformat()},
        )
        assert patched.status_code == 200, patched.text
    return progress_id


# --- {client_first_name} : gratuit ---------------------------------------------------


async def test_the_first_name_is_frozen_and_never_refuses(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """L'accroche (« Bonjour Marie, ») sans coût : le principal est DÉJÀ lu au
    figeage pour {client_name}, et son prénom est NOT NULL. Ce jeton n'a donc
    aucune raison de refus — c'est ce qui le distingue des deux suivants."""
    created = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "Bonjour {client_first_name}, ici l'équipe qui suit {client_name}.",
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "Bonjour Jean, ici l'équipe qui suit Jean Martin."


# --- {step_due_date} : la condition DONNÉE ------------------------------------------


async def test_the_due_date_is_written_in_the_language_of_the_CLIENT(
    client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
) -> None:
    """LA règle de forme : « 5 septembre 2026 » à un client francophone,
    « 5 de septiembre de 2026 » à un hispanophone — la même échéance, le même
    dossier, la même agence. Une date numérique aurait été ambiguë (05/09 se
    lit September 5th ailleurs) ; elle est écrite en toutes lettres."""
    due = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    text = "Votre étape {step_name} est attendue pour le {step_due_date}."

    for lang, expected in (("fr", "5 septembre 2026"), ("es", "5 de septiembre de 2026")):
        expat = await make_expat_user(preferred_lang=lang)
        case = await make_client_case(
            agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
        )
        progress_id = await _step_with_due_date(
            client, manager_agent, agent_headers, case.id, due_at=due
        )
        created = await client.post(
            f"/cases/{case.id}/reminders",
            headers=agent_headers(manager_agent),
            json={
                "channel": "mail",
                "scheduled_at": _FUTURE.isoformat(),
                "recipient_type": "expat",
                "step_progress_id": progress_id,
                "message_body": text,
            },
        )
        assert created.status_code == 201, created.text
        assert (
            created.json()["message_body"] == f"Votre étape Visa est attendue pour le {expected}."
        )


async def test_an_external_recipient_reads_the_date_in_the_AGENCY_language(
    client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_external_contact,
    agent_headers: AuthHeaders,
) -> None:
    """Le prestataire n'est pas le client : il lit dans la langue de l'AGENCE,
    jamais dans celle de l'expatrié. C'est déjà la règle du dispatch pour le
    corps du mail — le jeton ne peut pas en inventer une autre."""
    expat = await make_expat_user(preferred_lang="ru")  # le CLIENT est russophone…
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    contact = await make_external_contact(case=case, email="notaire@example.com")
    progress_id = await _step_with_due_date(
        client,
        manager_agent,
        agent_headers,
        case.id,
        due_at=datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
    )
    created = await client.post(
        f"/cases/{case.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "external",
            "recipient_external_id": str(contact.id),
            "step_progress_id": progress_id,
            "message_body": "Échéance : {step_due_date}.",
        },
    )
    assert created.status_code == 201, created.text
    # …mais le notaire lit le français de l'agence.
    assert created.json()["message_body"] == "Échéance : 5 septembre 2026."


async def test_a_step_without_a_firm_deadline_is_a_named_422(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """`due_at` est NULLABLE : sans échéance posée, le jeton n'a rien à dire.
    Même discipline que {days_left} — on refuse en le nommant, on ne devine
    pas (le compteur estimé est une AUTRE information, pas un repli), et
    surtout on ne scelle pas un trou dans un message qui part une fois."""
    progress_id = await _step_with_due_date(
        client, manager_agent, agent_headers, case_with_client.id
    )
    refused = await client.post(
        f"/cases/{case_with_client.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "step_progress_id": progress_id,
            "message_body": "Avant le {step_due_date}.",
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "reminder.variable_unresolvable"
    assert refused.json()["params"] == {
        "variable": "step_due_date",
        "reason": "due_date_required",
    }


async def test_the_due_date_without_a_step_refuses_like_its_neighbours(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Un rappel libre (aucune étape liée) : le jeton rejoint {step_name} et
    {days_left} sur `step_required` — et c'est l'ORDRE DU CATALOGUE, pas
    l'ordre du code, qui décide lequel des trois le 422 nomme."""
    refused = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "Avant le {step_due_date}.",
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["params"] == {"variable": "step_due_date", "reason": "step_required"}

    both = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "Le {step_due_date} pour {step_name}.",
    )
    # {step_name} est AVANT au catalogue, donc c'est lui qui est nommé — quel
    # que soit l'ordre dans lequel le texte les emploie.
    assert both.json()["params"]["variable"] == "step_name"


# --- {client_space_link} : la condition ÉTAT ----------------------------------------


async def test_the_space_link_is_frozen_when_the_space_is_ACTIVE(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    """Espace activé → le lien part, et il porte le slug de l'agence : le
    client atterrit sur SA page de connexion blanche-marque, pas sur un
    /space nu (même construction que tous les mails clients)."""
    agency = await db_session.get(Agency, manager_agent.agency_id)
    assert agency is not None
    created = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "Suivez votre dossier ici : {client_space_link}",
    )
    assert created.status_code == 201, created.text
    expected = f"{get_settings().frontend_url}/space?agency={agency.slug}"
    assert created.json()["message_body"] == f"Suivez votre dossier ici : {expected}"


async def test_a_dormant_space_refuses_the_link_rather_than_sending_to_a_WALL(
    client: AsyncClient,
    manager_agent: Agent,
    dormant_case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """LE POINT DU JETON. Le client n'a jamais activé son espace : le lien
    l'enverrait sur une page qu'il ne peut pas passer. Le figeage refuse, en
    le nommant — cohérent avec le lot activation, dont l'invitation reste le
    chemin de ce client-là."""
    refused = await _post(
        client,
        manager_agent,
        agent_headers,
        dormant_case.id,
        "Votre espace : {client_space_link}",
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "reminder.variable_unresolvable"
    assert refused.json()["params"] == {
        "variable": "client_space_link",
        "reason": "client_space_inactive",
    }


async def test_a_recipient_who_has_no_space_at_all_refuses_the_link(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    make_external_contact,
    agent_headers: AuthHeaders,
) -> None:
    """Le corollaire : l'espace du client est ACTIF, mais le destinataire est
    un notaire. Il n'a pas d'espace du tout — le lien serait un mur pour lui
    aussi. La condition juge le DESTINATAIRE, jamais le dossier seul."""
    contact = await make_external_contact(case=case_with_client, email="notaire@example.com")
    refused = await client.post(
        f"/cases/{case_with_client.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "external",
            "recipient_external_id": str(contact.id),
            "message_body": "Votre espace : {client_space_link}",
        },
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["params"] == {
        "variable": "client_space_link",
        "reason": "recipient_not_client",
    }


# --- l'aperçu dit AVANT ce que le figeage fera ---------------------------------------


async def test_the_preview_announces_both_conditional_refusals(
    client: AsyncClient,
    manager_agent: Agent,
    dormant_case: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """L'invariant du lot précédent, tenu par les nouveaux jetons : un refus
    ne surprend jamais au moment d'enregistrer. L'aperçu nomme les deux
    conditions AVANT, avec la même résolution que le figeage — et rend quand
    même une phrase entière (spécimens en repli)."""
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={
            "content": "Bonjour {client_first_name}, avant le {step_due_date} : "
            "{client_space_link}",
            "case_id": str(dormant_case.id),
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert {t["name"]: t["reason"] for t in body["unresolvable_tokens"]} == {
        "step_due_date": "step_required",
        "client_space_link": "client_space_inactive",
    }
    # Le gratuit, lui, est rendu pour de vrai au milieu des deux refus.
    assert "Bonjour Paul" in body["rendered"]
    assert "5 septembre 2026" in body["rendered"]  # spécimen en repli, pas un trou


async def test_the_preview_judges_the_recipient_it_is_given(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """L'aperçu ne peut pas flatter le figeage : sur un dossier dont l'espace
    est ACTIF, le même texte passe pour le client et se refuse pour le
    prestataire — parce que le destinataire fait partie de la question."""
    payload = {
        "content": "Votre espace : {client_space_link}",
        "case_id": str(case_with_client.id),
    }
    headers = agent_headers(manager_agent)

    for_client = await client.post("/reminders/preview", headers=headers, json=payload)
    assert for_client.status_code == 200, for_client.text
    assert for_client.json()["unresolvable_tokens"] == []  # défaut : le client

    for_provider = await client.post(
        "/reminders/preview", headers=headers, json={**payload, "recipient_type": "external"}
    )
    assert for_provider.status_code == 200, for_provider.text
    assert [t["reason"] for t in for_provider.json()["unresolvable_tokens"]] == [
        "recipient_not_client"
    ]


# ── QUI NOMME QUI (14/08, second passage) ─────────────────────────────────────
#
# LE BUG RÉPARÉ ICI : une relance routée vers un MEMBRE du dossier commençait
# par le nom du TITULAIRE. « Bonjour Jean » envoyé à Marie — l'agence se
# ridiculise devant son client, et rien dans le produit ne l'en avertissait.
#
# La cause n'était pas une erreur de résolution mais un mot ambigu :
# {client_name} ne disait pas DE QUEL humain il s'agissait. Deux jetons le
# remplacent, chacun explicite :
#
#   {recipient_name} / {recipient_first_name}  → QUI LIT ce message
#   {case_client_name} / {case_client_first_name} → LE TITULAIRE du dossier
#
# Les deux existent parce que les DEUX usages sont légitimes : on salue son
# lecteur, et on nomme le dossier dont on parle (« le dossier de Jean Dupont
# attend votre retour », écrit à un notaire, est le cas NORMAL).


@pytest_asyncio.fixture
async def case_read_by_a_member(
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_case_person,
    client: AsyncClient,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> tuple[ClientCase, str]:
    """Un dossier dont l'étape en cours ne réclame plus rien qu'à MARIE, un
    membre avec son propre accès — le routage du 18/07 envoie donc la relance
    à elle, pas au titulaire Jean. Retourne (dossier, id du progrès d'étape).

    C'est le montage exact où le bug se voyait."""
    headers = agent_headers(manager_agent)
    holder = await make_expat_user(first_name="Jean", last_name="Dupont")
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=holder.id
    )
    member_account = await make_expat_user(
        first_name="Marie", last_name="Martin", preferred_lang="es"
    )
    member = await make_case_person(
        case=case, full_name="Marie Martin", expat_user_id=member_account.id
    )

    template = (await client.post("/journeys", headers=headers, json={"name": "P"})).json()
    step = (
        await client.post(
            f"/journeys/{template['id']}/steps",
            headers=headers,
            json={"name": "Dépôt du passeport", "estimated_days": 15},
        )
    ).json()
    await client.post(
        f"/journeys/{template['id']}/steps/{step['id']}/requirements",
        headers=headers,
        json={"kind": "document", "reference": "passeport", "scope": "principal"},
    )
    timeline = (
        await client.post(
            f"/cases/{case.id}/journey",
            headers=headers,
            json={"journey_template_id": template["id"]},
        )
    ).json()
    progress_id = str(timeline[0]["id"])
    # Les exigences concrètes se matérialisent au DÉMARRAGE de l'étape.
    started = await client.patch(
        f"/cases/{case.id}/steps/{progress_id}", headers=headers, json={"status": "in_progress"}
    )
    assert started.status_code == 200, started.text
    # On repointe l'exigence sur Marie : l'étape ne réclame plus qu'à elle.
    await db_session.execute(
        update(CaseStepRequirement)
        .where(CaseStepRequirement.case_step_progress_id == uuid.UUID(progress_id))
        .values(person_id=member.id)
    )
    await db_session.commit()
    return case, progress_id


async def _post_to_member(
    client: AsyncClient, agent: Agent, headers: AuthHeaders, case_id, progress_id: str, body: str
):
    return await client.post(
        f"/cases/{case_id}/reminders",
        headers=headers(agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "step_progress_id": progress_id,
            "message_body": body,
        },
    )


async def test_the_recipient_token_names_THE_PERSON_WHO_READS(
    client: AsyncClient,
    manager_agent: Agent,
    case_read_by_a_member: tuple[ClientCase, str],
    agent_headers: AuthHeaders,
) -> None:
    """LE TEST DU BUG. La relance part à Marie : elle doit lire son nom, pas
    celui de Jean. Avant ce lot, ce message commençait par « Bonjour Jean »."""
    case, progress_id = case_read_by_a_member
    created = await _post_to_member(
        client,
        manager_agent,
        agent_headers,
        case.id,
        progress_id,
        "Bonjour {recipient_first_name}, il nous manque une pièce. — {recipient_name}",
    )
    assert created.status_code == 201, created.text
    # Le prénom vient du COMPTE, le nom affiché de la personne du dossier :
    # deux sources, la même que l'écran d'approbation pour le second.
    assert (
        created.json()["message_body"] == "Bonjour Marie, il nous manque une pièce. — Marie Martin"
    )
    # Et l'écran d'approbation annonce la même personne — jamais un écart
    # entre ce que l'agence lit et ce que le client recevra.
    assert created.json()["resolved_recipient"] == "Marie Martin"


async def test_the_holder_stays_nameable_in_a_message_read_by_someone_else(
    client: AsyncClient,
    manager_agent: Agent,
    case_read_by_a_member: tuple[ClientCase, str],
    agent_headers: AuthHeaders,
) -> None:
    """L'autre moitié, et la raison d'être des DEUX jetons : nommer le dossier
    dont on parle reste légitime quand on écrit à quelqu'un d'autre."""
    case, progress_id = case_read_by_a_member
    created = await _post_to_member(
        client,
        manager_agent,
        agent_headers,
        case.id,
        progress_id,
        "Bonjour {recipient_first_name}, le dossier de {case_client_name} avance.",
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "Bonjour Marie, le dossier de Jean Dupont avance."


async def test_the_recipient_token_names_the_external_provider(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    make_external_contact,
    agent_headers: AuthHeaders,
) -> None:
    """Le destinataire n'est pas toujours un client. Écrit à un notaire, le
    message le salue LUI et nomme le dossier — les deux jetons dans la même
    phrase, chacun sur son humain. C'est l'usage qui interdisait de faire de
    {client_name} un simple alias du destinataire."""
    contact = await make_external_contact(
        case=case_with_client, name="Maître Ada Roux", email="ada@etude.fr"
    )
    created = await client.post(
        f"/cases/{case_with_client.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "external",
            "recipient_external_id": str(contact.id),
            "message_body": (
                "{recipient_name}, le dossier de {case_client_name} attend votre retour."
            ),
        },
    )
    assert created.status_code == 201, created.text
    assert (
        created.json()["message_body"]
        == "Maître Ada Roux, le dossier de Jean Martin attend votre retour."
    )


async def test_a_provider_has_no_first_name_so_the_token_uses_the_name_he_has(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    make_external_contact,
    agent_headers: AuthHeaders,
) -> None:
    """Un contact externe n'a QU'UN champ nom. {recipient_first_name} retombe
    dessus plutôt que de découper « Maître Ada Roux » au petit bonheur : la
    phrase reste correcte, et rien n'est deviné."""
    contact = await make_external_contact(
        case=case_with_client, name="Maître Ada Roux", email="ada@etude.fr"
    )
    created = await client.post(
        f"/cases/{case_with_client.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "external",
            "recipient_external_id": str(contact.id),
            "message_body": "Bonjour {recipient_first_name}.",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "Bonjour Maître Ada Roux."


async def test_without_a_targeted_member_the_recipient_IS_the_holder(
    client: AsyncClient,
    manager_agent: Agent,
    case_with_client: ClientCase,
    agent_headers: AuthHeaders,
) -> None:
    """Le cas de loin le plus courant, et la non-régression : sans étape qui
    désigne quelqu'un, le destinataire EST le titulaire — les deux jetons
    disent alors la même chose, et c'est normal."""
    created = await _post(
        client,
        manager_agent,
        agent_headers,
        case_with_client.id,
        "{recipient_name} / {case_client_name} / {recipient_first_name}",
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "Jean Martin / Jean Martin / Jean"


# --- la dépréciation : on ne casse aucun texte déjà écrit ---------------------------


async def test_the_deprecated_tokens_still_resolve_and_still_name_the_HOLDER(
    client: AsyncClient,
    manager_agent: Agent,
    case_read_by_a_member: tuple[ClientCase, str],
    agent_headers: AuthHeaders,
) -> None:
    """LA PROMESSE DE LA DÉPRÉCIATION. {client_name} garde EXACTEMENT le sens
    qu'il avait — le titulaire — même sur le montage où le bug se voyait. En
    faire un alias du destinataire aurait réécrit en silence le sens des textes
    adressés aux prestataires, où nommer le titulaire est tout l'objet.

    Conséquence assumée : un texte déjà enregistré avec {client_name} n'est pas
    réparé tout seul. L'agence bascule sur {recipient_first_name}."""
    case, progress_id = case_read_by_a_member
    created = await _post_to_member(
        client,
        manager_agent,
        agent_headers,
        case.id,
        progress_id,
        "{client_name} — {client_first_name}",
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "Jean Dupont — Jean"  # le TITULAIRE, inchangé


async def test_a_deprecated_token_is_never_reported_as_unknown(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """Déprécié ≠ inconnu. Le jeton disparaît de la liste d'insertion, mais
    l'agence qui l'a déjà dans son texte ne doit pas voir surgir un
    avertissement « personne ne résout ceci » : il est résolu."""
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={"content": "Bonjour {client_name}, et {inconnu}."},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["unknown_tokens"] == ["{inconnu}"]
    assert "Jean Dupont" in preview.json()["rendered"]


# ── {step_name} DANS LA LANGUE DU DESTINATAIRE ────────────────────────────────
#
# Les noms d'étapes SONT traduits en base (journey_template_step.name_i18n,
# bloc i18n). Le figeage connaissait la langue du destinataire depuis le lot
# précédent mais rendait quand même le scalaire — le seul jeton à l'ignorer.


async def _step_named(
    client: AsyncClient,
    agent: Agent,
    headers: AuthHeaders,
    case_id: uuid.UUID,
    db_session: AsyncSession,
    name_i18n: dict[str, str],
) -> str:
    """Une étape dont le nom est traduit, posée sur le dossier."""
    h = headers(agent)
    template = (await client.post("/journeys", headers=h, json={"name": "P"})).json()
    step = (
        await client.post(
            f"/journeys/{template['id']}/steps", headers=h, json={"name": "Dépôt du dossier"}
        )
    ).json()
    await db_session.execute(
        update(JourneyTemplateStep)
        .where(JourneyTemplateStep.id == uuid.UUID(step["id"]))
        .values(name_i18n=name_i18n)
    )
    await db_session.commit()
    timeline = (
        await client.post(
            f"/cases/{case_id}/journey", headers=h, json={"journey_template_id": template["id"]}
        )
    ).json()
    return str(timeline[0]["id"])


async def test_the_step_name_is_frozen_in_the_language_of_the_recipient(
    client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    """Le nom d'étape suit le lecteur, comme la date d'échéance à côté de lui.
    Une agence francophone, un client hispanophone : le client lit l'espagnol
    que l'agence a saisi, pas le français."""
    expat = await make_expat_user(first_name="Ana", last_name="Ruiz", preferred_lang="es")
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    progress_id = await _step_named(
        client,
        manager_agent,
        agent_headers,
        case.id,
        db_session,
        {"fr": "Dépôt du dossier", "es": "Presentación del expediente"},
    )
    created = await client.post(
        f"/cases/{case.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "step_progress_id": progress_id,
            "message_body": "Etapa: {step_name}.",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "Etapa: Presentación del expediente."


async def test_the_step_name_falls_back_to_the_agency_language_then_the_scalar(
    client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    """La chaîne de replis, gravée : langue du lecteur → langue de l'agence →
    le libellé saisi. Jamais un vide, jamais une clé brute — un nom d'étape
    manquant dans une relance serait pire qu'un nom dans la mauvaise langue."""
    # Le client lit le russe ; l'étape n'est traduite qu'en fr (agence) et es.
    expat = await make_expat_user(first_name="Ivan", last_name="Petrov", preferred_lang="ru")
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    progress_id = await _step_named(
        client,
        manager_agent,
        agent_headers,
        case.id,
        db_session,
        {"fr": "Dépôt du dossier", "es": "Presentación del expediente"},
    )
    fell_back = await client.post(
        f"/cases/{case.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "step_progress_id": progress_id,
            "message_body": "{step_name}",
        },
    )
    assert fell_back.status_code == 201, fell_back.text
    assert fell_back.json()["message_body"] == "Dépôt du dossier"  # la langue de l'agence

    # Blob VIDE : on retombe sur le scalaire, qui est NOT NULL. (Un dossier ne
    # porte qu'UN parcours — d'où un second dossier plutôt qu'un second
    # assignement, qui serait refusé.)
    other = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    bare_id = await _step_named(client, manager_agent, agent_headers, other.id, db_session, {})
    bare = await client.post(
        f"/cases/{other.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "expat",
            "step_progress_id": bare_id,
            "message_body": "{step_name}",
        },
    )
    assert bare.status_code == 201, bare.text
    assert bare.json()["message_body"] == "Dépôt du dossier"


async def test_a_provider_reads_the_step_name_in_the_AGENCY_language(
    client: AsyncClient,
    manager_agent: Agent,
    make_client_case: MakeClientCase,
    make_expat_user: MakeExpatUser,
    make_external_contact,
    agent_headers: AuthHeaders,
    db_session: AsyncSession,
) -> None:
    """Le pendant de la date d'échéance : le prestataire lit la langue de
    l'agence, jamais celle du client. Les deux jetons de langue tirent du même
    destinataire, ils ne peuvent pas diverger."""
    expat = await make_expat_user(first_name="Ana", last_name="Ruiz", preferred_lang="es")
    case = await make_client_case(
        agency_id=manager_agent.agency_id, principal_expat_user_id=expat.id
    )
    contact = await make_external_contact(case=case, name="Maître Ada Roux", email="ada@etude.fr")
    progress_id = await _step_named(
        client,
        manager_agent,
        agent_headers,
        case.id,
        db_session,
        {"fr": "Dépôt du dossier", "es": "Presentación del expediente"},
    )
    created = await client.post(
        f"/cases/{case.id}/reminders",
        headers=agent_headers(manager_agent),
        json={
            "channel": "mail",
            "scheduled_at": _FUTURE.isoformat(),
            "recipient_type": "external",
            "recipient_external_id": str(contact.id),
            "step_progress_id": progress_id,
            "message_body": "Étape : {step_name}.",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["message_body"] == "Étape : Dépôt du dossier."


# --- le troisième signal d'édition : le jeton déprécié se DÉNONCE ------------------
#
# Retiré de la liste ET résolu, un jeton déprécié devient INVISIBLE : absent du
# catalogue servi, absent des inconnus (il est résolu), absent des
# non-résolubles (il ne refuse rien). Une agence garderait « Bonjour
# {client_name} » sans jamais apprendre qu'il nomme le titulaire. D'où ce
# signal — le seul des trois qui ne dénonce ni une erreur ni un refus, mais un
# MALENTENDU.


async def test_the_preview_denounces_a_deprecated_token_and_offers_BOTH_ways_out(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """Les deux sorties ne sont PAS interchangeables, et c'est pourquoi le
    contrat en sert deux : `resolves_to` garde la valeur (le titulaire),
    `suggested` la CHANGE (le lecteur). Un remplacement en un clic qui
    choisirait tout seul trancherait à la place de l'agence."""
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={"content": "Bonjour {client_name}, et encore {client_name}. {client_first_name}"},
    )
    assert preview.status_code == 200, preview.text
    flagged = preview.json()["deprecated_tokens"]
    # Ordre de lecture, sans doublon — même idiome que les jetons inconnus.
    assert [t["token"] for t in flagged] == ["{client_name}", "{client_first_name}"]
    assert flagged[0] == {
        "token": "{client_name}",
        "name": "client_name",
        "resolves_to": "{case_client_name}",  # même valeur : le titulaire
        "suggested": "{recipient_name}",  # ce qu'on voulait dire : le lecteur
    }
    # Et il n'est ni inconnu, ni non-résoluble : il rend une valeur.
    assert preview.json()["unknown_tokens"] == []
    assert preview.json()["unresolvable_tokens"] == []
    assert "Jean Dupont" in preview.json()["rendered"]


async def test_a_text_free_of_deprecated_tokens_says_so(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """La clé est TOUJOURS présente, liste vide comprise — une clé qui
    n'apparaît que quand elle se déclenche se lit comme une anomalie (même
    discipline que `skipped_step_done` du lot précédent)."""
    preview = await client.post(
        "/reminders/preview",
        headers=agent_headers(manager_agent),
        json={"content": "Bonjour {recipient_first_name}."},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["deprecated_tokens"] == []


async def test_a_message_template_carries_the_same_signal_without_being_opened(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """Servi sur le MODÈLE lui-même, à la création comme à la liste : une
    agence doit repérer les modèles à revoir sans les ouvrir un par un. Dérivé
    du corps, aucune requête de plus."""
    headers = agent_headers(manager_agent)
    created = await client.post(
        "/message-templates",
        headers=headers,
        json={"name": "À revoir", "body": "Bonjour {client_name}, votre dossier avance."},
    )
    assert created.status_code == 201, created.text
    assert [t["name"] for t in created.json()["deprecated_tokens"]] == ["client_name"]
    assert created.json()["deprecated_tokens"][0]["suggested"] == "{recipient_name}"

    clean = await client.post(
        "/message-templates",
        headers=headers,
        json={"name": "À jour", "body": "Bonjour {recipient_first_name}."},
    )
    assert clean.status_code == 201, clean.text
    assert clean.json()["deprecated_tokens"] == []

    # La liste porte le signal, donc l'écran de gestion peut badger ses lignes.
    listed = {
        t["name"]: t["deprecated_tokens"]
        for t in (await client.get("/message-templates", headers=headers)).json()
    }
    assert [t["token"] for t in listed["À revoir"]] == ["{client_name}"]
    assert listed["À jour"] == []


async def test_the_signal_follows_an_edit_in_both_directions(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """Dérivé, jamais stocké : il apparaît quand l'agence introduit le jeton et
    DISPARAÎT quand elle le corrige — sinon l'avertissement survivrait à sa
    propre correction, ce qui apprendrait à l'ignorer."""
    headers = agent_headers(manager_agent)
    template_id = (
        await client.post(
            "/message-templates", headers=headers, json={"name": "T", "body": "Bonjour."}
        )
    ).json()["id"]

    regressed = await client.patch(
        f"/message-templates/{template_id}",
        headers=headers,
        json={"body": "Bonjour {client_name}."},
    )
    assert regressed.status_code == 200, regressed.text
    assert [t["name"] for t in regressed.json()["deprecated_tokens"]] == ["client_name"]

    fixed = await client.patch(
        f"/message-templates/{template_id}",
        headers=headers,
        json={"body": "Bonjour {recipient_name}."},
    )
    assert fixed.status_code == 200, fixed.text
    assert fixed.json()["deprecated_tokens"] == []


# --- les familles : ce que l'exemple ne peut pas dire ------------------------------


async def test_every_served_token_carries_its_FAMILY(
    client: AsyncClient, manager_agent: Agent, agent_headers: AuthHeaders
) -> None:
    """Le titre du groupe porte la distinction que l'exemple ne peut pas
    porter : sans membre ciblé, {recipient_name} et {case_client_name} rendent
    LA MÊME valeur, et seul « la personne qui reçoit » / « le titulaire du
    dossier » les sépare à l'écriture.

    Le contrat sert une CLÉ i18n, jamais un titre — le front traduit."""
    served = (await client.get("/agencies/me", headers=agent_headers(manager_agent))).json()
    tokens = {t["name"]: t for t in served["reminder_tokens"]}

    assert tokens["recipient_name"]["group"] == "recipient"
    assert tokens["recipient_first_name"]["group"] == "recipient"
    assert tokens["case_client_name"]["group"] == "case_client"
    assert tokens["case_client_first_name"]["group"] == "case_client"
    assert tokens["step_name"]["group"] == "step"
    assert tokens["days_left"]["group"] == "step"
    assert tokens["step_due_date"]["group"] == "step"
    assert tokens["agency_name"]["group"] == "agency"
    assert tokens["contact_email"]["group"] == "agency"
    assert tokens["contact_phone"]["group"] == "agency"
    assert tokens["client_space_link"]["group"] == "link"

    # Aucun jeton sans famille, et les familles arrivent GROUPÉES dans l'ordre
    # du catalogue — le front n'a rien à retrier pour construire ses sections.
    order = [t["group"] for t in served["reminder_tokens"]]
    assert all(order), "un jeton servi sans famille"
    assert list(dict.fromkeys(order)) == ["recipient", "case_client", "step", "agency", "link"]
    assert order == sorted(order, key=lambda g: list(dict.fromkeys(order)).index(g))


def test_the_five_families_are_derived_from_the_catalogue_not_hand_written() -> None:
    """GROUPS n'est pas une liste tenue à la main à côté : une famille nouvelle
    apparaît par la seule addition d'un jeton qui la porte. C'est la même règle
    que VARIABLE_PATTERN et STEP_TOKENS — le catalogue est LA source."""
    assert GROUPS == ("recipient", "case_client", "step", "agency", "link")
    assert {spec.group for spec in CATALOGUE} == set(GROUPS)
