"""Note interne d'agence sur une FICHE client (complément sections).

MIROIR STRICT de `case_note` : mêmes colonnes, même règle de
confidentialité (visible seulement avec la permission dédiée — contrôle
d'accès sur une vraie colonne), même règle d'auteur (seul l'auteur
modifie/supprime). Les notes de dossier ne bougent pas."""

import uuid

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ClientProfileNote(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "client_profile_note"

    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("client_profile.id", ondelete="CASCADE"), index=True, nullable=False
    )
    author_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("agent.id", ondelete="SET NULL")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_confidential: Mapped[bool] = mapped_column(default=False, nullable=False)
