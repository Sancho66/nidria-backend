"""Traduction IA des variantes d'un modèle de message — DÉCALQUE du système
des parcours, et c'est un choix, pas un raccourci : même table de jobs, même
mémoire de hachés, même pool de points mensuel, même barre de progression par
langue, mêmes règles d'obsolescence. Le front qui sait suivre un job de
parcours sait suivre celui-ci.

CE QUI EST RÉUTILISÉ (importé de `journeys.translation_manager`, jamais
recopié) : `TranslationEntry`, `send_langs`, `content_hash`, les statuts, et
le crochet de session du worker (`session_factory`) — le harnais de test qui
pointe l'un pointe l'autre.

CE QUI DIFFÈRE, et pourquoi :
- UNE seule entrée traduisible : le CORPS. Le `name` est le libellé de
  rangement de l'agence (le picker de SA liste), jamais envoyé à un client ;
  et il n'existe PAS d'objet sur un modèle de message (décision du 14/08,
  gravée dans le modèle : le sujet du mail est produit par la chrome,
  localisé, au nom de l'agence — une colonne objet ici serait stockée puis
  ignorée à l'envoi).
- la langue SOURCE est la langue par défaut de l'AGENCE, comme les parcours.
  L'étiquette `language` du modèle reste un repère de rangement : elle ne
  pilote rien ici.
- les lignes de job et de mémoire visent `message_template_id` (le CHECK
  « exactement une cible » garantit qu'un job ne peut pas être les deux).

L'INVARIANT DES JETONS ne vit pas ici : il est dans `translation_client`
(prompt + validation au grain champ), donc il protège CHAQUE appelant du
rail. Un {jeton} traduit, renommé, perdu ou dupliqué suit le chemin existant
— repair pass, puis `failed_keys` / done_with_gaps : le champ n'est jamais
publié en silence."""

import logging
import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.ai_translation_job import AiTranslationJob, AiTranslationSource
from shared.models.message_template import MessageTemplate
from src.ai import quota, translation_client
from src.core.enums import ActorType
from src.core.exceptions import ConflictError, NidriaError, NotFoundError, ValidationError
from src.core.i18n import SUPPORTED_LANGUAGES
from src.journeys import translation_manager as journey_rail
from src.journeys.journeys_schema import (
    JobProgress,
    LangTranslationCounts,
    TranslateEstimateResponse,
    TranslationJobResponse,
)
from src.journeys.translation_manager import (
    DONE,
    DONE_WITH_GAPS,
    FAILED,
    PENDING,
    RUNNING,
    TranslationEntry,
    content_hash,
    send_langs,
)
from src.reminders.reminders_repository import RemindersRepository
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

# L'unique clé de contenu d'un modèle : son corps. Stable — c'est elle qui
# indexe la mémoire de hachés.
BODY_KEY = "template.body"


def job_response(job: AiTranslationJob) -> TranslationJobResponse:
    """LE schéma unique des jobs (décision 14/08) : deux cibles
    optionnelles, exactement une remplie — ici `message_template_id`,
    `template_id` restant nul. Le front n'entretient qu'une forme."""
    assert job.message_template_id is not None  # garanti par le CHECK en base
    return TranslationJobResponse(
        id=job.id,
        translation_job_id=job.id,
        template_id=None,
        message_template_id=job.message_template_id,
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


class TemplateTranslationManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = RemindersRepository(db)

    # --- assemblage partagé (miroir de _load_entries côté parcours) ---------------

    async def _load_entry(
        self, agent: Agent, template_id: uuid.UUID, target_langs: list[str] | None
    ) -> tuple[MessageTemplate, TranslationEntry, str, list[str]]:
        template = await self.repo.get_message_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Message template not found.")
        agency = await self.db.get(Agency, agent.agency_id)
        default = (agency.default_language if agency else "fr") or "fr"
        source_lang = default if default in SUPPORTED_LANGUAGES else "fr"

        targets = [lang for lang in (target_langs or SUPPORTED_LANGUAGES) if lang != source_lang]
        if not targets:
            raise ValidationError(
                "No target language to translate into.", code="ai.no_target_language"
            )

        blob = dict(template.body_i18n or {})
        text = (blob.get(source_lang) or template.body or "").strip()
        entry = TranslationEntry(key=BODY_KEY, text=text, blob=blob, obj=template, attr="body")
        entry.needed = [lang for lang in targets if not (blob.get(lang) or "").strip()]
        trail_rows = {
            row.lang: row
            for row in (
                await self.db.execute(
                    select(AiTranslationSource).where(
                        AiTranslationSource.message_template_id == template_id
                    )
                )
            ).scalars()
        }
        src_hash = content_hash(entry.text)
        # STALE = la variante est encore la sortie de l'IA mais la source a
        # bougé. Pas de trace (traduction humaine) ou une variante qui a
        # dérivé de la sortie enregistrée (correction humaine) : jamais.
        entry.stale = [
            lang
            for lang in targets
            if lang not in entry.needed
            and (row := trail_rows.get(lang)) is not None
            and row.source_hash != src_hash
            and row.output_hash == content_hash(blob[lang])
        ]
        return template, entry, source_lang, targets

    # --- l'estimation (le chiffre honnête du front) --------------------------------

    async def estimate(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        target_langs: list[str] | None,
        include_stale: bool = False,
        retranslate_langs: list[str] | None = None,
    ) -> TranslateEstimateResponse:
        _template, entry, _source, targets = await self._load_entry(
            agent, template_id, target_langs
        )
        retranslate = set(retranslate_langs or []) & set(targets)
        langs = send_langs(entry, include_stale, retranslate) if entry.text else []
        used, limit, month = await quota.get_usage(self.db, agent.agency_id)
        # MÊME barème que les parcours (audité sur les runs réels), nourri des
        # caractères RÉELS du corps — un modèle de message coûte naturellement
        # bien moins qu'un parcours, sans compteur ni formule à part.
        estimated = quota.estimate_points(len(entry.text), 1, len(langs)) if langs else 0
        counts = {
            lang: LangTranslationCounts(
                empty=1 if lang in entry.needed else 0,
                stale=1 if lang in entry.stale else 0,
            )
            for lang in targets
        }
        return TranslateEstimateResponse(
            items=1 if langs else 0,
            langs=sorted(langs),
            counts=counts,
            estimated_points=estimated,
            quota_used=used,
            quota_limit=limit,
            month=month,
        )

    # --- démarrage (202) + polling --------------------------------------------------

    async def start_translation(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        target_langs: list[str] | None,
        include_stale: bool = False,
        retranslate_langs: list[str] | None = None,
    ) -> AiTranslationJob:
        """Valide, gate le quota, crée la ligne de RUN — l'appelant planifie
        `execute_job` en tâche de fond et répond 202."""
        template, entry, _source, targets = await self._load_entry(agent, template_id, target_langs)
        retranslate = set(retranslate_langs or []) & set(targets)
        langs = send_langs(entry, include_stale, retranslate) if entry.text else []
        if not langs:
            raise ConflictError(
                "Nothing to translate: every requested variant is already filled.",
                code="ai.nothing_to_translate",
            )
        running = (
            await self.db.execute(
                select(AiTranslationJob).where(
                    AiTranslationJob.message_template_id == template.id,
                    AiTranslationJob.status.in_([PENDING, RUNNING]),
                )
            )
        ).scalar_one_or_none()
        if running is not None:
            raise ConflictError(
                "A translation is already running for this message template.",
                code="ai.translation_already_running",
            )
        await quota.ensure_quota(
            self.db,
            agent.agency_id,
            quota.estimate_points(len(entry.text), 1, len(langs)),
        )
        job = AiTranslationJob(
            agency_id=agent.agency_id,
            message_template_id=template.id,
            status=PENDING,
            langs=sorted(langs),
            progress_done=0,
            progress_total=len(langs),
        )
        self.db.add(job)
        await self.db.commit()
        await self.db.refresh(job)
        return job

    async def get_job(self, agent: Agent, job_id: uuid.UUID) -> TranslationJobResponse:
        """Lecture de polling — scopée agence, et scopée SURFACE : un job de
        parcours interrogé ici répond 404, les deux guichets ne se répondent
        pas l'un pour l'autre."""
        job = (
            await self.db.execute(
                select(AiTranslationJob).where(
                    AiTranslationJob.id == job_id,
                    AiTranslationJob.agency_id == agent.agency_id,
                    AiTranslationJob.message_template_id.is_not(None),
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
    """Worker de fond — session PROPRE (celle de la requête ferme avec la
    réponse), via LE crochet du rail parcours : le harnais de test qui pointe
    `journeys.translation_manager.session_factory` couvre les deux flux.

    Un lot = une langue, la progression avance après chacun ; un échec en
    cours de route garde les langues déjà écrites (et débitées). Chaque
    écriture laisse sa trace de hachés — la mémoire d'obsolescence."""
    retranslate = set(retranslate_langs or [])
    async with journey_rail._sessions()() as db:
        job = await db.get(AiTranslationJob, job_id)
        if job is None or job.message_template_id is None:
            return
        job.status = RUNNING
        await db.commit()
        manager = TemplateTranslationManager(db)
        try:
            for lang in list(job.langs or []):
                _template, entry, source_lang, _targets = await manager._load_entry(
                    agent, job.message_template_id, [lang]
                )
                if lang not in send_langs(entry, include_stale, retranslate) or not entry.text:
                    job.progress_done += 1
                    await db.commit()
                    continue
                # Traçabilité sous-traitant (même règle que les parcours) : le
                # TYPE part en log, jamais le contenu — et le périmètre est un
                # corps de MODÈLE, zéro donnée de dossier par construction.
                kinds = Counter([entry.key.split(".")[0]])
                logger.info(
                    "ai translation -> provider: message_template=%s lang=%s entries=1 types=%s",
                    job.message_template_id,
                    lang,
                    dict(kinds),
                )
                (
                    translations,
                    failed_keys,
                    usages,
                ) = await translation_client.request_translations_with_repair(
                    [{"key": entry.key, "text": entry.text}], source_lang, [lang]
                )
                if entry.key in translations:
                    value = translations[entry.key][lang]
                    entry.obj.body_i18n = {**entry.blob, lang: value}
                    trail = (
                        await db.execute(
                            select(AiTranslationSource).where(
                                AiTranslationSource.message_template_id == job.message_template_id,
                                AiTranslationSource.lang == lang,
                                AiTranslationSource.content_key == entry.key,
                            )
                        )
                    ).scalar_one_or_none()
                    if trail is None:
                        db.add(
                            AiTranslationSource(
                                agency_id=job.agency_id,
                                message_template_id=job.message_template_id,
                                content_key=entry.key,
                                lang=lang,
                                source_hash=content_hash(entry.text),
                                output_hash=content_hash(value),
                            )
                        )
                    else:
                        trail.source_hash = content_hash(entry.text)
                        trail.output_hash = content_hash(value)
                    points = max(1, sum(quota.points_for_usage(u) for u in usages))
                    await quota.debit(db, job.agency_id, points)
                    job.points_charged += points
                    job.translated_keys += 1
                if failed_keys:
                    # Grain CHAMP, comme les parcours : le résidu est exposé,
                    # jamais publié en silence — done_with_gaps, pas failed.
                    job.failed_keys = [
                        *(job.failed_keys or []),
                        *(f"{lang}:{key}" for key in failed_keys),
                    ]
                    logger.warning(
                        "AI template translation job %s: %s body still invalid "
                        "after the repair pass (kept as gap)",
                        job_id,
                        lang,
                    )
                job.progress_done += 1
                await db.commit()
            job.status = DONE_WITH_GAPS if job.failed_keys else DONE
            await UsageManager(db).emit(
                agency_id=job.agency_id,
                event_type="ai.translation_used",
                actor_type=ActorType.AGENT,
                actor_id=agent.id,
                details={
                    "message_template_id": str(job.message_template_id),
                    "langs": list(job.langs or []),
                    "points": job.points_charged,
                },
            )
            await db.commit()
        except NidriaError as exc:
            logger.warning("AI template translation job %s failed: %s", job_id, exc)
            await db.rollback()
            job = await db.get(AiTranslationJob, job_id)
            if job is not None:
                job.status = FAILED
                job.error = exc.code
                await db.commit()
        except Exception:
            logger.exception("AI template translation job %s crashed", job_id)
            await db.rollback()
            job = await db.get(AiTranslationJob, job_id)
            if job is not None:
                job.status = FAILED
                job.error = "ai.translation_failed"
                await db.commit()
