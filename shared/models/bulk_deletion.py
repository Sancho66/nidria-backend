"""LA TRACE D'UNE SUPPRESSION DE MASSE de fiches.

Le problème que cette table ferme : `activity_log` est CASE-SCOPÉ
(`case_id` non nullable). Une fiche sans dossier n'y laisse rien — et
une fiche sans dossier est précisément la seule qu'on ait le droit de
supprimer. Une suppression de masse ne laissait donc AUCUNE trace :
seulement l'absence, constatable des semaines plus tard sans pouvoir
dire qui, quand, ni sur quel critère.

Ici vit la trace, et elle survit à ce qu'elle raconte : qui, quand, sur
quel critère, combien. Les fiches disparaissent, le geste reste.

Même grammaire d'audit qu'`AgencyDeletionLog` (le précédent maison) :
INSERT-ONLY (pas de `TimestampMixin` — un audit ne se met pas à jour),
et l'acteur est capturé VERBATIM (UUID nu + email) plutôt que par une
FK : un agent qui quitte l'agence ne doit pas emporter le nom de celui
qui a fait le geste. L'`agency_id`, lui, garde sa FK — l'agence, elle,
est toujours là (on supprime ses fiches, pas elle).

Ce qui n'est PAS gardé : la liste des identifiants supprimés. Un filtre
peut viser des milliers de lignes, et conserver leurs identifiants
reviendrait à garder une ombre de ce que l'agence a demandé d'effacer —
le critère raconte le geste, la liste ne ferait que survivre au geste.
Un dry-run n'écrit rien non plus : il ne supprime rien.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, UUIDPrimaryKeyMixin

#: Les deux faces de fiches. Le dossier a son propre journal (case-scopé).
BULK_DELETION_ENTITIES: tuple[str, ...] = ("client_profile", "company_profile")


class BulkDeletionLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "bulk_deletion_log"
    __table_args__ = (
        # La lecture naturelle : le journal d'une agence, du plus récent.
        Index("ix_bulk_deletion_log_agency_created", "agency_id", "created_at"),
    )

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: client_profile | company_profile
    entity: Mapped[str] = mapped_column(String(30), nullable=False)

    # QUI — figé sur place, sans FK (cf. AgencyDeletionLog).
    performed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column()
    performed_by_email: Mapped[str] = mapped_column(String(255), nullable=False)

    #: SUR QUEL CRITÈRE. `{"mode": "filter", "filter": {...}}` — les
    #: paramètres exacts de la liste, ceux que l'agence avait à l'écran —
    #: ou `{"mode": "ids", "count": n}` pour une sélection à la main.
    selector: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    #: COMBIEN. `matching` = ce que le critère désigne ; `protected` = ce
    #: qu'un dossier retient (l'historique est sacré) ; `deletable` =
    #: matching − protected, le chiffre annoncé AVANT le geste ; `deleted`
    #: = ce qui est réellement parti. deletable ≠ deleted se lit comme un
    #: incident, jamais comme une nuance.
    matching: Mapped[int] = mapped_column(Integer, nullable=False)
    protected: Mapped[int] = mapped_column(Integer, nullable=False)
    deletable: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted: Mapped[int] = mapped_column(Integer, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
