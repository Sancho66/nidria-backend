"""AI journey translation v2 — ASYNC per-language runs, quota, perimeter.

Covers: (a) POST → 202 + run; the background worker fills ONLY empty
variants (one provider call per language, progress complete), existing
translations untouched, usage event once; (b) quota exceeded → 403 and
the provider is NEVER called (no run either); (c) provider failure →
run failed, nothing written, nothing debited; (c2) mid-run failure keeps
the completed languages (written AND debited); (d) the payload builder
only carries template content; (e) dashes in the model output stripped +
Cyrillic enforced; (f) monthly reset; plus the estimate endpoint and
nothing-to-translate; (g) STALENESS: a source edit marks the AI
variants stale (detected, retranslated, hash trail updated) while
human translations and human corrections are never touched; (h)
RETRANSLATE (consented overwrite): a language regenerates entirely on
explicit request — trail laid on pre-feature translations (staleness
finally works on them), human retouches overwritten BY CONTRACT; (i)
REPAIR pass, FIELD grain (live 2026-07-06: the model echoes verbatim an
item whose text is not in the declared source language): a botched item
is re-asked ALONE with a stricter instruction and the lot survives; a
still-botched repair fails the JOB but the lot's valid fields are
written and debited pro rata; protected latin terms in a valid ru
translation never trigger the repair."""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.ai_translation_job import AiTranslationJob, AiTranslationSource
from shared.models.ai_usage import AgencyAiUsage
from shared.models.custom_field import CustomFieldDefinition
from shared.models.journey import JourneySection, JourneyTemplate, JourneyTemplateStep
from shared.models.rbac import Role
from shared.models.usage import UsageEvent
from src.ai import translation_client
from src.ai.quota import month_key, points_for_usage
from src.journeys.sample_seed import BG_FL_NAME, seed_sample_journeys
from src.journeys.translation_manager import build_translation_entries, content_hash
from tests.plugins.agent_plugin import AuthHeaders, MakeAgent

pytestmark = pytest.mark.usefixtures("rbac_baseline")

USAGE = {"prompt_tokens": 900, "completion_tokens": 2500}
POINTS_PER_CALL = points_for_usage(USAGE)  # config-pinned in conftest


@pytest_asyncio.fixture
async def admin(make_agent: MakeAgent, system_roles: dict[str, Role]) -> Agent:
    return await make_agent(role=system_roles["admin"])


@pytest.fixture
def fake_provider(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the raw HTTP call: echoes '[lang] text' (Cyrillic-marked
    for ru — the script validation is real), records every call."""
    state: dict[str, Any] = {"calls": []}

    async def _fake(
        items: list[dict[str, str]],
        source_lang: str,
        target_langs: list[str],
        strict_retry: bool = False,
    ):
        state["calls"].append({"items": items, "source": source_lang, "targets": target_langs})
        translations = {
            item["key"]: {
                lang: f"[{lang}] {'Перевод ' if lang == 'ru' else ''}{item['text']}"
                for lang in target_langs
            }
            for item in items
        }
        return translations, dict(USAGE)

    import src.journeys.translation_manager as tm

    monkeypatch.setattr(tm.translation_client, "request_translations", _fake)
    return state


async def _template_with_content(client: AsyncClient, headers: dict[str, str]) -> str:
    template = await client.post("/journeys", headers=headers, json={"name": "Résidence D7"})
    tid = template.json()["id"]
    step = await client.post(
        f"/journeys/{tid}/steps", headers=headers, json={"name": "Dépôt du dossier"}
    )
    assert step.status_code == 201
    return tid


# --- (a) async run: fill-empty-only, per-language progress ------------------------------------


async def test_async_run_fills_only_empty_variants(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    headers = agent_headers(admin)
    agency_id = admin.agency_id
    tid = await _template_with_content(client, headers)
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    template.name_i18n = {**template.name_i18n, "en": "D7 Residency (human)"}
    await db_session.commit()

    started = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert started.status_code == 202, started.text
    launched = started.json()
    assert launched["status"] == "pending"
    assert launched["langs"] == ["en", "es", "it", "pt", "ru"]
    assert launched["progress"] == {"done": 0, "total": 5}
    job_id = launched["translation_job_id"]

    # With the ASGI transport the background task has completed by now.
    status = await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)
    body = status.json()
    assert body["status"] == "done", body
    assert body["progress"] == {"done": 5, "total": 5}
    assert body["points_charged"] == 5 * POINTS_PER_CALL  # one debit per lot

    # One provider call PER language; the EN call excludes the already-
    # translated template name (only the step needed EN).
    assert [c["targets"] for c in fake_provider["calls"]] == [
        ["en"],
        ["es"],
        ["it"],
        ["pt"],
        ["ru"],
    ]
    en_call = fake_provider["calls"][0]
    assert [i["key"] for i in en_call["items"]] != []
    assert all(i["key"] != "template.name" for i in en_call["items"])

    db_session.expire_all()
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.name_i18n["en"] == "D7 Residency (human)"  # untouched
    assert template.name_i18n["es"] == "[es] Résidence D7"  # filled
    step = (
        await db_session.execute(
            select(JourneyTemplateStep).where(JourneyTemplateStep.template_id == uuid.UUID(tid))
        )
    ).scalar_one()
    assert step.name_i18n["en"] == "[en] Dépôt du dossier"
    assert step.name_i18n["ru"].startswith("[ru] Перевод")

    event = (
        await db_session.execute(
            select(UsageEvent).where(
                UsageEvent.agency_id == agency_id,
                UsageEvent.event_type == "ai.translation_used",
            )
        )
    ).scalar_one()  # exactly ONE event for the whole run
    assert event.details["points"] == body["points_charged"]

    # Everything filled → a new run is refused (nothing to translate).
    again = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert again.status_code == 409
    assert again.json()["code"] == "ai.nothing_to_translate"


# --- (a-bis) the REAL Bulgaria library sample: N fields detected, N translated ------------------


async def test_bulgaria_sample_detects_and_translates_every_field(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """The 2026-07-05 '0 languages translated' case, pinned on a REAL
    library journey: clone the Bulgaria freelance sample, strip the clone
    back to French-only (the agency-authored state), then check the
    estimate detects EVERY text field and the run fills EVERY variant."""
    headers = agent_headers(admin)
    await seed_sample_journeys(db_session)
    sample_id = (
        await db_session.execute(
            select(JourneyTemplate.id).where(
                JourneyTemplate.name == BG_FL_NAME, JourneyTemplate.is_sample.is_(True)
            )
        )
    ).scalar_one()
    clone = await client.post(f"/journeys/{sample_id}/clone", headers=headers, json={})
    assert clone.status_code == 201, clone.text
    tid = uuid.UUID(clone.json()["id"])

    template = await db_session.get(JourneyTemplate, tid)
    assert template is not None
    steps = list(
        (
            await db_session.execute(
                select(JourneyTemplateStep).where(JourneyTemplateStep.template_id == tid)
            )
        ).scalars()
    )
    sections = list(
        (
            await db_session.execute(
                select(JourneySection).where(JourneySection.template_id == tid)
            )
        ).scalars()
    )
    rows: list[tuple[Any, tuple[str, ...]]] = [
        (template, ("name",)),
        *[(s, ("name", "content_note")) for s in steps],
        *[(s, ("name", "description")) for s in sections],
    ]
    # Wipe every non-FR variant off the CLONE and count the translatable
    # fields INDEPENDENTLY of the detector (same source rule: fr variant,
    # else the scalar).
    expected = 0
    for obj, attrs in rows:
        for attr in attrs:
            blob = dict(getattr(obj, f"{attr}_i18n") or {})
            setattr(obj, f"{attr}_i18n", {k: v for k, v in blob.items() if k == "fr"})
            if (blob.get("fr") or getattr(obj, attr) or "").strip():
                expected += 1
    await db_session.commit()
    assert len(steps) >= 3 and expected > 10  # a real journey, not a toy

    estimate = (await client.get(f"/journeys/{tid}/translate/estimate", headers=headers)).json()
    assert estimate["items"] == expected  # N fields DETECTED
    assert estimate["langs"] == ["en", "es", "it", "pt", "ru"]

    started = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "done", status
    assert status["progress"] == {"done": 5, "total": 5}
    assert status["translated_keys"] == 5 * expected  # N fields x 5 langs TRANSLATED

    # Every detected field now carries the 5 target variants in base
    # (re-FETCHED: the worker wrote through its own session).
    db_session.expire_all()
    template = await db_session.get(JourneyTemplate, tid)
    assert template is not None
    steps = list(
        (
            await db_session.execute(
                select(JourneyTemplateStep).where(JourneyTemplateStep.template_id == tid)
            )
        ).scalars()
    )
    sections = list(
        (
            await db_session.execute(
                select(JourneySection).where(JourneySection.template_id == tid)
            )
        ).scalars()
    )
    rows = [
        (template, ("name",)),
        *[(s, ("name", "content_note")) for s in steps],
        *[(s, ("name", "description")) for s in sections],
    ]
    for obj, attrs in rows:
        for attr in attrs:
            blob = dict(getattr(obj, f"{attr}_i18n") or {})
            if (blob.get("fr") or getattr(obj, attr) or "").strip():
                assert set(blob) >= {"en", "es", "it", "pt", "ru"}, (attr, blob)

    again = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert again.status_code == 409
    assert again.json()["code"] == "ai.nothing_to_translate"


# --- estimate: the front's honest number -------------------------------------------------------


async def test_estimate_reports_points_and_quota(
    client: AsyncClient, admin: Agent, agent_headers: AuthHeaders
) -> None:
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    estimate = await client.get(f"/journeys/{tid}/translate/estimate", headers=headers)
    assert estimate.status_code == 200, estimate.text
    body = estimate.json()
    assert body["items"] == 2  # template name + step name
    assert body["langs"] == ["en", "es", "it", "pt", "ru"]
    assert body["counts"]["en"] == {"empty": 2, "stale": 0}  # the front's split
    assert body["estimated_points"] >= 1
    assert body["quota_limit"] == 200
    assert body["quota_used"] == 0


# --- (g) staleness: source edits resend AI variants, human work is untouchable ------------------


async def _fresh_step(db_session: AsyncSession, tid: str) -> JourneyTemplateStep:
    db_session.expire_all()
    return (
        await db_session.execute(
            select(JourneyTemplateStep).where(JourneyTemplateStep.template_id == uuid.UUID(tid))
        )
    ).scalar_one()


async def test_source_edit_marks_stale_and_retranslation_overwrites(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """(a) + (d): after a FR edit the default mode still says 'nothing
    to translate' (fill-empty untouched) but the estimate now exposes
    the stale count; include_stale resends EXACTLY the drifted field,
    overwrites its 5 AI variants and refreshes the hash trail."""
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    first = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert first.status_code == 202

    # The hash trail exists for BOTH fields x 5 langs, even in default mode.
    trails = list(
        (
            await db_session.execute(
                select(AiTranslationSource).where(AiTranslationSource.template_id == uuid.UUID(tid))
            )
        ).scalars()
    )
    assert len(trails) == 2 * 5
    step_key = next(t.content_key for t in trails if t.content_key.startswith("step."))

    # The FR source moves THROUGH the editor API — like the front does:
    # apply_i18n_write updates the scalar AND its blob["fr"] mirror (the
    # detector reads the mirror first).
    step = await _fresh_step(db_session, tid)
    patched = await client.patch(
        f"/journeys/{tid}/steps/{step.id}",
        headers=headers,
        json={"name": "Dépôt du dossier COMPLET"},
    )
    assert patched.status_code == 200, patched.text

    estimate = (await client.get(f"/journeys/{tid}/translate/estimate", headers=headers)).json()
    assert estimate["items"] == 0  # default mode: 'already complete'...
    assert estimate["counts"]["es"] == {"empty": 0, "stale": 1}  # ...but the front SEES the stale
    assert all(estimate["counts"][lang] == {"empty": 0, "stale": 1} for lang in estimate["counts"])
    default_post = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert default_post.status_code == 409  # (d) fill-empty-only unchanged
    assert default_post.json()["code"] == "ai.nothing_to_translate"

    stale_estimate = (
        await client.get(f"/journeys/{tid}/translate/estimate?include_stale=true", headers=headers)
    ).json()
    assert stale_estimate["items"] == 1
    assert stale_estimate["langs"] == ["en", "es", "it", "pt", "ru"]

    started = await client.post(
        f"/journeys/{tid}/translate", headers=headers, json={"include_stale": True}
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "done", status
    assert status["progress"] == {"done": 5, "total": 5}
    assert status["translated_keys"] == 5  # ONLY the drifted field, x5 langs

    step = await _fresh_step(db_session, tid)
    assert step.name_i18n["es"] == "[es] Dépôt du dossier COMPLET"  # overwritten
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.name_i18n["es"] == "[es] Résidence D7"  # untouched (not stale)

    trail = (
        await db_session.execute(
            select(AiTranslationSource).where(
                AiTranslationSource.template_id == uuid.UUID(tid),
                AiTranslationSource.content_key == step_key,
                AiTranslationSource.lang == "es",
            )
        )
    ).scalar_one()
    assert trail.source_hash == content_hash("Dépôt du dossier COMPLET")  # hash refreshed
    assert trail.output_hash == content_hash("[es] Dépôt du dossier COMPLET")

    after = (
        await client.get(f"/journeys/{tid}/translate/estimate?include_stale=true", headers=headers)
    ).json()
    assert after["items"] == 0  # nothing stale left
    assert all(after["counts"][lang] == {"empty": 0, "stale": 0} for lang in after["counts"])


async def test_human_corrected_variant_is_protected_from_retranslation(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """(b): a variant hand-corrected AFTER an AI translation no longer
    matches the recorded AI output — it is treated as human and NEVER
    resent, even though its source hash drifted."""
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    first = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert first.status_code == 202

    step = await _fresh_step(db_session, tid)
    step.name_i18n = {**step.name_i18n, "es": "Corrección humana"}  # human fixes ES
    await db_session.commit()
    patched = await client.patch(  # then the FR source moves (editor API)
        f"/journeys/{tid}/steps/{step.id}",
        headers=headers,
        json={"name": "Nouveau dépôt du dossier"},
    )
    assert patched.status_code == 200, patched.text

    estimate = (
        await client.get(f"/journeys/{tid}/translate/estimate?include_stale=true", headers=headers)
    ).json()
    assert estimate["counts"]["es"] == {"empty": 0, "stale": 0}  # protected
    assert estimate["counts"]["en"] == {"empty": 0, "stale": 1}
    assert estimate["langs"] == ["en", "it", "pt", "ru"]  # ES is not even a lot

    started = await client.post(
        f"/journeys/{tid}/translate", headers=headers, json={"include_stale": True}
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "done", status
    assert status["progress"] == {"done": 4, "total": 4}

    step = await _fresh_step(db_session, tid)
    assert step.name_i18n["es"] == "Corrección humana"  # human work intact
    assert step.name_i18n["en"] == "[en] Nouveau dépôt du dossier"  # AI variants refreshed

    # Still protected on the next pass: the trail kept its old hashes and
    # the variant still differs from the recorded AI output.
    again = (
        await client.get(f"/journeys/{tid}/translate/estimate?include_stale=true", headers=headers)
    ).json()
    assert again["items"] == 0
    assert again["counts"]["es"] == {"empty": 0, "stale": 0}


async def test_pure_human_translation_is_never_stale(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """(c): fields translated by a human only (no hash trail) are never
    stale — a source edit changes nothing, in either mode."""
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    human = {lang: f"human {lang}" for lang in ("en", "es", "it", "pt", "ru")}
    step = await _fresh_step(db_session, tid)  # expire_all FIRST, then mutate
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    template.name_i18n = {**template.name_i18n, **human}  # keep the fr mirror
    step.name_i18n = {**step.name_i18n, **human}
    await db_session.commit()
    # Both FR sources move through the editor API.
    assert (
        await client.patch(f"/journeys/{tid}", headers=headers, json={"name": "Résidence D7 v2"})
    ).status_code == 200
    assert (
        await client.patch(
            f"/journeys/{tid}/steps/{step.id}", headers=headers, json={"name": "Dépôt v2"}
        )
    ).status_code == 200

    estimate = (
        await client.get(f"/journeys/{tid}/translate/estimate?include_stale=true", headers=headers)
    ).json()
    assert estimate["items"] == 0
    assert all(estimate["counts"][lang] == {"empty": 0, "stale": 0} for lang in estimate["counts"])

    started = await client.post(
        f"/journeys/{tid}/translate", headers=headers, json={"include_stale": True}
    )
    assert started.status_code == 409
    assert started.json()["code"] == "ai.nothing_to_translate"
    assert fake_provider["calls"] == []  # the provider was never reached

    step = await _fresh_step(db_session, tid)
    assert step.name_i18n["es"] == "human es"  # untouched


# --- (h) retranslate: consented overwrite, trail laid on pre-feature translations ---------------


async def test_retranslate_lang_overwrites_and_seeds_the_trail(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """(a): a language translated BEFORE the staleness feature (filled,
    no trail) is fully regenerated by retranslate_langs — and the trail
    laid by that run makes a LATER source edit finally detectable."""
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    human = {lang: f"human {lang}" for lang in ("en", "es", "it", "pt", "ru")}
    step = await _fresh_step(db_session, tid)
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    template.name_i18n = {**template.name_i18n, **human}
    step.name_i18n = {**step.name_i18n, **human}
    await db_session.commit()

    # Everything filled: the default estimate has nothing to send...
    default = (await client.get(f"/journeys/{tid}/translate/estimate", headers=headers)).json()
    assert default["items"] == 0
    # ...but the retranslate estimate covers EVERY field of that language.
    estimate = (
        await client.get(
            f"/journeys/{tid}/translate/estimate?retranslate_langs=es", headers=headers
        )
    ).json()
    assert estimate["items"] == 2  # all fields, not just empty+stale
    assert estimate["langs"] == ["es"]
    assert estimate["estimated_points"] >= 1

    started = await client.post(
        f"/journeys/{tid}/translate", headers=headers, json={"retranslate_langs": ["es"]}
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "done", status
    assert status["langs"] == ["es"]
    assert status["progress"] == {"done": 1, "total": 1}
    assert status["translated_keys"] == 2

    step = await _fresh_step(db_session, tid)
    assert step.name_i18n["es"] == "[es] Dépôt du dossier"  # regenerated
    assert step.name_i18n["en"] == "human en"  # other languages untouched
    trails = list(
        (
            await db_session.execute(
                select(AiTranslationSource).where(AiTranslationSource.template_id == uuid.UUID(tid))
            )
        ).scalars()
    )
    assert len(trails) == 2 and {t.lang for t in trails} == {"es"}

    # THE transition: with the trail laid, a source edit IS detected.
    patched = await client.patch(
        f"/journeys/{tid}/steps/{step.id}", headers=headers, json={"name": "Dépôt révisé"}
    )
    assert patched.status_code == 200, patched.text
    after = (
        await client.get(f"/journeys/{tid}/translate/estimate?include_stale=true", headers=headers)
    ).json()
    assert after["counts"]["es"] == {"empty": 0, "stale": 1}  # finally detectable
    assert after["counts"]["en"] == {"empty": 0, "stale": 0}  # still trail-less


async def test_retranslate_overwrites_human_retouches_by_contract(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    """(b): in retranslate mode a human retouch IS overwritten — that is
    the consented contract (the front warned before sending the mode)."""
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    first = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert first.status_code == 202
    step = await _fresh_step(db_session, tid)
    step.name_i18n = {**step.name_i18n, "es": "Retouche humaine"}
    await db_session.commit()

    started = await client.post(
        f"/journeys/{tid}/translate", headers=headers, json={"retranslate_langs": ["es"]}
    )
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "done", status
    assert status["translated_keys"] == 2  # every ES field resent

    step = await _fresh_step(db_session, tid)
    assert step.name_i18n["es"] == "[es] Dépôt du dossier"  # the retouch is gone, as consented
    assert step.name_i18n["en"] == "[en] Dépôt du dossier"  # other languages untouched


# --- (i) salvage pass: a botched item is re-asked alone, the lot survives -----------------------


def _slip_provider(calls: list[dict[str, Any]], slips: int) -> Any:
    """A provider whose FIRST `slips` RU answers echo plain English under
    template.name (the live failure) — everything else clean."""
    ru_calls = {"n": 0}

    async def _fake(
        items: list[dict[str, str]],
        source_lang: str,
        target_langs: list[str],
        strict_retry: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        calls.append(
            {
                "keys": [i["key"] for i in items],
                "targets": list(target_langs),
                "strict": strict_retry,
            }
        )
        out: dict[str, Any] = {
            item["key"]: {
                lang: f"[{lang}] {'Перевод ' if lang == 'ru' else ''}{item['text']}"
                for lang in target_langs
            }
            for item in items
        }
        if target_langs == ["ru"] and any(i["key"] == "template.name" for i in items):
            ru_calls["n"] += 1
            if ru_calls["n"] <= slips:
                out["template.name"] = {"ru": "Full residency form required."}  # English echo
        return out, dict(USAGE)

    return _fake


async def test_salvage_pass_reasks_only_the_botched_item(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    calls: list[dict[str, Any]] = []
    import src.journeys.translation_manager as tm

    monkeypatch.setattr(tm.translation_client, "request_translations", _slip_provider(calls, 1))

    started = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "done", status  # the lot SURVIVED the slip
    assert status["progress"] == {"done": 5, "total": 5}

    # 6 calls total: 5 lots + 1 repair, and the repair re-asked ONLY the
    # botched key, with the HARDENED instruction; the extra call is
    # debited (real cost).
    ru = [c for c in calls if c["targets"] == ["ru"]]
    assert len(ru) == 2 and ru[1]["keys"] == ["template.name"]
    assert ru[0]["strict"] is False and ru[1]["strict"] is True
    assert status["points_charged"] == 6 * POINTS_PER_CALL

    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.name_i18n["ru"].startswith("[ru] Перевод")  # the re-ask landed


async def test_persistent_repair_failure_keeps_the_valid_fields(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both the batch AND the repair come back wrong for ONE field → the
    job fails naming it, but the lot's VALID fields are written and
    debited pro rata (field grain, not lot grain)."""
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    calls: list[dict[str, Any]] = []
    import src.journeys.translation_manager as tm

    monkeypatch.setattr(tm.translation_client, "request_translations", _slip_provider(calls, 2))

    started = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "failed", status
    assert status["error"] == "ai.translation_failed"
    assert status["progress"] == {"done": 4, "total": 5}  # the ru lot never completed
    assert status["translated_keys"] == 4 * 2 + 1  # ...but its valid field IS written
    # Debit pro rata: the ru lot cost 2 calls for 2 fields, 1 written →
    # ceil(2 x 1/2) = 1 call-equivalent on top of the 4 clean lots.
    assert status["points_charged"] == 5 * POINTS_PER_CALL

    db_session.expire_all()
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    assert "ru" not in template.name_i18n  # nothing English ever written
    assert template.name_i18n["pt"] == "[pt] Résidence D7"  # completed lots kept
    step = await _fresh_step(db_session, tid)
    assert step.name_i18n["ru"].startswith("[ru] Перевод")  # the VALID ru field landed
    trail_langs = {
        (t.content_key, t.lang)
        for t in (
            await db_session.execute(
                select(AiTranslationSource).where(
                    AiTranslationSource.template_id == uuid.UUID(tid),
                    AiTranslationSource.lang == "ru",
                )
            )
        ).scalars()
    }
    assert len(trail_langs) == 1  # trail laid for the written field only


def test_cyrillic_ratio_tolerates_protected_latin_terms() -> None:
    """(b) + (c): verbatim scheme names/acronyms in a VALID ru answer
    never trigger the repair; a full echo or a half-latin mix does; the
    other languages carry no script check at all."""
    from src.ai.translation_client import invalid_keys

    items = [{"key": "k", "text": "Dépôt du Pink Slip au consulat D7"}]
    valid_ru = {"k": {"ru": "Подача Pink Slip в консульство D7"}}
    assert invalid_keys(valid_ru, items, ["ru"]) == []
    echoed = {"k": {"ru": "Deposit of the Pink Slip at the consulate."}}
    assert invalid_keys(echoed, items, ["ru"]) == ["k"]
    mixed = {"k": {"ru": "Подача zayavleniya na vizu v konsulstvo"}}
    assert invalid_keys(mixed, items, ["ru"]) == ["k"]
    latin_es = {"k": {"es": "Entrega del Pink Slip en el consulado D7"}}
    assert invalid_keys(latin_es, items, ["es"]) == []


# --- (b) quota exceeded: 403, no run, provider never called ------------------------------------


async def test_quota_exceeded_blocks_before_the_call(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    db_session.add(AgencyAiUsage(agency_id=admin.agency_id, month=month_key(), points_used=200))
    await db_session.commit()

    response = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "ai.quota_exceeded"
    assert response.json()["params"] == {"used": 200, "limit": 200}
    assert fake_provider["calls"] == []
    jobs = list(
        (
            await db_session.execute(
                select(AiTranslationJob).where(AiTranslationJob.agency_id == admin.agency_id)
            )
        ).scalars()
    )
    assert jobs == []  # no job was even created


# --- (c) provider failure: run failed, nothing written, nothing debited ------------------------


async def test_provider_failure_debits_nothing(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)

    async def _boom(items, source_lang, target_langs):
        from src.core.exceptions import UpstreamError

        raise UpstreamError("Provider down.", code="ai.translation_failed")

    import src.journeys.translation_manager as tm

    monkeypatch.setattr(tm.translation_client, "request_translations", _boom)
    started = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert started.status_code == 202
    job_id = started.json()["translation_job_id"]

    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "failed"
    assert status["error"] == "ai.translation_failed"
    assert status["points_charged"] == 0

    usage_rows = list(
        (
            await db_session.execute(
                select(AgencyAiUsage).where(AgencyAiUsage.agency_id == admin.agency_id)
            )
        ).scalars()
    )
    assert usage_rows == []
    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None and "es" not in template.name_i18n


# --- (c2) mid-run failure keeps the completed languages ----------------------------------------


async def test_midrun_failure_keeps_completed_languages(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    calls = {"n": 0}

    async def _second_call_fails(items, source_lang, target_langs):
        calls["n"] += 1
        if calls["n"] >= 2:
            from src.core.exceptions import UpstreamError

            raise UpstreamError("Provider down.", code="ai.translation_failed")
        return {
            item["key"]: {lang: f"[{lang}] {item['text']}" for lang in target_langs}
            for item in items
        }, dict(USAGE)

    import src.journeys.translation_manager as tm

    monkeypatch.setattr(tm.translation_client, "request_translations", _second_call_fails)
    started = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert started.status_code == 202
    job_id = started.json()["translation_job_id"]

    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "failed"
    assert status["progress"] == {"done": 1, "total": 5}  # EN landed before the failure
    assert status["points_charged"] == POINTS_PER_CALL  # pro rata of successful lots

    template = await db_session.get(JourneyTemplate, uuid.UUID(tid))
    assert template is not None
    assert template.name_i18n["en"] == "[en] Résidence D7"  # kept
    assert "es" not in template.name_i18n  # never reached


# --- (d) the payload builder is template-only ---------------------------------------------------


def test_payload_builder_carries_template_content_only() -> None:
    template = JourneyTemplate(name="Parcours X", name_i18n={})
    step = JourneyTemplateStep(
        id=uuid.uuid4(),
        name="Étape 1",
        content_note="Note descendante.",
        name_i18n={},
        content_note_i18n={},
        position=0,
    )
    section = JourneySection(
        id=uuid.uuid4(), name="Identité", description=None, name_i18n={}, description_i18n={}
    )
    definition = CustomFieldDefinition(
        key="visa_number", label="Numéro de visa", label_i18n={}, field_type="text"
    )
    entries = build_translation_entries(template, [step], [section], [definition], "fr")
    texts = {e.key: e.text for e in entries}
    assert texts == {
        "template.name": "Parcours X",
        f"step.{step.id}.name": "Étape 1",
        f"step.{step.id}.content_note": "Note descendante.",
        f"section.{section.id}.name": "Identité",
        "field.visa_number.label": "Numéro de visa",
    }


# --- (e) dash + script guards on the model output -----------------------------------------------


def test_output_dashes_are_stripped_and_cyrillic_enforced() -> None:
    items = [{"key": "template.name", "text": "Bulgarie : Freelance"}]
    raw = {"template.name": {"en": "Bulgaria — Freelance", "es": "Bulgaria – Freelance"}}
    cleaned = translation_client.validate_translations(raw, items, ["en", "es"])
    assert cleaned["template.name"]["en"] == "Bulgaria : Freelance"
    assert cleaned["template.name"]["es"] == "Bulgaria - Freelance"

    romanized = {"template.name": {"ru": "Bolgariya: Frilans"}}
    with pytest.raises(Exception, match="Cyrillic"):
        translation_client.validate_translations(romanized, items, ["ru"])


# --- (b-bis) units: estimation and debit on an EXACT numeric case -------------------------------


def test_points_units_exact_case() -> None:
    """1 point = 0.1 centime. Prices pinned by conftest at 0.1/0.4 USD
    per Mtok: 1M in + 1M out = $0.50 = 50.0 centimes = 500 points. The
    estimate mirrors the DEBIT structure (audit 2026-07-05): one call
    per language, each floored at 1 point — never below n_langs."""
    from src.ai.quota import estimate_points

    assert points_for_usage({"prompt_tokens": 1_000_000, "completion_tokens": 1_000_000}) == 500
    # Audit case 1 shape (1 field, 21 chars, 5 langs): per-call cost is
    # tiny but each of the 5 calls floors at 1 point → 5, not 1.
    assert estimate_points(21, 1, 5) == 5
    # Above the floor, per-call cost scales with content + key echo:
    # 26 000 chars, 10 items, 2 langs → prompt 230+350+8667=9247 tok,
    # completion 15+350+10000=10365 tok → $0.0050707 → ceil(5.07)=6 ×2.
    assert estimate_points(26_000, 10, 2) == 12


# --- (f) monthly reset ---------------------------------------------------------------------------


async def test_monthly_reset_starts_a_clean_counter(
    client: AsyncClient,
    db_session: AsyncSession,
    admin: Agent,
    agent_headers: AuthHeaders,
    fake_provider: dict[str, Any],
) -> None:
    headers = agent_headers(admin)
    tid = await _template_with_content(client, headers)
    db_session.add(AgencyAiUsage(agency_id=admin.agency_id, month="2026-06", points_used=200))
    await db_session.commit()

    state = (await client.get("/agencies/me/ai-usage", headers=headers)).json()
    # THE exact shape the front consumes (the NaN fix: remaining served).
    assert state == {"used": 0, "limit": 200, "remaining": 200, "month": month_key()}

    started = await client.post(f"/journeys/{tid}/translate", headers=headers, json={})
    assert started.status_code == 202, started.text
    job_id = started.json()["translation_job_id"]
    status = (await client.get(f"/journeys/translate-jobs/{job_id}", headers=headers)).json()
    assert status["status"] == "done"
    state = (await client.get("/agencies/me/ai-usage", headers=headers)).json()
    assert state["used"] == status["points_charged"]
    assert state["remaining"] == 200 - status["points_charged"]

    job = (
        await db_session.execute(
            select(AiTranslationJob).where(AiTranslationJob.template_id == uuid.UUID(tid))
        )
    ).scalar_one()
    assert job.status == "done"
