"""Lot 13/08 — les conditions générales AU NOM DE L'AGENCE, avec modèle.

Une agence sans conditions propres faisait accepter à ses clients les
conditions de NIDRIA (juridiquement intenable). Désormais : un modèle au
nom de l'agence, pré-rempli, publié immédiatement (le repli Nidria meurt),
avec marqueurs visibles pour ce qui manque, et un geste de validation."""

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.consent import ConsentDocument
from shared.models.rbac import Role
from src.agencies.agencies_manager import AgenciesManager
from src.consents.agency_template import RESPONSIBILITY_DISCLAIMER, generate_client_terms
from src.consents.consents_seed import ensure_agency_default_terms
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]):
    return await make_agent(role=system_roles["admin"])


async def _agency(db: AsyncSession, agency_id) -> Agency:
    db.expire_all()
    agency = await db.get(Agency, agency_id)
    assert agency is not None
    return agency


async def _own_active(db: AsyncSession, agency_id, doc_type: str) -> ConsentDocument | None:
    return (
        await db.execute(
            select(ConsentDocument).where(
                ConsentDocument.agency_id == agency_id,
                ConsentDocument.type == doc_type,
                ConsentDocument.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


def test_generation_omits_missing_segments_grammatically() -> None:
    """Omission by segment (13/08): a missing field DROPS its segment — no
    bracket in the published text, and the sentence stays grammatical at
    every fill level (no orphan comma, no « sous le numéro . », no empty
    slot). The brand name stays the {agency_name} token."""
    from src.consents.agency_template import generate_client_privacy

    # Progressive fill: 0, 1, 2, … up to all fields.
    steps = [
        {},
        {"legal_name": "ACME SARL"},
        {"legal_name": "ACME SARL", "registration_number": "RCS 123"},
        {
            "legal_name": "ACME SARL",
            "legal_form": "SAS",
            "registration_number": "RCS 123",
            "address": "1 rue de la Paix",
            "city": "Paris",
            "postal_code": "75001",
            "country": "FR",
            "contact_email": "contact@acme.fr",
        },
    ]
    for fields in steps:
        agency = Agency(name="ACME", slug="acme", **fields)
        for text in (generate_client_terms(agency), generate_client_privacy(agency)):
            assert "[" not in text and "]" not in text  # no brackets, ever
            assert ", ," not in text  # no orphan comma
            assert ",," not in text  # no glued orphan comma either
            assert "numéro ." not in text and "situé ." not in text  # no dangling segment
            assert "est ." not in text and "par ." not in text  # identity never empty-then-period
            assert "{agency_name}" in text  # brand token kept for read-time resolve
    # Empty profile → the identity is just the brand name (no legal segments).
    empty_terms = generate_client_terms(Agency(name="ACME", slug="acme"))
    assert "édité par {agency_name}." in empty_terms
    # A filled field appears as its segment.
    filled = generate_client_terms(Agency(name="ACME", slug="acme", registration_number="RCS 9"))
    assert "immatriculée sous le numéro RCS 9" in filled


async def test_ensure_publishes_both_docs_and_kills_the_fallback(
    db_session: AsyncSession, admin
) -> None:
    """The agency gets its OWN client_terms + client_privacy (agency_id set)
    — its clients no longer resolve to the Nidria canonical."""
    agency = await _agency(db_session, admin.agency_id)
    assert await ensure_agency_default_terms(db_session, agency)
    await db_session.commit()

    for doc_type in ("client_terms", "client_privacy"):
        doc = await _own_active(db_session, agency.id, doc_type)
        assert doc is not None and doc.agency_id == agency.id  # its OWN, not Nidria's
        assert "[" not in doc.content_md  # no bracket in the published text

    manager = AgenciesManager(db_session)
    assert (await manager.own_client_terms(agency)) is not None
    assert (await manager.own_client_privacy(agency)) is not None

    # Idempotent: a second call publishes nothing new.
    assert (await ensure_agency_default_terms(db_session, agency)) is False


async def test_legacy_bracketed_doc_is_regenerated_bracket_free(
    db_session: AsyncSession, admin
) -> None:
    """The one-shot bracket removal: an agency seeded with the OLD bracketed
    template gets it regenerated (no bracket) — but a hand-written text is
    left untouched."""
    from src.consents.consents_seed import publish_if_changed

    agency = await _agency(db_session, admin.agency_id)
    # Simulate the OLD bracketed auto-generated doc.
    await publish_if_changed(
        db_session,
        "client_terms",
        "# Conditions\n\nCet espace est édité par [Votre dénomination légale].\n",
        agency_id=agency.id,
    )
    await db_session.commit()

    assert await ensure_agency_default_terms(db_session, agency)  # regenerated
    await db_session.commit()
    doc = await _own_active(db_session, agency.id, "client_terms")
    assert doc is not None and "[" not in doc.content_md  # bracket gone

    # A hand-written doc (no legacy marker) is NOT clobbered.
    await publish_if_changed(db_session, "client_terms", "Mon texte à moi", agency_id=agency.id)
    await db_session.commit()
    assert (await ensure_agency_default_terms(db_session, agency)) is False
    doc = await _own_active(db_session, agency.id, "client_terms")
    assert doc is not None and doc.content_md == "Mon texte à moi"


async def test_patch_publishes_privacy_and_validate_sets_the_flag(
    client: AsyncClient, db_session: AsyncSession, admin, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    patched = await client.patch(
        "/agencies/me",
        headers=headers,
        json={"client_terms_md": "Mes conditions à moi", "client_privacy_md": "Ma note RGPD"},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["client_terms_md"] == "Mes conditions à moi"
    assert body["client_privacy_md"] == "Ma note RGPD"
    assert body["client_terms_reviewed_at"] is None  # published ≠ validated
    assert body["client_terms_disclaimer"] == RESPONSIBILITY_DISCLAIMER  # edition-only

    # The disclaimer is NEVER inside the client-facing text.
    assert RESPONSIBILITY_DISCLAIMER not in body["client_terms_md"]

    # Onboarding shows the review step, not done yet.
    onb = (await client.get("/agencies/me/onboarding", headers=headers)).json()
    step = next(s for s in onb["steps"] if s["key"] == "review_client_terms")
    assert step["done"] is False

    # « J'ai vérifié » → the flag is set, the onboarding step is done.
    validated = await client.post("/agencies/me/client-terms/validate", headers=headers)
    assert validated.status_code == 200, validated.text
    assert validated.json()["client_terms_reviewed_at"] is not None
    onb = (await client.get("/agencies/me/onboarding", headers=headers)).json()
    step = next(s for s in onb["steps"] if s["key"] == "review_client_terms")
    assert step["done"] is True


async def test_blank_regenerates_the_template_never_nidria(
    client: AsyncClient, db_session: AsyncSession, admin, agent_headers: AuthHeaders
) -> None:
    """Blanking the field REGENERATES the agency template — it never falls
    back to the Nidria text (that fallback is dead), and carries no bracket."""
    headers = agent_headers(admin)
    await client.patch("/agencies/me", headers=headers, json={"client_terms_md": "Perso"})
    blanked = await client.patch("/agencies/me", headers=headers, json={"client_terms_md": ""})
    assert blanked.status_code == 200, blanked.text
    terms = blanked.json()["client_terms_md"]
    assert terms is not None  # NOT None (would be the Nidria fallback signal)
    assert "édité par" in terms  # the agency template (Nidria's canonical has no such line)
    assert "[" not in terms  # no bracket in the published text


async def test_missing_legal_fields_are_served_and_shrink_when_filled(
    client: AsyncClient, admin, agent_headers: AuthHeaders
) -> None:
    """The missing fields are SERVED as a list (the brackets left the text,
    so the front no longer counts them); filling one drops it."""
    headers = agent_headers(admin)
    before = (await client.get("/agencies/me", headers=headers)).json()["missing_legal_fields"]
    assert set(before) == {
        "legal_name",
        "legal_form",
        "registration_number",
        "address",
        "city",
        "postal_code",
        "country",
        "contact_email",
        # Le téléphone rejoint l'identité légale (lot acquisition 13/08) :
        # même rang que contact_email, donc même signalement quand il manque.
        "contact_phone",
    }
    after = (
        await client.patch("/agencies/me", headers=headers, json={"registration_number": "RCS 1"})
    ).json()["missing_legal_fields"]
    assert "registration_number" not in after and "legal_name" in after


async def test_legal_field_regenerates_while_untouched_but_not_after_edit(
    client: AsyncClient, db_session: AsyncSession, admin, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    # Seed the agency's default terms first.
    agency = await _agency(db_session, admin.agency_id)
    await ensure_agency_default_terms(db_session, agency)
    await db_session.commit()

    # Filling a legal field regenerates the untouched template → its segment
    # now appears in the text (and never a bracket).
    await client.patch("/agencies/me", headers=headers, json={"legal_name": "ACME SA"})
    terms = (await client.get("/agencies/me", headers=headers)).json()["client_terms_md"]
    assert "dénommée ACME SA" in terms
    assert "[" not in terms

    # Now the agency EDITS the text → a later legal change must NOT clobber it.
    await client.patch("/agencies/me", headers=headers, json={"client_terms_md": "Texte maison"})
    await client.patch("/agencies/me", headers=headers, json={"legal_form": "SAS"})
    terms = (await client.get("/agencies/me", headers=headers)).json()["client_terms_md"]
    assert terms == "Texte maison"  # the edit is preserved, not regenerated


# ── Le pays (14/08) : un NOM au rendu, une VRAIE liste à l'écriture ──────────
#
# Constat : `_f(agency.country)` collait l'alpha-2 brut dans la ligne
# d'adresse — un client d'Asunción lisait « Asunción, PY ». Et la validation
# n'était qu'une longueur : « ZZ » passait. Le front ne pouvait rien : c'est
# un texte généré et servi.


def test_the_country_renders_as_a_name_never_as_a_code() -> None:
    """La ligne d'adresse du modèle généré NOMME le pays, dans la langue du
    document (français). Le code stocké ne se lit nulle part."""
    agency = Agency(
        name="ACME",
        slug="acme",
        address="Avenida España 123",
        postal_code="1209",
        city="Asunción",
        country="PY",
    )
    terms = generate_client_terms(agency)
    assert "dont le siège est situé Avenida España 123, 1209, Asunción, Paraguay" in terms
    assert "Asunción, PY" not in terms


def test_a_lowercase_stored_code_renders_all_the_same() -> None:
    """Le rendu est insensible à la casse : une ligne déjà en base avec
    « fr » (le cas que la validation laissait passer) se lit « France » sans
    attendre une correction de donnée."""
    agency = Agency(name="ACME", slug="acme", city="Paris", country="fr")
    assert "situé Paris, France" in generate_client_terms(agency)


def test_an_unnameable_code_is_rendered_as_written() -> None:
    """Un rendu ne RETIRE jamais une information d'un texte légal : un code
    qu'on ne sait pas nommer (ligne héritée) s'affiche tel quel, comme
    avant — il ne disparaît pas de l'adresse."""
    agency = Agency(name="ACME", slug="acme", city="Nulle-part", country="ZZ")
    assert "situé Nulle-part, ZZ" in generate_client_terms(agency)


async def test_the_country_token_resolves_to_the_name(
    make_agent: MakeAgent, system_roles, db_session: AsyncSession
) -> None:
    """Le jeton {country} suit la même règle que la ligne d'adresse — la
    résolution à la lecture sert le NOM, et le catalogue servi au front
    montre ce que le client lira."""
    from src.consents.agency_tokens import resolve, token_values

    admin = await make_agent(role=system_roles["admin"])
    agency = await db_session.get(Agency, admin.agency_id)
    assert agency is not None

    agency.country = "PY"
    assert resolve("Établie au {country}.", agency) == "Établie au Paraguay."
    value = next(v for name, _label, v in token_values(agency) if name == "country")
    assert value == "Paraguay"

    # Vide → blanc, jamais un nom inventé.
    agency.country = None
    assert resolve("Établie au {country}.", agency) == "Établie au ."


async def test_an_unknown_country_code_is_a_named_422(
    client: AsyncClient, admin, agent_headers: AuthHeaders, db_session: AsyncSession
) -> None:
    """« ZZ » n'est pas un pays : refusé au BACK (l'API est appelable par un
    client tiers), avec un code nommé — et rien n'est écrit au passage."""
    headers = agent_headers(admin)
    refused = await client.patch(
        "/agencies/me", headers=headers, json={"country": "ZZ", "city": "Asunción"}
    )
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "agency.country_invalid"
    assert refused.json()["params"] == {"country": "ZZ"}

    # Le PATCH entier est refusé : la ville voisine n'a pas été écrite.
    agency = await _agency(db_session, admin.agency_id)
    assert agency.country is None and agency.city is None


async def test_a_lowercase_code_is_accepted_and_stored_uppercase(
    client: AsyncClient, admin, agent_headers: AuthHeaders
) -> None:
    """La casse est NORMALISÉE, pas refusée : « fr » est la France, et ce que
    l'API rend ensuite est le code canonique."""
    headers = agent_headers(admin)
    patched = await client.patch("/agencies/me", headers=headers, json={"country": "fr"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["country"] == "FR"
    assert (await client.get("/agencies/me", headers=headers)).json()["country"] == "FR"
    # Et le pays sort de la liste des champs manquants (il est bien écrit).
    assert "country" not in patched.json()["missing_legal_fields"]


async def test_the_served_documents_and_the_preview_show_the_country_name(
    client: AsyncClient, db_session: AsyncSession, admin, agent_headers: AuthHeaders
) -> None:
    """La vérification de bout en bout du lot : une agence renseigne son
    siège, et les DEUX surfaces qui montrent un texte — les conditions
    servies et « ce que voit votre client » — nomment le pays."""
    headers = agent_headers(admin)
    agency = await _agency(db_session, admin.agency_id)
    await ensure_agency_default_terms(db_session, agency)
    await db_session.commit()

    patched = await client.patch(
        "/agencies/me",
        headers=headers,
        json={
            "address": "Avenida España 123",
            "postal_code": "1209",
            "city": "Asunción",
            "country": "PY",
        },
    )
    assert patched.status_code == 200, patched.text

    # 1. Les conditions SERVIES (le texte que le client accepte).
    terms = (await client.get("/agencies/me", headers=headers)).json()["client_terms_md"]
    assert "siège est situé Avenida España 123, 1209, Asunción, Paraguay" in terms
    assert "Asunción, PY" not in terms

    # 2. L'aperçu « ce que voit votre client », sur un brouillon à jeton.
    preview = await client.post(
        "/agencies/me/client-terms/preview",
        headers=headers,
        json={"content": "Notre siège est au {country}."},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["rendered"] == "Notre siège est au Paraguay."
    assert preview.json()["unfilled_tokens"] == []
