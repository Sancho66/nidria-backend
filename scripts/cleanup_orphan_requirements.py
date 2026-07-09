"""Nettoyage ponctuel des `case_step_requirement` ORPHELINS.

Un orphelin = un requisit concret dont la definition template a ete
supprimee sous l'ANCIEN comportement (hard delete de `step_requirement`
qui laissait l'instance avec `step_requirement_id = NULL` via la FK ON
DELETE SET NULL). Le fix v0.35.0 empeche les NOUVEAUX orphelins
(propagation explicite) ; ce script retire ceux DEJA en base.

SECURITE (invariants de ce script) :
- `--dry-run` est le DEFAUT. `--apply` est requis pour supprimer.
- Supprime UNIQUEMENT des lignes `case_step_requirement`
  WHERE `step_requirement_id IS NULL`. Ne touche JAMAIS `document`,
  `case_person`, ni `case_step_progress`.
- `--exclude-with-document` CONSERVE tout orphelin portant un `document_id`
  non nul : un fichier a reellement ete depose, supprimer la ligne romprait
  son rattachement a l'etape (le fichier survit dans `document`, mais le
  lien etape<->fichier disparait). Regle LISIBLE, pas une liste d'UUID.
- Compte avant, RE-compte juste avant de supprimer, et REFUSE de tourner
  si le compte des cibles a change entre les deux (ecriture concurrente).

Usage :
  python -m scripts.cleanup_orphan_requirements                       # dry-run
  python -m scripts.cleanup_orphan_requirements --exclude-with-document
  python -m scripts.cleanup_orphan_requirements --apply --exclude-with-document
"""

import argparse
import asyncio
from typing import Any

from sqlalchemy import ColumnElement, Row, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.journey import JourneyTemplateStep
from src.core.database import async_session_maker

_ORPHAN = CaseStepRequirement.step_requirement_id.is_(None)


def _target(exclude_with_document: bool) -> ColumnElement[bool]:
    """The rows the script will DELETE. Without the flag: every orphan.
    With it: orphans WITHOUT a deposited file (document_id IS NULL)."""
    if exclude_with_document:
        return _ORPHAN & CaseStepRequirement.document_id.is_(None)
    return _ORPHAN


async def _count(db: AsyncSession, pred: ColumnElement[bool]) -> int:
    return (
        await db.execute(select(func.count()).select_from(CaseStepRequirement).where(pred))
    ).scalar_one()


async def _list(db: AsyncSession) -> list[Row[Any]]:
    stmt = (
        select(
            Agency.name.label("agency"),
            ClientCase.id.label("case_id"),
            ExpatUser.first_name,
            ExpatUser.last_name,
            JourneyTemplateStep.name.label("step"),
            CaseStepRequirement.id.label("csr_id"),
            CaseStepRequirement.kind,
            CaseStepRequirement.reference,
            CaseStepRequirement.status,
            CaseStepRequirement.provided_at,
            CaseStepRequirement.document_id,
        )
        .select_from(CaseStepRequirement)
        .join(CaseStepProgress, CaseStepProgress.id == CaseStepRequirement.case_step_progress_id)
        .join(ClientCase, ClientCase.id == CaseStepProgress.case_id)
        .join(Agency, Agency.id == ClientCase.agency_id)
        .join(ExpatUser, ExpatUser.id == ClientCase.principal_expat_user_id)
        .join(JourneyTemplateStep, JourneyTemplateStep.id == CaseStepProgress.template_step_id)
        .where(_ORPHAN)
        .order_by(Agency.name, ClientCase.id)
    )
    return list((await db.execute(stmt)).all())


async def run(apply: bool, exclude_with_document: bool) -> None:
    target = _target(exclude_with_document)
    async with async_session_maker() as db:
        total = await _count(db, _ORPHAN)
        target_n = await _count(db, target)
        kept_n = total - target_n
        rows = await _list(db)

        print(f"Orphelins (step_requirement_id IS NULL): {total}")
        print(f"Mode: exclude_with_document={exclude_with_document}  apply={apply}")
        print("=" * 100)
        for r in rows:
            keep = exclude_with_document and r.document_id is not None
            tag = "CONSERVE (fichier depose)" if keep else "SUPPR"
            print(
                f"[{tag}] [{r.agency}] case={r.case_id} principal={r.first_name} {r.last_name}\n"
                f"    csr={r.csr_id}  etape={r.step!r}\n"
                f"    {r.kind}:{r.reference!r}  status={r.status}  "
                f"provided_at={r.provided_at}  document_id={r.document_id}"
            )
        print("=" * 100)
        print(f"Cibles de suppression: {target_n} | Conservees (document depose): {kept_n}")

        if not apply:
            print("\nDRY-RUN — aucun DELETE. Relancer avec --apply pour supprimer.")
            return

        # --apply : RE-compte les cibles ; REFUSE si le compte a change.
        recheck = await _count(db, target)
        if recheck != target_n:
            print(
                f"\nABORT : le compte des cibles a change ({target_n} -> {recheck}) entre le "
                f"comptage et la suppression. AUCUN DELETE."
            )
            return
        result = await db.execute(
            delete(CaseStepRequirement).where(target).execution_options(synchronize_session=False)
        )
        deleted = result.rowcount
        after = await _count(db, _ORPHAN)
        if deleted != target_n or after != kept_n:
            await db.rollback()
            print(
                f"\nABORT (rollback) : deleted={deleted} (attendu {target_n}), "
                f"restant={after} (attendu {kept_n}). Aucune modification."
            )
            return
        await db.commit()
        print(
            f"\nAPPLY : {deleted} orphelin(s) supprime(s), {kept_n} conserve(s). "
            f"Orphelins restants: {after}. document / case_person / case_step_progress: INTOUCHES."
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Cleanup orphan case_step_requirement rows.")
    parser.add_argument(
        "--apply", action="store_true", help="Supprime reellement (defaut : dry-run)."
    )
    parser.add_argument(
        "--exclude-with-document",
        action="store_true",
        help="Conserve les orphelins portant un document_id non nul.",
    )
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.exclude_with_document))
