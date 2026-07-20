"""AI translation of a journey template's 6-language variants — ASYNC.

STRICT PERIMETER, locked by construction: the entry points take a
template_id and read ONLY template tables (template name, step names +
content notes, section names/descriptions, and the labels of the
agency's custom-field definitions REFERENCED by the template). No case,
person or client table is ever queried here — the payload builder
(`build_translation_entries`) only accepts those rows, and the test
suite pins its output to them.

The provider work runs in a BACKGROUND task, ONE LANGUAGE PER CALL
(~20s each instead of one 90s monolith): the `ai_translation_run` row
is the progress bar the front polls, and a mid-run failure keeps the
completed languages — written and debited — while fill-empty-only makes
the retry idempotent (a second run only translates what is still empty).

Quota: estimated and gated BEFORE starting (403 ai.quota_exceeded),
debited per successful language call from the provider's real token
usage. A failed call debits nothing.

STALENESS: every AI-written variant leaves a hash trail in
`ai_translation_source` (source text hash + output hash). A variant is
STALE when the source drifted while the variant still IS the recorded
AI output; a variant with no trail, or that differs from the recorded
output (a human corrected it), is NEVER stale — human work is only
overwritten by humans. The default mode stays fill-empty-only;
`include_stale=True` also resends (and overwrites) the stale ones.

RETRANSLATE (`retranslate_langs`) is the CONSENTED overwrite: for those
languages every field is resent regardless of state — including human
work — and the hash trail is laid/refreshed, which re-arms staleness on
translations made before the trail existed (no backfill is possible by
principle: we cannot know what the AI produced back then). The back
never infers it; the front asks for explicit confirmation."""

import hashlib
import logging
import math
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.ai_translation_job import AiTranslationJob, AiTranslationSource
from shared.models.custom_field import CustomFieldDefinition
from shared.models.journey import JourneySection, JourneyTemplate, JourneyTemplateStep
from src.ai import quota, translation_client
from src.core.database import async_session_maker
from src.core.enums import ActorType, StepRequirementKind
from src.core.exceptions import ConflictError, NidriaError, NotFoundError, ValidationError
from src.core.i18n import SUPPORTED_LANGUAGES
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.journeys.journeys_repository import JourneysRepository
from src.journeys.journeys_schema import (
    JobProgress,
    LangTranslationCounts,
    TranslateEstimateResponse,
    TranslationJobResponse,
)
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

PENDING = "pending"
RUNNING = "running"
DONE = "done"
# The field-grain wrote the majority; a residue resisted even the repair.
# NOT a failure — the good fields are written and the residue is exposed.
DONE_WITH_GAPS = "done_with_gaps"
FAILED = "failed"

# Test hook: the background worker opens its OWN session (the request one
# closes with the response). The harness points this at the testcontainer;
# None = the app engine.
session_factory: Any | None = None


def _sessions() -> Any:
    return session_factory or async_session_maker


def content_hash(text: str) -> str:
    """Stable fingerprint of a translatable text (source or AI output).
    Staleness compares hashes, never full texts."""
    return hashlib.sha256(text.strip().encode()).hexdigest()


@dataclass
class TranslationEntry:
    """One translatable text of the template + where to write it back.
    `blob` is a snapshot of the current i18n dict; the write path merges
    into it so existing variants stay untouched. `needed` = empty
    variants, `stale` = AI-written variants whose source drifted."""

    key: str
    text: str
    blob: dict[str, str]
    obj: Any
    attr: str  # scalar attribute; the blob lives at f"{attr}_i18n"
    needed: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)


def send_langs(
    entry: TranslationEntry,
    include_stale: bool,
    retranslate: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """The languages this entry is actually sent for, per mode: empty
    variants, + stale ones (include_stale), + every retranslate language
    (consented overwrite — sent regardless of the variant's state)."""
    langs = entry.needed + (entry.stale if include_stale else [])
    return langs + [lang for lang in sorted(retranslate) if lang not in langs]


def build_translation_entries(
    template: JourneyTemplate,
    steps: list[JourneyTemplateStep],
    sections: list[JourneySection],
    definitions: list[CustomFieldDefinition],
    source_lang: str,
) -> list[TranslationEntry]:
    """THE perimeter lock: only template-table rows enter, only their
    text columns leave. Source text = the source-language variant when
    present, else the scalar (the agency's editing content)."""
    entries: list[TranslationEntry] = []

    def add(key: str, obj: Any, attr: str) -> None:
        blob = dict(getattr(obj, f"{attr}_i18n") or {})
        text = (blob.get(source_lang) or getattr(obj, attr) or "").strip()
        if text:
            entries.append(TranslationEntry(key=key, text=text, blob=blob, obj=obj, attr=attr))

    add("template.name", template, "name")
    for step in steps:
        add(f"step.{step.id}.name", step, "name")
        add(f"step.{step.id}.content_note", step, "content_note")
    for section in sections:
        add(f"section.{section.id}.name", section, "name")
        add(f"section.{section.id}.description", section, "description")
    for definition in definitions:
        add(f"field.{definition.key}.label", definition, "label")
    return entries


def job_response(job: AiTranslationJob) -> TranslationJobResponse:
    return TranslationJobResponse(
        id=job.id,
        translation_job_id=job.id,
        template_id=job.template_id,
        status=job.status,
        langs=list(job.langs or []),
        progress=JobProgress(done=job.progress_done, total=job.progress_total),
        translated_keys=job.translated_keys,
        points_charged=job.points_charged,
        error=job.error,
        failed_keys=list(job.failed_keys or []),
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


class TranslationManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = JourneysRepository(db)

    # --- shared assembly -----------------------------------------------------------

    async def _load_entries(
        self, agent: Agent, template_id: uuid.UUID, target_langs: list[str] | None
    ) -> tuple[JourneyTemplate, list[TranslationEntry], str, list[str]]:
        template = await self.repo.get_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Journey template not found.", code="journey.template_not_found")
        agency = await self.db.get(Agency, agent.agency_id)
        default = (agency.default_language if agency else "fr") or "fr"
        source_lang = default if default in SUPPORTED_LANGUAGES else "fr"

        targets = [lang for lang in (target_langs or SUPPORTED_LANGUAGES) if lang != source_lang]
        if not targets:
            raise ValidationError(
                "No target language to translate into.", code="ai.no_target_language"
            )

        steps = await self.repo.list_steps(template_id)
        sections = await self.repo.list_sections(template_id)
        fields = await self.repo.list_fields(template_id)
        referenced = {
            f.reference for f in fields if f.kind == StepRequirementKind.CUSTOM_FIELD.value
        }
        definitions = [
            d
            for d in await CustomFieldsRepository(self.db).list_for_agency(
                agent.agency_id, include_archived=True
            )
            if d.key in referenced
        ]
        entries = build_translation_entries(template, steps, sections, definitions, source_lang)
        hash_rows = {
            (r.content_key, r.lang): r
            for r in (
                await self.db.execute(
                    select(AiTranslationSource).where(
                        AiTranslationSource.template_id == template_id
                    )
                )
            ).scalars()
        }
        for entry in entries:
            entry.needed = [lang for lang in targets if not (entry.blob.get(lang) or "").strip()]
            src_hash = content_hash(entry.text)
            # STALE = the variant is still the AI's own output but the
            # source moved. No hash trail (human translation) or a variant
            # that drifted from the recorded output (human correction)
            # never qualifies.
            entry.stale = [
                lang
                for lang in targets
                if lang not in entry.needed
                and (row := hash_rows.get((entry.key, lang))) is not None
                and row.source_hash != src_hash
                and row.output_hash == content_hash(entry.blob[lang])
            ]
        # ALL entries are returned (even fully-translated ones): the
        # retranslate mode sends them regardless of state; callers pick
        # what actually goes out via `send_langs`.
        return template, entries, source_lang, targets

    # --- estimate (the front's honest number) ----------------------------------------

    async def estimate(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        target_langs: list[str] | None,
        include_stale: bool = False,
        retranslate_langs: list[str] | None = None,
    ) -> TranslateEstimateResponse:
        _template, entries, _source, targets = await self._load_entries(
            agent, template_id, target_langs
        )
        retranslate = set(retranslate_langs or []) & set(targets)
        to_send = [e for e in entries if send_langs(e, include_stale, retranslate)]
        union_langs = sorted(
            {lang for e in to_send for lang in send_langs(e, include_stale, retranslate)}
        )
        source_chars = sum(len(e.text) for e in to_send)
        used, limit, month = await quota.get_usage(self.db, agent.agency_id)
        estimated = (
            quota.estimate_points(source_chars, len(to_send), len(union_langs)) if to_send else 0
        )
        # The per-language {empty, stale} split is ALWAYS reported (both
        # modes): the front's modal needs it to offer the retranslate
        # choice when everything is filled but stale.
        counts = {
            lang: LangTranslationCounts(
                empty=sum(1 for e in entries if lang in e.needed),
                stale=sum(1 for e in entries if lang in e.stale),
            )
            for lang in targets
        }
        return TranslateEstimateResponse(
            items=len(to_send),
            langs=union_langs,
            counts=counts,
            estimated_points=estimated,
            quota_used=used,
            quota_limit=limit,
            month=month,
        )

    # --- start (202) + status -----------------------------------------------------------

    async def start_translation(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        target_langs: list[str] | None,
        include_stale: bool = False,
        retranslate_langs: list[str] | None = None,
    ) -> AiTranslationJob:
        """Validate, gate the quota, create the RUN row — the caller
        schedules `execute_run` in the background and answers 202."""
        template, entries, _source, targets = await self._load_entries(
            agent, template_id, target_langs
        )
        retranslate = set(retranslate_langs or []) & set(targets)
        to_send = [e for e in entries if send_langs(e, include_stale, retranslate)]
        if not to_send:
            raise ConflictError(
                "Nothing to translate: every requested variant is already filled.",
                code="ai.nothing_to_translate",
            )
        running = (
            await self.db.execute(
                select(AiTranslationJob).where(
                    AiTranslationJob.template_id == template.id,
                    AiTranslationJob.status.in_([PENDING, RUNNING]),
                )
            )
        ).scalar_one_or_none()
        if running is not None:
            raise ConflictError(
                "A translation is already running for this journey.",
                code="ai.translation_already_running",
            )
        union_langs = sorted(
            {lang for e in to_send for lang in send_langs(e, include_stale, retranslate)}
        )
        source_chars = sum(len(e.text) for e in to_send)
        await quota.ensure_quota(
            self.db,
            agent.agency_id,
            quota.estimate_points(source_chars, len(to_send), len(union_langs)),
        )
        job = AiTranslationJob(
            agency_id=agent.agency_id,
            template_id=template.id,
            status=PENDING,
            langs=union_langs,
            progress_done=0,
            progress_total=len(union_langs),
        )
        self.db.add(job)
        await self.db.commit()
        await self.db.refresh(job)
        return job

    async def get_job(self, agent: Agent, job_id: uuid.UUID) -> TranslationJobResponse:
        """Polling read — strictly agency-scoped (404, never 403)."""
        job = (
            await self.db.execute(
                select(AiTranslationJob).where(
                    AiTranslationJob.id == job_id,
                    AiTranslationJob.agency_id == agent.agency_id,
                )
            )
        ).scalar_one_or_none()
        if job is None:
            raise NotFoundError("Translation job not found.", code="ai.job_not_found")
        return job_response(job)


async def execute_job(
    job_id: uuid.UUID,
    agent: Agent,
    include_stale: bool = False,
    retranslate_langs: list[str] | None = None,
) -> None:
    """Background worker: OWN session, one provider call PER LOT (= one
    language), progress bumped after each lot so the bar has real grain.
    A failure marks the job failed and KEEPS everything already written —
    the grain is the FIELD: within a lot, the fields the model got right
    are written and debited pro rata even when some keys resist the
    repair pass; fill-empty-only makes the retry idempotent. Every write
    leaves its hash trail in `ai_translation_source` (source + output) —
    the staleness memory."""
    retranslate = set(retranslate_langs or [])
    async with _sessions()() as db:
        job = await db.get(AiTranslationJob, job_id)
        if job is None:
            return
        job.status = RUNNING
        await db.commit()
        manager = TranslationManager(db)
        try:
            for lang in list(job.langs or []):
                _t, entries, source_lang, _targets = await manager._load_entries(
                    agent, job.template_id, [lang]
                )
                sendable = [e for e in entries if lang in send_langs(e, include_stale, retranslate)]
                failed_keys: list[str] = []
                if sendable:
                    items = [{"key": e.key, "text": e.text} for e in sendable]
                    # Tracabilite sous-traitant (mitigation Z.ai 2026-07-19) :
                    # le TYPE de contenu part en log, JAMAIS le contenu. Le
                    # perimetre est verrouille par build_translation_entries
                    # (contenus de TEMPLATE uniquement, zero donnee de dossier).
                    kinds = Counter(e.key.split(".")[0] for e in sendable)
                    logger.info(
                        "ai translation -> Z.ai: template=%s lang=%s entries=%d types=%s",
                        job.template_id,
                        lang,
                        len(sendable),
                        dict(kinds),
                    )
                    # One batch call + one stricter repair pass on botched
                    # items — FIELD grain: valid fields are written even
                    # when some keys resist.
                    (
                        translations,
                        failed_keys,
                        usages,
                    ) = await translation_client.request_translations_with_repair(
                        items, source_lang, [lang]
                    )
                    written = [e for e in sendable if e.key in translations]
                    trails = {
                        r.content_key: r
                        for r in (
                            await db.execute(
                                select(AiTranslationSource).where(
                                    AiTranslationSource.template_id == job.template_id,
                                    AiTranslationSource.lang == lang,
                                    AiTranslationSource.content_key.in_([e.key for e in written]),
                                )
                            )
                        ).scalars()
                    }
                    for entry in written:
                        value = translations[entry.key][lang]
                        setattr(entry.obj, f"{entry.attr}_i18n", {**entry.blob, lang: value})
                        trail = trails.get(entry.key)
                        if trail is None:
                            db.add(
                                AiTranslationSource(
                                    agency_id=job.agency_id,
                                    template_id=job.template_id,
                                    content_key=entry.key,
                                    lang=lang,
                                    source_hash=content_hash(entry.text),
                                    output_hash=content_hash(value),
                                )
                            )
                        else:
                            trail.source_hash = content_hash(entry.text)
                            trail.output_hash = content_hash(value)
                    if written:
                        # Debit pro rata of the fields actually written.
                        full = sum(quota.points_for_usage(u) for u in usages)
                        points = max(1, math.ceil(full * len(written) / len(sendable)))
                        await quota.debit(db, job.agency_id, points)
                        job.points_charged += points
                        job.translated_keys += len(written)
                if failed_keys:
                    # FIELD-grain GAPS (option A): the good fields ARE written
                    # (above); the residue is recorded and EXPOSED, the lot is
                    # still processed — the job does NOT fail on a residue.
                    job.failed_keys = [
                        *(job.failed_keys or []),
                        *(f"{lang}:{k}" for k in failed_keys),
                    ]
                    logger.warning(
                        "AI translation job %s: %d/%d %s field(s) still invalid "
                        "after the repair pass (kept as gaps): %s",
                        job_id,
                        len(failed_keys),
                        len(sendable),
                        lang,
                        failed_keys,
                    )
                job.progress_done += 1
                await db.commit()  # progress + this lot's fills (+ gaps) land together
            # done_with_gaps iff a residue survived; DONE only when 0 residue.
            job.status = DONE_WITH_GAPS if job.failed_keys else DONE
            await UsageManager(db).emit(
                agency_id=job.agency_id,
                event_type="ai.translation_used",
                actor_type=ActorType.AGENT,
                actor_id=agent.id,
                details={
                    "template_id": str(job.template_id),
                    "langs": list(job.langs or []),
                    "points": job.points_charged,
                },
            )
            await db.commit()
        except NidriaError as exc:
            # The code goes to the job row (front i18n); the DETAIL —
            # which key, which language, why — only lives here.
            logger.warning("AI translation job %s failed: %s", job_id, exc)
            await db.rollback()
            job = await db.get(AiTranslationJob, job_id)
            if job is not None:
                job.status = FAILED
                job.error = exc.code
                await db.commit()
        except Exception:
            logger.exception("AI translation job %s crashed", job_id)
            await db.rollback()
            job = await db.get(AiTranslationJob, job_id)
            if job is not None:
                job.status = FAILED
                job.error = "ai.translation_failed"
                await db.commit()
