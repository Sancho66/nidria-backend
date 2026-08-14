"""Lot 13/08 — LES JETONS DYNAMIQUES DÉCOUVRABLES + l'écran client vérifiable.

Un seul jeton existait ({agency_name}) et rien ne le nommait : l'agence qui
rédigeait ses conditions ne pouvait pas savoir qu'un jeton existe, et le
front tenait la liste à la main. Ici : un jeton par champ de profil, le
catalogue SERVI (libellé + valeur actuelle), un jeton inconnu qui ne casse
rien mais qui est SIGNALÉ à l'édition, et l'aperçu rendu par la MÊME
résolution que la face client."""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from shared.models.agent import Agent
from shared.models.rbac import Role
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

LEGAL_PROFILE = {
    "legal_name": "ACME SARL",
    "legal_form": "SARL",
    "registration_number": "RCS Paris 123 456",
    "address": "1 rue de la Paix",
    "postal_code": "75001",
    "city": "Paris",
    "country": "FR",
    "contact_email": "contact@acme.fr",
    # Dixième jeton (lot acquisition 13/08) : le téléphone rejoint l'identité
    # légale, donc le catalogue et « tous les jetons » l'incluent.
    "contact_phone": "+33 1 02 03 04 05",
}

# Un texte qui utilise TOUS les jetons du catalogue.
ALL_TOKENS_TEXT = (
    "# Conditions de {agency_name}\n\n"
    "Éditeur : {legal_name}, {legal_form}, immatriculée {registration_number}, "
    "siège {address} {postal_code} {city} ({country}), contact {contact_email} "
    "ou {contact_phone}.\n"
)


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


async def _client_terms_seen_by_a_client(
    client: AsyncClient,
    agency_id,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    expat_headers: AuthHeaders,
) -> str:
    """Le texte tel qu'un VRAI client le lit (la face client, pas l'édition)."""
    expat = await make_expat_user()
    await make_client_case(agency_id=agency_id, principal_expat_user_id=expat.id)
    pending = await client.get("/consents/expat/pending", headers=expat_headers(expat))
    assert pending.status_code == 200, pending.text
    docs = [d for d in pending.json()[0]["documents"] if d["type"] == "client_terms"]
    assert len(docs) == 1, pending.json()
    return docs[0]["content"]


async def test_every_profile_field_is_a_token_resolved_on_the_client_screen(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    expat_headers: AuthHeaders,
) -> None:
    """Les 9 jetons (le nom + les 8 champs légaux) rendent leur valeur sur la
    face CLIENT — plus aucune accolade dans ce que lit le client."""
    headers = agent_headers(admin)
    patched = await client.patch(
        "/agencies/me",
        headers=headers,
        json={**LEGAL_PROFILE, "client_terms_md": ALL_TOKENS_TEXT},
    )
    assert patched.status_code == 200, patched.text
    # Le texte STOCKÉ garde les jetons : la résolution est une lecture, et le
    # hash de version porte sur le brut.
    assert "{registration_number}" in patched.json()["client_terms_md"]

    content = await _client_terms_seen_by_a_client(
        client, admin.agency_id, make_expat_user, make_client_case, expat_headers
    )
    assert "{" not in content and "}" not in content
    for name, value in LEGAL_PROFILE.items():
        if name == "country":
            # LE SEUL jeton dont la valeur RENDUE diffère de la valeur
            # STOCKÉE (lot « le pays », 14/08) : la colonne tient un code ISO,
            # le client lit un NOM, dans la langue du document. Un texte légal
            # nomme un pays, il n'imprime pas un code.
            assert "(France)" in content and "(FR)" not in content
            continue
        assert value in content
    # Le nom commercial reste résolu comme avant (non-régression).
    agency_name = (await client.get("/agencies/me", headers=headers)).json()["name"]
    assert f"Conditions de {agency_name}" in content


async def test_an_unknown_token_reaches_the_client_verbatim_and_never_errors(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    expat_headers: AuthHeaders,
) -> None:
    """Une coquille ({registration_numer}) ou une invention ({numéro de TVA})
    ne peut ni lever une erreur, ni trouer le texte : elle passe TELLE QUELLE
    (la publication ne peut pas mutiler un texte juridique)."""
    headers = agent_headers(admin)
    text = "Immat {registration_numer}, TVA {numéro de TVA}, agence {agency_name}."
    patched = await client.patch("/agencies/me", headers=headers, json={"client_terms_md": text})
    assert patched.status_code == 200, patched.text

    content = await _client_terms_seen_by_a_client(
        client, admin.agency_id, make_expat_user, make_client_case, expat_headers
    )
    assert "{registration_numer}" in content  # verbatim, jamais un vide
    assert "{numéro de TVA}" in content
    assert "{agency_name}" not in content  # le connu, lui, est bien résolu


async def test_the_unknown_token_is_signalled_at_edition_not_to_the_client(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    expat_headers: AuthHeaders,
) -> None:
    """Le signal vit sur la charge d'ÉDITION ; la charge servie au client ne
    porte aucun avertissement (elle n'a que le texte et son empreinte)."""
    headers = agent_headers(admin)
    await client.patch(
        "/agencies/me",
        headers=headers,
        json={"client_terms_md": "Immat {registration_numer}", "client_privacy_md": "RGPD {tva}"},
    )
    body = (await client.get("/agencies/me", headers=headers)).json()
    # Les DEUX documents sont inspectés, pas seulement les conditions.
    assert body["client_terms_unknown_tokens"] == ["{registration_numer}", "{tva}"]

    expat = await make_expat_user()
    await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)
    pending = (await client.get("/consents/expat/pending", headers=expat_headers(expat))).json()
    for doc in pending[0]["documents"]:
        assert set(doc) == {"type", "version", "content", "content_hash"}


async def test_a_known_but_empty_token_reads_blank_and_is_named_at_edition(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    expat_headers: AuthHeaders,
) -> None:
    """Un jeton connu dont le champ est vide rend un BLANC chez le client
    (jamais un crochet, jamais notre plomberie) — et l'édition le nomme, à
    part des champs simplement non renseignés."""
    headers = agent_headers(admin)
    await client.patch(
        "/agencies/me",
        headers=headers,
        json={"client_terms_md": "Immatriculation : {registration_number}."},
    )
    body = (await client.get("/agencies/me", headers=headers)).json()
    assert body["client_terms_unfilled_tokens"] == ["{registration_number}"]
    assert body["client_terms_unknown_tokens"] == []
    # missing_legal_fields nomme TOUS les champs vides ; unfilled_tokens, le
    # sous-ensemble dont le texte dépend vraiment.
    assert "registration_number" in body["missing_legal_fields"]
    assert len(body["missing_legal_fields"]) > len(body["client_terms_unfilled_tokens"])

    content = await _client_terms_seen_by_a_client(
        client, admin.agency_id, make_expat_user, make_client_case, expat_headers
    )
    assert "Immatriculation : ." in content  # un blanc, pas « {registration_number} »

    # Le champ rempli → le jeton n'est plus signalé, et le client lit la valeur.
    filled = await client.patch(
        "/agencies/me", headers=headers, json={"registration_number": "RCS 9"}
    )
    assert filled.json()["client_terms_unfilled_tokens"] == []


async def test_the_catalogue_is_served_with_labels_and_current_values(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """Le front ne devine aucune liste : le catalogue, son libellé humain et
    la valeur ACTUELLE de chaque jeton pour CETTE agence (None = non
    renseigné)."""
    headers = agent_headers(admin)
    body = (await client.get("/agencies/me", headers=headers)).json()
    tokens = {entry["name"]: entry for entry in body["client_terms_tokens"]}
    assert set(tokens) == {"agency_name", *LEGAL_PROFILE}

    name = tokens["registration_number"]
    assert name["token"] == "{registration_number}"  # insérable tel quel
    assert name["label"] == "votre numéro d'immatriculation"  # libellé humain servi
    assert name["value"] is None  # « actuellement : non renseigné »
    assert tokens["agency_name"]["value"] == body["name"]  # toujours renseigné

    patched = await client.patch(
        "/agencies/me", headers=headers, json={"registration_number": "RCS 1"}
    )
    served = {e["name"]: e["value"] for e in patched.json()["client_terms_tokens"]}
    assert served["registration_number"] == "RCS 1"  # le PATCH répond la valeur qu'il pose


async def test_the_preview_renders_exactly_what_the_client_will_read(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    make_expat_user: MakeExpatUser,
    make_client_case: MakeClientCase,
    expat_headers: AuthHeaders,
) -> None:
    """« Ce que voit votre client » : l'aperçu du BROUILLON passe par la même
    résolution que la face client — publier ensuite ne réserve aucune
    surprise. Et il nomme les jetons douteux AVANT publication."""
    headers = agent_headers(admin)
    await client.patch("/agencies/me", headers=headers, json=LEGAL_PROFILE)
    draft = ALL_TOKENS_TEXT + "TVA : {tva}. Ville de repli : {city}.\n"

    preview = await client.post(
        "/agencies/me/client-terms/preview", headers=headers, json={"content": draft}
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["unknown_tokens"] == ["{tva}"]
    assert body["unfilled_tokens"] == []
    assert "ACME SARL" in body["rendered"] and "{tva}" in body["rendered"]

    # L'aperçu ET la face client, mot pour mot.
    await client.patch("/agencies/me", headers=headers, json={"client_terms_md": draft})
    content = await _client_terms_seen_by_a_client(
        client, admin.agency_id, make_expat_user, make_client_case, expat_headers
    )
    assert content == body["rendered"]


async def test_the_preview_is_gated_like_the_edition_it_previews(
    client: AsyncClient,
    make_agent: MakeAgent,
    system_roles: dict[str, Role],
    agent_headers: AuthHeaders,
) -> None:
    """Un membre sans agency.manage ne peut pas rendre le brouillon de son
    agence (même gate que PATCH /agencies/me)."""
    member = await make_agent(role=system_roles["member"])
    refused = await client.post(
        "/agencies/me/client-terms/preview",
        headers=agent_headers(member),
        json={"content": "Bonjour {agency_name}"},
    )
    assert refused.status_code == 403, refused.text
