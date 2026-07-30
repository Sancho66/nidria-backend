"""Bibliothèque de modèles de documents à signer (méga-lot modèles 29/07).

Le cycle constaté à la sonde : le modèle naît CHEZ NOUS (PDF stocké) et
CHEZ le provider dans la même transaction (template sans champs,
external_id = notre UUID — la clé find-or-create du builder embeddé, le
claim template_id du JWT étant ignoré). L'agence pose ses zones dans le
builder (jeton court signé avec la clé API) ; le front, sur l'événement
save du composant, appelle builder-sync qui CONSTATE l'état provider
(fields_configured, roles_count) — les gardes d'envoi lisent ce constat,
jamais le provider en direct.

Limite constatée (sonde) : DocuSeal ne snapshotte PAS le template à la
soumission — une édition de zones affecte les demandes EN VOL. Nommée au
rapport ; la suppression, elle, est bien refusée tant qu'une définition ou
une ligne pendante référence le modèle (409 nommé, liste des références).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

from jose import jwt
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.document_template import DocumentTemplate
from src.core import storage
from src.core.config import get_settings
from src.core.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UpstreamError,
    ValidationError,
)
from src.document_templates.document_templates_repository import DocumentTemplatesRepository
from src.signatures.flags import signatures_effectively_enabled
from src.signatures.provider import AGENCY_ROLE, get_provider

logger = logging.getLogger(__name__)


class DocumentTemplatesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = DocumentTemplatesRepository(db)

    async def _guard_enabled(self, agent: Agent) -> None:
        agency = await self.db.get(Agency, agent.agency_id)
        if not signatures_effectively_enabled(agency):
            raise ConflictError(
                "E-signatures are not enabled for this agency.",
                code="signatures.disabled",
            )

    async def _get(self, agent: Agent, template_id: uuid.UUID) -> DocumentTemplate:
        template = await self.repo.get_for_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Document template not found.", code="document_template.not_found")
        return template

    async def list(self, agent: Agent) -> list[DocumentTemplate]:
        await self._guard_enabled(agent)
        return await self.repo.list_for_agency(agent.agency_id)

    async def get(self, agent: Agent, template_id: uuid.UUID) -> DocumentTemplate:
        await self._guard_enabled(agent)
        return await self._get(agent, template_id)

    async def create(
        self,
        agent: Agent,
        *,
        name: str,
        filename: str,
        content: bytes,
        content_type: str | None,
        agency_countersigns: bool = False,
    ) -> DocumentTemplate:
        """PDF stocké chez nous + template provider (sans champs) dans la
        même transaction — un échec provider annule tout (et nettoie le
        fichier uploadé, best-effort)."""
        await self._guard_enabled(agent)
        if not filename.lower().endswith(".pdf") or (
            content_type is not None and content_type not in ("application/pdf",)
        ):
            raise ValidationError(
                "The document template must be a PDF.",
                code="document_template.not_pdf",
            )
        settings = get_settings()
        max_bytes = settings.max_document_size_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise PayloadTooLargeError(
                f"File exceeds the {settings.max_document_size_mb} MB limit."
            )
        template_id = uuid.uuid4()
        path = (
            f"document-templates/{agent.agency_id}/{template_id}/"
            f"{storage.sanitize_filename(filename)}"
        )
        await asyncio.to_thread(storage.upload, path, content, "application/pdf")
        try:
            provider_ref = await get_provider().create_template(
                name=name, pdf=content, filename=filename, external_id=str(template_id)
            )
        except Exception:
            try:
                await asyncio.to_thread(storage.delete, path)
            except Exception:  # noqa: BLE001 — le nettoyage ne masque jamais l'erreur
                logger.warning("orphan template file left behind: %s", path)
            raise
        template = DocumentTemplate(
            id=template_id,
            agency_id=agent.agency_id,
            name=name,
            storage_path=path,
            filename=filename,
            provider_template_ref=provider_ref,
            agency_countersigns=agency_countersigns,
        )
        self.db.add(template)
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def rename(
        self,
        agent: Agent,
        template_id: uuid.UUID,
        name: str | None,
        agency_countersigns: bool | None = None,
    ) -> DocumentTemplate:
        await self._guard_enabled(agent)
        template = await self._get(agent, template_id)
        if name is not None:
            template.name = name
        if agency_countersigns is not None and agency_countersigns != template.agency_countersigns:
            # Basculer le contreseing change ce que « configuré » veut
            # dire : on re-constate l'état provider dans la foulée (le
            # verrou exige — ou n'exige plus — la zone du rôle Agence).
            template.agency_countersigns = agency_countersigns
            summary = await get_provider().template_summary(template.provider_template_ref)
            client_roles = [r for r in summary.roles if r != AGENCY_ROLE]
            clients_ok = bool(client_roles) and all(
                role in summary.roles_with_signature for role in client_roles
            )
            agency_ok = (not agency_countersigns) or (
                AGENCY_ROLE in summary.roles and AGENCY_ROLE in summary.roles_with_signature
            )
            template.fields_configured = clients_ok and agency_ok
            template.roles_count = len(client_roles)
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def builder_token(self, agent: Agent, template_id: uuid.UUID) -> str:
        """JWT court (HS256, signé avec la clé API provider) scoped au
        modèle via external_id — la sonde a constaté que le claim
        template_id est ignoré, external_id est LA liaison."""
        await self._guard_enabled(agent)
        template = await self._get(agent, template_id)
        settings = get_settings()
        if not settings.docuseal_api_key or not settings.docuseal_account_email:
            raise UpstreamError(
                "Signature provider is not configured for the embedded builder.",
                code="signatures.provider_unconfigured",
            )
        return jwt.encode(
            {
                "user_email": settings.docuseal_account_email,
                "external_id": str(template.id),
                "exp": datetime.now(UTC)
                + timedelta(minutes=settings.docuseal_builder_token_expires_minutes),
            },
            settings.docuseal_api_key,
            algorithm="HS256",
        )

    async def builder_sync(self, agent: Agent, template_id: uuid.UUID) -> DocumentTemplate:
        """Appelé par le front après le save du builder : constate l'état
        provider et le matérialise (fields_configured, roles_count) — les
        gardes d'envoi lisent CE constat."""
        await self._guard_enabled(agent)
        template = await self._get(agent, template_id)
        summary = await get_provider().template_summary(template.provider_template_ref)
        # Verrou par rôle (mini-lot 30/07) : configuré = CHAQUE rôle porte
        # sa zone signature — un rôle sans zone est un signataire qui
        # n'aurait rien à signer (le provider ne le refuse pas, constat).
        # Contreseing (lot 30/07) : le rôle « Agence » est compté À PART —
        # roles_count reste le compte des rôles CLIENTS (toutes les gardes
        # rôles == signataires restent vraies telles quelles), et le verrou
        # exige EN PLUS la zone du rôle Agence quand agency_countersigns.
        client_roles = [r for r in summary.roles if r != AGENCY_ROLE]
        clients_ok = bool(client_roles) and all(
            role in summary.roles_with_signature for role in client_roles
        )
        agency_ok = (not template.agency_countersigns) or (
            AGENCY_ROLE in summary.roles and AGENCY_ROLE in summary.roles_with_signature
        )
        template.fields_configured = clients_ok and agency_ok
        template.roles_count = len(client_roles)
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def delete(self, agent: Agent, template_id: uuid.UUID) -> None:
        """Refusée (409 nommé, références listées) tant qu'une définition de
        parcours OU une ligne matérialisée pendante pointe le modèle."""
        await self._guard_enabled(agent)
        template = await self._get(agent, template_id)
        references = await self.repo.definition_references(template.id)
        pending = await self.repo.pending_row_count(template.id)
        if references or pending:
            raise ConflictError(
                "This document template is referenced by signable requirements.",
                code="document_template.in_use",
                params={"references": references, "pending_rows": pending},
            )
        try:
            await get_provider().archive_template(template.provider_template_ref)
        except Exception:  # noqa: BLE001 — le ménage provider est best-effort
            logger.warning("provider archive failed for template %s", template.id)
        try:
            await asyncio.to_thread(storage.delete, template.storage_path)
        except Exception:  # noqa: BLE001
            logger.warning("storage delete failed for template %s", template.id)
        await self.db.delete(template)
        await self.db.commit()
