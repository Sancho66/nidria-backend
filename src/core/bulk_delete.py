"""LE MOTEUR DE SUPPRESSION DE MASSE, commun aux deux faces de fiches.

Ce que le moteur garantit, et que ni la face personne ni la face société
ne redécide :

1. **Le compte annoncé EST le compte réel.** Le dry-run et l'exécution
   empruntent le MÊME chemin ; seule la dernière instruction change. Il
   n'y a pas deux façons de compter, donc pas de façon de mentir.
2. **La protection d'abord.** Ce qu'un dossier retient est écarté AVANT
   toute écriture, jamais rattrapé après coup.
3. **Par paquets, une transaction par paquet.** Un filtre peut viser des
   milliers de lignes ; une transaction unique tiendrait un verrou trop
   longtemps et perdrait tout sur l'échec du dernier paquet. Chaque
   paquet est acquis pour de bon — le rapport dit combien.
4. **La trace survit aux fiches.** Une exécution écrit sa ligne de
   journal AVANT de rendre la main : qui, quand, quel critère, combien.

Le moteur ne sait rien des fiches. Il reçoit des identifiants déjà
choisis (le filtre est l'affaire du repo, qui le partage avec la liste)
et l'ensemble de ceux qui sont protégés.
"""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.bulk_deletion import BulkDeletionLog

#: La taille d'un paquet. Assez grand pour que 10 000 fiches partent en
#: 20 transactions, assez petit pour qu'aucune ne tienne un verrou long.
BATCH_SIZE = 500


class BulkDeleteReport(BaseModel):
    """LES CHIFFRES, dans l'ordre où on les lit.

    `matching` — ce que le critère désigne dans l'agence.
    `protected` — ce qu'un dossier retient (vivant, clos ou supprimé).
    `deletable` — `matching − protected` : LE chiffre à annoncer avant le
    geste, et celui que l'exécution honore.
    `deleted` — ce qui est réellement parti (0 en dry-run).
    `dry_run` — pour qu'une réponse ne puisse jamais être prise pour
    l'autre en la relisant plus tard.
    """

    entity: str
    dry_run: bool
    matching: int
    protected: int
    deletable: int
    deleted: int
    #: Les identifiants RETENUS, pour que le front puisse les désigner à
    #: l'écran (« ces 3 fiches restent, elles ont un dossier »). Plafonné :
    #: au-delà, le chiffre parle mieux qu'une liste illisible.
    protected_ids: list[uuid.UUID]

    @property
    def complete(self) -> bool:
        """Vrai quand tout ce qui était supprimable est parti."""
        return self.dry_run or self.deleted == self.deletable


#: Au-delà, `protected_ids` est tronqué (le compte, lui, reste exact).
PROTECTED_IDS_CAP = 100


async def run_bulk_delete(
    db: AsyncSession,
    *,
    agent: Agent,
    entity: str,
    selector: dict[str, Any],
    candidate_ids: list[uuid.UUID],
    protected_ids: set[uuid.UUID],
    delete_batch: Callable[[list[uuid.UUID]], Awaitable[int]],
    dry_run: bool,
) -> BulkDeleteReport:
    """Compte, puis (hors dry-run) supprime par paquets et trace.

    `delete_batch` supprime UN paquet et rend le nombre de lignes
    parties ; il ne commit pas — le moteur commit chaque paquet, c'est
    lui qui tient la règle « une transaction par paquet ».
    """
    deletable_ids = [pid for pid in candidate_ids if pid not in protected_ids]
    report_base: dict[str, Any] = {
        "entity": entity,
        "matching": len(candidate_ids),
        "protected": len(protected_ids),
        "deletable": len(deletable_ids),
        "protected_ids": sorted(protected_ids)[:PROTECTED_IDS_CAP],
    }
    if dry_run:
        # Rien n'est écrit — pas même la trace : un compte n'est pas un
        # geste. Le chemin de comptage ci-dessus est celui de l'exécution.
        return BulkDeleteReport(dry_run=True, deleted=0, **report_base)

    deleted = 0
    for start in range(0, len(deletable_ids), BATCH_SIZE):
        batch = deletable_ids[start : start + BATCH_SIZE]
        deleted += await delete_batch(batch)
        # Une transaction par paquet : ce qui est parti est acquis, même
        # si le paquet suivant échoue.
        await db.commit()

    db.add(
        BulkDeletionLog(
            agency_id=agent.agency_id,
            entity=entity,
            performed_by_agent_id=agent.id,
            performed_by_email=agent.email,
            selector=selector,
            matching=len(candidate_ids),
            protected=len(protected_ids),
            deletable=len(deletable_ids),
            deleted=deleted,
        )
    )
    await db.commit()
    return BulkDeleteReport(dry_run=False, deleted=deleted, **report_base)
