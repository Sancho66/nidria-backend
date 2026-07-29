"""Bibliothèque de MODÈLES de documents à signer, par agence (méga-lot
modèles 29/07).

Le PDF source vit CHEZ NOUS (storage) ; le provider reçoit les octets à la
création et matérialise son propre template (`provider_template_ref`,
opaque). Les zones de signature sont posées par l'AGENCE dans le builder
embeddé du provider (jeton JWT court scoped au modèle) — plus jamais de
coordonnées fixes en code. `fields_configured` + `roles_count` sont le
constat du dernier sync post-save builder : on n'envoie JAMAIS un modèle
sans zones, ni plus de signataires que de rôles configurés (constat sonde :
DocuSeal accepte un rôle inconnu sans broncher — la garde est chez nous)."""

import uuid

from sqlalchemy import Boolean, ForeignKey, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column

from shared.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class DocumentTemplate(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "document_template"

    agency_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agency.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Le PDF source, stocké chez nous — le provider n'est jamais la vérité.
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(20), default="docuseal", server_default="docuseal", nullable=False
    )
    # Ref opaque du template côté provider (liaison par external_id = notre
    # UUID, constat sonde : le claim template_id du JWT builder est ignoré).
    provider_template_ref: Mapped[str] = mapped_column(String(100), nullable=False)
    fields_configured: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false"), nullable=False
    )
    roles_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=text("0"), nullable=False
    )
