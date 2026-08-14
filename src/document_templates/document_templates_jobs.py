"""Balayage des brouillons de modèles de document (lot 14/08).

Le builder embeddé du provider ne peut pas s'ouvrir sans un document déjà
matérialisé chez lui : on est donc OBLIGÉ de créer avant que l'agence n'ait
posé la moindre zone. L'état `draft` fait qu'elle ne le voit pas ; ce job
fait qu'il ne reste pas.

Un brouillon de plus de DRAFT_TTL_HOURS que personne n'a promu est un
abandon — l'agence a fermé le builder et n'y est jamais revenue. On emporte
les TROIS supports, comme la suppression manuelle : le template chez le
provider, le PDF dans le storage, la ligne en base.

PRUDENCE VOLONTAIRE : un brouillon RÉFÉRENCÉ n'est pas balayé. Il ne devrait
pas exister (le front délie la ligne d'étape quand le builder se ferme sans
promotion), mais si l'on en trouve un, c'est que quelqu'un l'a délibérément
câblé dans un parcours — et la FK `step_requirement` est en RESTRICT, donc
la suppression échouerait de toute façon. On le laisse et on le NOMME dans
le log : un balayage muet qui saute des lignes est un balayage qui ment.
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.document_template import DocumentTemplate
from shared.models.step_requirement import StepRequirement
from src.core import storage
from src.core.enums import DocumentTemplateState
from src.core.job_wrapper import LogFn
from src.signatures.provider import get_provider

logger = logging.getLogger(__name__)

# 24 h : largement au-delà d'une séance de travail interrompue (déjeuner,
# réunion, fin de journée reprise le lendemain matin), et assez court pour
# qu'un abandon ne dorme pas une semaine chez le provider.
DRAFT_TTL_HOURS = 24


def _archive_provider_templates(refs: list[str]) -> int:
    """Ménage provider, best-effort et groupé — le job tourne dans un thread
    du scheduler, sans boucle : on en ouvre une seule pour tout le lot (même
    patron que le push de sièges de `agencies_jobs`)."""

    async def _archive() -> int:
        done = 0
        provider = get_provider()
        for ref in refs:
            try:
                await provider.archive_template(ref)
                done += 1
            except Exception:
                logger.exception("draft sweep: provider archive failed for template ref %s", ref)
        return done

    return asyncio.run(_archive())


def _is_referenced(db: Session, template_id: uuid.UUID) -> bool:
    """Une définition de parcours OU une ligne matérialisée, quel que soit son
    statut — le balayage est plus prudent que le 409 de suppression, qui ne
    compte que les lignes pendantes."""
    definitions = db.execute(
        select(func.count()).where(StepRequirement.document_template_id == template_id)
    ).scalar_one()
    materialised = db.execute(
        select(func.count()).where(CaseStepRequirement.document_template_id == template_id)
    ).scalar_one()
    return bool(definitions or materialised)


def sweep_document_template_drafts(
    db: Session, *, log: LogFn, dry_run: bool = False
) -> dict[str, Any]:
    """Supprime les brouillons abandonnés (état `draft`, plus vieux que le
    TTL, sans référence). FOR UPDATE SKIP LOCKED : deux ticks qui se
    chevauchent ne balaient jamais la même ligne deux fois."""
    cutoff = datetime.now(UTC) - timedelta(hours=DRAFT_TTL_HOURS)
    rows = list(
        db.execute(
            select(DocumentTemplate)
            .where(
                DocumentTemplate.state == DocumentTemplateState.DRAFT.value,
                DocumentTemplate.created_at <= cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )

    swept = skipped = 0
    provider_refs: list[str] = []
    storage_paths: list[str] = []
    for template in rows:
        if _is_referenced(db, template.id):
            skipped += 1
            log(
                f"brouillon {template.id} ({template.name!r}) RÉFÉRENCÉ : "
                "laissé en place, à examiner"
            )
            continue
        swept += 1
        if dry_run:
            continue
        provider_refs.append(template.provider_template_ref)
        storage_paths.append(template.storage_path)
        db.delete(template)

    archived = purged = 0
    if not dry_run and swept:
        # La ligne d'abord : si le ménage externe échoue, on ne veut pas d'un
        # brouillon ressuscité au tick suivant. Les deux autres supports sont
        # best-effort et journalisés, exactement comme à la suppression
        # manuelle.
        db.commit()
        archived = _archive_provider_templates(provider_refs)
        for path in storage_paths:
            try:
                storage.delete(path)
                purged += 1
            except Exception:
                logger.exception("draft sweep: storage delete failed for %s", path)

    log(
        f"balayé {swept} brouillon(s) de plus de {DRAFT_TTL_HOURS} h "
        f"({archived} archivé(s) chez le provider, {purged} PDF supprimé(s)), "
        f"{skipped} laissé(s) car référencé(s)"
    )
    return {
        "swept": swept,
        "provider_archived": archived,
        "storage_purged": purged,
        "referenced_skipped": skipped,
    }
