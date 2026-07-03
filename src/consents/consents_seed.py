"""Consent documents seed (point 16): version 1 of each type, placeholder
texts clearly marked as provisional. Seeded at boot ONLY where the type has
no row at all: a runtime publication (new version via script) is never
overwritten, same rule as the system roles and job configs.

The client-facing texts carry the {agency_name} token, resolved at READ
time with the responsible agency's name (the hash covers the RAW text)."""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.consent import ConsentDocument
from src.core.enums import ConsentDocumentType

_AGENCY_TERMS_V1 = """# Conditions generales de vente Nidria

[TEXTE PROVISOIRE, a remplacer]

1. Objet : Nidria fournit a l'agence un service en ligne de gestion et de
   suivi des dossiers de mobilite internationale.
2. Acces : le service est accessible par abonnement ; les identifiants des
   membres de l'agence sont personnels.
3. Responsabilites : l'agence reste seule responsable du contenu des
   dossiers et de ses obligations envers ses clients.
4. Disponibilite : Nidria s'efforce d'assurer une disponibilite continue du
   service, sans garantie absolue.
5. Resiliation : les conditions de resiliation et de restitution des
   donnees seront precisees dans la version definitive de ce document.
"""

_AGENCY_DPA_V1 = """# Accord de traitement des donnees (DPA)

[TEXTE PROVISOIRE, a remplacer]

1. Roles : l'agence est responsable de traitement des donnees de ses
   clients ; Nidria agit en qualite de sous-traitant au sens du RGPD.
2. Traitements : hebergement, stockage et mise a disposition des donnees
   des dossiers, uniquement sur instruction de l'agence.
3. Hebergement : les donnees sont hebergees dans l'Union europeenne.
4. Securite : Nidria met en oeuvre des mesures techniques et
   organisationnelles appropriees (chiffrement en transit, controle
   d'acces, journalisation).
5. Sous-traitance ulterieure et transferts : detailles dans la version
   definitive de ce document.
"""

_CLIENT_TERMS_V1 = """# Conditions generales d'utilisation de l'espace client

[TEXTE PROVISOIRE, a remplacer]

1. Objet : cet espace vous permet de suivre l'avancement de votre dossier
   aupres de {agency_name} et de transmettre les informations et documents
   demandes.
2. Compte : vos identifiants sont personnels ; vous etes responsable de
   leur confidentialite.
3. Usage : les informations et documents que vous deposez doivent etre
   exacts et concerner votre dossier.
4. L'agence {agency_name} reste votre interlocuteur pour toute question
   relative a votre dossier.
"""

_CLIENT_PRIVACY_V1 = """# Note de confidentialite

[TEXTE PROVISOIRE, a remplacer]

1. Responsable de traitement : l'agence {agency_name} est responsable du
   traitement de vos donnees personnelles dans le cadre de votre dossier.
2. Sous-traitant : Nidria heberge et traite ces donnees pour le compte de
   {agency_name}, en qualite de sous-traitant au sens du RGPD.
3. Hebergement : vos donnees sont hebergees dans l'Union europeenne.
4. Vos droits : vous disposez des droits d'acces, de rectification,
   d'effacement, de limitation et d'opposition. Pour les exercer,
   adressez-vous a {agency_name}.
5. Duree de conservation et contacts : precises dans la version definitive
   de ce document.
"""

PLACEHOLDER_DOCUMENTS: dict[str, str] = {
    ConsentDocumentType.AGENCY_TERMS.value: _AGENCY_TERMS_V1,
    ConsentDocumentType.AGENCY_DPA.value: _AGENCY_DPA_V1,
    ConsentDocumentType.CLIENT_TERMS.value: _CLIENT_TERMS_V1,
    ConsentDocumentType.CLIENT_PRIVACY.value: _CLIENT_PRIVACY_V1,
}


def content_sha256(content_md: str) -> str:
    return hashlib.sha256(content_md.encode("utf-8")).hexdigest()


async def seed_consent_documents(db: AsyncSession) -> None:
    """Insert version 1 of every type that has NO document yet (any
    version, active or not). Idempotent; never touches existing rows."""
    existing = set((await db.execute(select(ConsentDocument.type).distinct())).scalars())
    now = datetime.now(UTC)
    for doc_type, content in PLACEHOLDER_DOCUMENTS.items():
        if doc_type in existing:
            continue
        db.add(
            ConsentDocument(
                type=doc_type,
                version=1,
                content_md=content,
                content_hash=content_sha256(content),
                published_at=now,
                is_active=True,
            )
        )
    await db.commit()
