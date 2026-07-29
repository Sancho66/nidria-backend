"""SignatureProvider — LE port (méga-lot 28/07). Le domaine parle en
identifiants à lui (signataires = signature_signer.id via external_id) et
en refs OPAQUES retournées par le provider ; aucun détail DocuSeal ne
franchit cette frontière.

Contrat du port (3 lots + méga-lot modèles 29/07) :
- `create_template` : matérialise chez le provider le template d'un modèle
  de la bibliothèque (octets du PDF de l'agence, external_id = NOTRE UUID
  — la clé de liaison du builder embeddé, constat sonde). Zéro champ : les
  zones sont posées par l'agence dans le builder.
- `template_summary` : constat post-save builder (nb de champs, rôles) —
  alimente fields_configured/roles_count du modèle.
- `archive_template` : ménage best-effort à la suppression du modèle.
- `create_from_template` : envoie UN document à signer à N signataires
  (ordre PARALLÈLE, mapping par NOM de rôle, expiration posée, AUCUN email
  provider — les emails sont au système de notifications v4). Retourne les
  refs opaques + le slug d'embed par signataire.
- `cancel`  : annule une demande vivante (release du crédit côté domaine).
- `download_completed` : récupère IMMÉDIATEMENT le PDF signé + le dossier
  de preuve d'une demande complétée — leurs URLs expirent, on ne stocke
  JAMAIS une URL, toujours les octets.

Résolution : `get_provider()` lit la config (provider docuseal, lot 3).
Les tests posent un FakeProvider via `override` (pattern outbox/mock)."""

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from src.core.config import get_settings
from src.core.exceptions import UpstreamError

logger = logging.getLogger(__name__)


@dataclass
class ProviderSigner:
    """What the port needs to seat one signer — domain ids only."""

    signer_id: str  # signature_signer.id, posé en external_id chez le provider
    name: str
    email: str | None
    # Convention sonde : "Signataire N" (1-based, ordre de matérialisation)
    # — DOIT exister sur le template (le provider ne valide PAS un rôle
    # inconnu : il assoit un signataire fantôme sans zones — garde chez nous).
    role: str = ""


@dataclass
class CreatedSigner:
    signer_id: str  # notre id, renvoyé tel quel
    provider_ref: str  # ref opaque par signataire
    slug: str  # slug d'embed servi à l'espace client


@dataclass
class CreatedSubmission:
    provider_ref: str  # ref opaque de la demande
    signers: list[CreatedSigner] = field(default_factory=list)


@dataclass
class TemplateSummary:
    """Constat du template provider après save builder."""

    fields_count: int
    roles: list[str] = field(default_factory=list)


@dataclass
class CompletedFiles:
    """Les OCTETS du résultat — jamais d'URL (elles expirent)."""

    document_pdf: bytes
    document_filename: str
    audit_pdf: bytes | None
    audit_filename: str | None


class SignatureProvider(Protocol):
    async def create_template(
        self, *, name: str, pdf: bytes, filename: str, external_id: str
    ) -> str: ...

    async def template_summary(self, template_ref: str) -> TemplateSummary: ...

    async def archive_template(self, template_ref: str) -> None: ...

    async def create_from_template(
        self,
        *,
        template_ref: str,
        signers: list[ProviderSigner],
        expires_at: datetime | None,
    ) -> CreatedSubmission: ...

    async def cancel(self, provider_ref: str) -> None: ...

    async def download_completed(self, provider_ref: str) -> CompletedFiles: ...


# Test seam (pattern outbox / translation_manager.session_factory) : les
# tests posent leur FakeProvider ici ; le runtime résout par la config.
override: SignatureProvider | None = None


def get_provider() -> SignatureProvider:
    if override is not None:
        return override
    settings = get_settings()
    if not settings.docuseal_api_key:
        # Flag on mais provider non configuré : erreur claire au moment de
        # l'ENVOI (jamais au boot) — aucun appel réseau n'a été tenté.
        raise UpstreamError(
            "No signature provider configured (DOCUSEAL_API_KEY missing).",
            code="signatures.provider_unconfigured",
        )
    from src.signatures.docuseal import DocuSealProvider

    return DocuSealProvider()
