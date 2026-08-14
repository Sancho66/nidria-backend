"""Traduction IA des MODÈLES DE MESSAGE — décalque du rail des parcours.

Couvre, dans l'ordre du chantier : (1) le run async ne remplit QUE les
variantes vides du corps (une langue = un lot = un cran de progression),
le travail humain jamais touché, le MÊME pool de points débité ; (2)
l'estimation avant lancement (même forme que les parcours, barème nourri
des caractères réels) ; (3) quota dépassé → 403 et le fournisseur n'est
JAMAIS appelé ; (4) L'INVARIANT DUR — un {jeton} traduit/renommé/perdu ne
publie RIEN en silence : repair pass, puis done_with_gaps et la variante
absente ; la réparation qui réussit écrit ; (5) obsolescence : une source
éditée rend les variantes IA « stale » (retraduites sur include_stale),
une variante CORRIGÉE À LA MAIN n'est jamais réécrite hors consentement
explicite (retranslate) ; (6) la résolution AU FIGEAGE : un rappel né d'un
modèle part dans la langue du destinataire si la variante existe, sinon la
source — jamais un blocage ; (7) l'écriture humaine des variantes garde le
scalaire en phase (apply_i18n_write) ; (8) les guichets ne se répondent pas
l'un pour l'autre (un job de parcours → 404 ici)."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.ai_translation_job import AiTranslationJob, AiTranslationSource
from shared.models.ai_usage import AgencyAiUsage
from shared.models.client_case import ClientCase
from shared.models.message_template import MessageTemplate
from shared.models.rbac import Role
from src.ai.quota import month_key, points_for_usage
from src.ai.translation_client import _item_error
from src.core.i18n import SUPPORTED_LANGUAGES
from src.reminders.template_translation_manager import BODY_KEY
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent
from tests.plugins.case_plugin import MakeClientCase
from tests.plugins.expat_plugin import MakeExpatUser

pytestmark = pytest.mark.usefixtures("rbac_baseline")

USAGE = {"prompt_tokens": 900, "completion_tokens": 2500}
POINTS_PER_CALL = points_for_usage(USAGE)
TARGETS = sorted(set(SUPPORTED_LANGUAGES) - {"fr"})
N_TARGETS = len(TARGETS)

SOURCE_BODY = "Bonjour {client_name}, il reste {days_left} jours pour « {step_name} »."
_FUTURE = datetime.now(UTC) + timedelta(days=3)


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Remplace l'appel HTTP brut : préfixe '[lang] ' (cyrillique pour ru —
    la validation de script est RÉELLE), GARDE les {jetons} tels quels, et
    journalise chaque appel. `break_token_for` casse le jeton d'une langue
    (le renomme) au premier appel, `heal_on_retry` le répare au strict
    retry — le couple qui joue l'invariant de bout en bout."""
    state: dict[str, Any] = {"calls": [], "break_token_for": set(), "heal_on_retry": False}

    async def _fake(
        items: list[dict[str, str]],
        source_lang: str,
        target_langs: list[str],
        strict_retry: bool = False,
    ):
        state["calls"].append(
            {"items": list(items), "targets": list(target_langs), "strict": strict_retry}
        )

        def value(text: str, lang: str) -> str:
            out = f"[{lang}] {'Перевод ' if lang == 'ru' else ''}{text}"
            if lang in state["break_token_for"] and not (strict_retry and state["heal_on_retry"]):
                out = out.replace("{client_name}", "{nom_du_client}")
            return out

        translations = {
            item["key"]: {lang: value(item["text"], lang) for lang in target_langs}
            for item in items
        }
        return translations, dict(USAGE)

    from src.ai import translation_client as tc

    monkeypatch.setattr(tc, "request_translations", _fake)
    return state


async def _template(client: AsyncClient, headers: dict[str, str], body: str = SOURCE_BODY) -> str:
    created = await client.post(
        "/message-templates", headers=headers, json={"name": "Relance douce", "body": body}
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


# --- (1) le run async : fill-empty-only, un lot par langue --------------------------


async def test_async_run_fills_only_empty_variants(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    headers = agent_headers(admin)
    tid = await _template(client, headers)
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    template.body_i18n = {"en": "Hello {client_name} (human)"}
    await db_session.commit()

    started = await client.post(f"/message-templates/{tid}/translate", headers=headers, json={})
    assert started.status_code == 202, started.text
    launched = started.json()
    assert launched["status"] == "pending"
    assert launched["message_template_id"] == tid
    remaining = sorted(set(TARGETS) - {"en"})
    assert launched["langs"] == remaining
    assert launched["progress"] == {"done": 0, "total": len(remaining)}
    job_id = launched["translation_job_id"]

    # Avec le transport ASGI, la tâche de fond est terminée ici.
    status = await client.get(f"/message-templates/translate-jobs/{job_id}", headers=headers)
    body = status.json()
    assert body["status"] == "done", body
    assert body["progress"] == {"done": len(remaining), "total": len(remaining)}
    assert body["translated_keys"] == len(remaining)
    assert body["points_charged"] == len(remaining) * POINTS_PER_CALL
    assert body["failed_keys"] == []

    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    # Le travail humain n'a pas bougé ; chaque variante IA garde ses jetons.
    assert template.body_i18n["en"] == "Hello {client_name} (human)"
    for lang in remaining:
        assert template.body_i18n[lang].startswith(f"[{lang}] ")
        assert "{client_name}" in template.body_i18n[lang]
        assert "{days_left}" in template.body_i18n[lang]
    # La mémoire de hachés vise le MODÈLE DE MESSAGE, jamais un parcours.
    trails = list(
        (
            await db_session.execute(
                select(AiTranslationSource).where(
                    AiTranslationSource.message_template_id == uuid.UUID(tid)
                )
            )
        ).scalars()
    )
    assert sorted(t.lang for t in trails) == remaining
    assert all(t.content_key == BODY_KEY and t.template_id is None for t in trails)


# --- (2) l'estimation, même pool ----------------------------------------------------


async def test_estimate_shares_the_journey_pool(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    headers = agent_headers(admin)
    tid = await _template(client, headers)

    before = await client.get(f"/message-templates/{tid}/translate/estimate", headers=headers)
    assert before.status_code == 200, before.text
    est = before.json()
    assert est["items"] == 1 and est["langs"] == TARGETS
    assert est["estimated_points"] > 0
    assert est["quota_used"] == 0
    assert {c["empty"] for c in est["counts"].values()} == {1}

    # Un run débite LE pool commun : l'estimation suivante le voit.
    assert (
        await client.post(f"/message-templates/{tid}/translate", headers=headers, json={})
    ).status_code == 202
    after = (
        await client.get(f"/message-templates/{tid}/translate/estimate", headers=headers)
    ).json()
    assert after["quota_used"] == N_TARGETS * POINTS_PER_CALL
    assert after["items"] == 0  # tout est rempli — plus rien à envoyer


# --- (3) quota dépassé : 403, fournisseur jamais appelé -----------------------------


async def test_quota_exceeded_blocks_before_the_call(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    headers = agent_headers(admin)
    tid = await _template(client, headers)
    db_session.add(AgencyAiUsage(agency_id=admin.agency_id, month=month_key(), points_used=200))
    await db_session.commit()

    response = await client.post(f"/message-templates/{tid}/translate", headers=headers, json={})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "ai.quota_exceeded"
    assert fake_provider["calls"] == []
    jobs = list(
        (
            await db_session.execute(
                select(AiTranslationJob).where(
                    AiTranslationJob.message_template_id == uuid.UUID(tid)
                )
            )
        ).scalars()
    )
    assert jobs == []


# --- (4) L'INVARIANT DUR : les {jetons} survivent, ou rien ne part ------------------


def test_item_error_catches_every_token_alteration() -> None:
    """Le grain unitaire de l'invariant : renommé, perdu, dupliqué — chacun
    est un défaut nommé ; un texte sans jeton reste hors sujet (no-op)."""
    src = "Bonjour {client_name}, {days_left} jours."
    assert _item_error({"en": "Hello {client_name}, {days_left} days."}, src, "en") is None
    for bad in (
        "Hello {nom_du_client}, {days_left} days.",
        "Hello, {days_left} days.",
        "Hello {client_name} {client_name}, {days_left} d.",
    ):
        error = _item_error({"en": bad}, src, "en")
        assert error is not None and "interpolation tokens" in error
    assert _item_error({"en": "No token at all."}, "Sans jeton.", "en") is None


async def test_broken_token_is_repaired_on_retry(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """Premier appel : le modèle renomme {client_name} en espagnol. Le
    repair pass re-demande CE champ seul, plus stricte — et la variante
    écrite est la bonne. Rien d'autre n'a été perturbé."""
    fake_provider["break_token_for"] = {"es"}
    fake_provider["heal_on_retry"] = True
    headers = agent_headers(admin)
    tid = await _template(client, headers)

    started = await client.post(
        f"/message-templates/{tid}/translate", headers=headers, json={"target_langs": ["es"]}
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    body = (await client.get(f"/message-templates/translate-jobs/{job_id}", headers=headers)).json()
    assert body["status"] == "done" and body["failed_keys"] == []
    # Deux appels : le lot, puis le strict retry sur le champ cassé.
    assert [c["strict"] for c in fake_provider["calls"]] == [False, True]

    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    assert "{client_name}" in template.body_i18n["es"]
    assert "{nom_du_client}" not in template.body_i18n["es"]


async def test_unrepairable_token_is_a_gap_never_a_silent_publish(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """Le modèle casse le jeton MÊME au strict retry : la variante n'est
    PAS écrite, le job finit done_with_gaps (jamais failed — les autres
    langues sont écrites), et le résidu est nommé pour la revue humaine."""
    fake_provider["break_token_for"] = {"es"}
    headers = agent_headers(admin)
    tid = await _template(client, headers)

    started = await client.post(
        f"/message-templates/{tid}/translate",
        headers=headers,
        json={"target_langs": ["es", "en"]},
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    body = (await client.get(f"/message-templates/translate-jobs/{job_id}", headers=headers)).json()
    assert body["status"] == "done_with_gaps", body
    assert body["failed_keys"] == [f"es:{BODY_KEY}"]
    assert body["translated_keys"] == 1  # en, écrite

    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    assert "es" not in template.body_i18n  # RIEN publié en silence
    assert "{client_name}" in template.body_i18n["en"]


# --- (5) obsolescence : la source bouge, l'humain est intouchable -------------------


async def test_staleness_and_human_corrections(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    headers = agent_headers(admin)
    tid = await _template(client, headers)
    assert (
        await client.post(f"/message-templates/{tid}/translate", headers=headers, json={})
    ).status_code == 202

    # L'agence corrige la variante ES à la main, puis édite la SOURCE.
    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    template.body_i18n = {**template.body_i18n, "es": "Hola {client_name} (corregido a mano)"}
    await db_session.commit()
    patched = await client.patch(
        f"/message-templates/{tid}",
        headers=headers,
        json={"body": SOURCE_BODY + " Merci."},
    )
    assert patched.status_code == 200, patched.text

    est = (
        await client.get(
            f"/message-templates/{tid}/translate/estimate?include_stale=true", headers=headers
        )
    ).json()
    # Toutes les variantes IA sont stale ; la correction humaine, jamais.
    assert est["counts"]["es"] == {"empty": 0, "stale": 0}
    assert {est["counts"][lang]["stale"] for lang in TARGETS if lang != "es"} == {1}

    assert (
        await client.post(
            f"/message-templates/{tid}/translate", headers=headers, json={"include_stale": True}
        )
    ).status_code == 202
    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.body_i18n["es"] == "Hola {client_name} (corregido a mano)"
    assert "Merci." in template.body_i18n["en"]  # retraduite depuis la source neuve

    # L'écrasement CONSENTI, langue par langue : es se régénère sur demande
    # explicite — et sur elle seule.
    assert (
        await client.post(
            f"/message-templates/{tid}/translate",
            headers=headers,
            json={"retranslate_langs": ["es"]},
        )
    ).status_code == 202
    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.body_i18n["es"].startswith("[es] ")


# --- (6) la résolution AU FIGEAGE ---------------------------------------------------


@pytest_asyncio.fixture
async def case_for(admin: Agent, make_expat_user: MakeExpatUser, make_client_case: MakeClientCase):
    async def _make(preferred_lang: str) -> ClientCase:
        expat = await make_expat_user(
            email=f"figeage-{preferred_lang}-{uuid.uuid4().hex[:6]}@example.com",
            first_name="Ana",
            last_name="Silva",
            preferred_lang=preferred_lang,
        )
        return await make_client_case(agency_id=admin.agency_id, principal_expat_user_id=expat.id)

    return _make


async def test_freeze_picks_the_recipient_variant(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    case_for,
) -> None:
    """Le rappel né d'un modèle part dans la langue de CELLE qui lira —
    variante présente → la variante ; absente → la source. Jamais un
    blocage : la langue ne peut pas empêcher une relance de partir."""
    headers = agent_headers(admin)
    tid = await _template(client, headers, body="Bonjour {client_name}, des nouvelles ?")
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    template.body_i18n = {"es": "Hola {client_name}, ¿alguna novedad?"}
    await db_session.commit()

    async def freeze(case: ClientCase) -> str:
        created = await client.post(
            f"/cases/{case.id}/reminders",
            headers=headers,
            json={
                "channel": "mail",
                "scheduled_at": _FUTURE.isoformat(),
                "recipient_type": "expat",
                "message_template_id": tid,
            },
        )
        assert created.status_code == 201, created.text
        return created.json()["message_body"]

    # Variante ES présente + destinataire hispanophone → la variante,
    # jetons résolus au figeage comme pour un texte libre.
    assert await freeze(await case_for("es")) == "Hola Ana Silva, ¿alguna novedad?"
    # Pas de variante PT → la SOURCE, sans erreur ni blocage.
    assert await freeze(await case_for("pt")) == "Bonjour Ana Silva, des nouvelles ?"


# --- (7) l'écriture humaine garde le scalaire en phase ------------------------------


async def test_human_blob_write_keeps_the_scalar_mirrored(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template(client, headers)
    patched = await client.patch(
        f"/message-templates/{tid}",
        headers=headers,
        json={"body_i18n": {"fr": "Bonjour {client_name}, on avance ?", "en": "Hello!"}},
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    # Le scalaire suit la variante par défaut (fr) — l'idiome des parcours.
    assert body["body"] == "Bonjour {client_name}, on avance ?"
    assert body["body_i18n"]["en"] == "Hello!"


# --- (8) les guichets ne se répondent pas l'un pour l'autre -------------------------


async def test_a_journey_job_is_404_on_the_message_endpoint(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
) -> None:
    headers = agent_headers(admin)
    created = await client.post("/journeys", headers=headers, json={"name": "T"})
    job = AiTranslationJob(
        agency_id=admin.agency_id,
        template_id=uuid.UUID(created.json()["id"]),
        status="done",
        langs=["en"],
        progress_done=1,
        progress_total=1,
    )
    db_session.add(job)
    await db_session.commit()

    response = await client.get(f"/message-templates/translate-jobs/{job.id}", headers=headers)
    assert response.status_code == 404, response.text


# --- le rendu des response_model, épinglé -------------------------------------------


async def test_response_models_render_with_their_exact_keys(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """Les DEUX payloads neufs, rendus depuis de vraies lignes et épinglés
    clé par clé — un champ qui explose à la sérialisation ou un champ
    fantôme se voit ICI, pas dans un log de warning Pydantic."""
    headers = agent_headers(admin)
    tid = await _template(client, headers)

    template_payload = (
        await client.patch(f"/message-templates/{tid}", headers=headers, json={"language": "fr"})
    ).json()
    assert set(template_payload) == {
        "id",
        "name",
        "body",
        "body_i18n",
        "language",
        "channel",
        "created_at",
        "updated_at",
        "deprecated_tokens",
    }

    job_payload = (
        await client.post(f"/message-templates/{tid}/translate", headers=headers, json={})
    ).json()
    assert set(job_payload) == {
        "id",
        "translation_job_id",
        "template_id",  # le schéma UNIQUE à deux cibles : nul ici
        "message_template_id",
        "status",
        "langs",
        "progress",
        "translated_keys",
        "points_charged",
        "error",
        "failed_keys",
        "created_at",
        "updated_at",
    }

    estimate_payload = (
        await client.get(f"/message-templates/{tid}/translate/estimate", headers=headers)
    ).json()
    assert set(estimate_payload) == {
        "items",
        "langs",
        "counts",
        "estimated_points",
        "quota_used",
        "quota_limit",
        "month",
    }


# --- l'estimation et le job chiffrent LE MÊME travail (correctif 14/08) -------------


async def test_estimate_with_retranslate_matches_the_job_charge(
    client: AsyncClient,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """Le mensonge signalé par le front : modèle entièrement traduit, une
    langue re-cochée — l'estimation annonçait zéro (le paramètre n'était
    même pas accepté) pendant que le job facturait. L'invariant gravé :
    l'estimation chiffre EXACTEMENT le travail que le job exécute — mêmes
    langues, même formule sur ce périmètre — et le job débite bien ces
    langues-là, pas une de moins."""
    from src.ai import quota

    headers = agent_headers(admin)
    tid = await _template(client, headers)
    assert (
        await client.post(f"/message-templates/{tid}/translate", headers=headers, json={})
    ).status_code == 202  # tout traduire d'abord : plus rien de vide

    est = (
        await client.get(
            f"/message-templates/{tid}/translate/estimate"
            "?retranslate_langs=es&retranslate_langs=ru",
            headers=headers,
        )
    ).json()
    assert est["items"] == 1
    assert est["langs"] == ["es", "ru"]
    assert est["estimated_points"] == quota.estimate_points(len(SOURCE_BODY), 1, 2)
    assert est["estimated_points"] > 0

    started = await client.post(
        f"/message-templates/{tid}/translate",
        headers=headers,
        json={"retranslate_langs": ["es", "ru"]},
    )
    assert started.status_code == 202, started.text
    job = (
        await client.get(
            f"/message-templates/translate-jobs/{started.json()['translation_job_id']}",
            headers=headers,
        )
    ).json()
    # Le job exécute LE périmètre annoncé — pas une langue de plus ou de
    # moins — et le débit couvre chacune de ces langues.
    assert job["langs"] == est["langs"]
    assert job["points_charged"] == 2 * POINTS_PER_CALL
    assert job["template_id"] is None and job["message_template_id"] == tid


# --- l'édition manuelle des variantes (complément 14/08) ----------------------------


async def test_partial_blob_patch_never_wipes_a_sibling_language(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    """LE défaut que le complément ferme : le PATCH du blob était un
    remplacement — deux langues éditées successivement s'écrasaient. C'est
    désormais une FUSION par langue : clé présente écrite, valeur vide
    effacée, clé absente intouchée."""
    headers = agent_headers(admin)
    tid = await _template(client, headers)

    first = await client.patch(
        f"/message-templates/{tid}",
        headers=headers,
        json={"body_i18n": {"es": "Hola {client_name}."}},
    )
    assert first.status_code == 200, first.text
    second = await client.patch(
        f"/message-templates/{tid}",
        headers=headers,
        json={"body_i18n": {"en": "Hello {client_name}."}},
    )
    assert second.status_code == 200, second.text
    blob = second.json()["body_i18n"]
    # La seconde édition n'a PAS emporté la première.
    assert blob["es"] == "Hola {client_name}." and blob["en"] == "Hello {client_name}."

    # La valeur vide EFFACE — et elle seule.
    cleared = await client.patch(
        f"/message-templates/{tid}", headers=headers, json={"body_i18n": {"es": ""}}
    )
    assert cleared.status_code == 200, cleared.text
    assert "es" not in cleared.json()["body_i18n"]
    assert cleared.json()["body_i18n"]["en"] == "Hello {client_name}."
    # Le corps source n'a jamais bougé pendant tout ça.
    assert cleared.json()["body"] == SOURCE_BODY


async def test_a_hand_written_variant_is_born_locked(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """Écrite à la main via le PATCH partiel, la variante ressort
    VERROUILLÉE sans geste supplémentaire : pas de trace IA, donc ni le
    remplissage global ni include_stale ne la touchent — seul le
    consentement explicite par langue l'écrase."""
    headers = agent_headers(admin)
    tid = await _template(client, headers)
    hand = "Hola {client_name}, ¿alguna novedad? (escrito a mano)"
    assert (
        await client.patch(
            f"/message-templates/{tid}", headers=headers, json={"body_i18n": {"es": hand}}
        )
    ).status_code == 200

    # 1. La traduction GLOBALE remplit les autres langues, pas celle-là.
    assert (
        await client.post(f"/message-templates/{tid}/translate", headers=headers, json={})
    ).status_code == 202
    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.body_i18n["es"] == hand
    assert template.body_i18n["en"].startswith("[en] ")

    # 2. include_stale non plus : sans trace IA, elle n'est jamais « stale ».
    started = await client.post(
        f"/message-templates/{tid}/translate", headers=headers, json={"include_stale": True}
    )
    assert started.status_code in (202, 409)  # 409 = plus rien à envoyer, tant mieux
    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.body_i18n["es"] == hand

    # 3. Seul le consentement EXPLICITE par langue l'écrase.
    assert (
        await client.post(
            f"/message-templates/{tid}/translate",
            headers=headers,
            json={"retranslate_langs": ["es"]},
        )
    ).status_code == 202
    db_session.expire_all()
    template = await db_session.get(MessageTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.body_i18n["es"].startswith("[es] ")
