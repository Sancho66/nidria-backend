"""Harnais signatures (méga-lot 28/07, port modèles 29/07) : FakeProvider
(pattern outbox — enregistre les appels, refs déterministes, zéro réseau)
+ fixtures flag/crédits."""

import uuid
from collections.abc import AsyncGenerator, Generator
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import get_settings
from src.signatures import provider as provider_module
from src.signatures.provider import (
    CompletedFiles,
    CreatedSigner,
    CreatedSubmission,
    ProviderSigner,
    TemplateSummary,
)

FAKE_PDF = b"%PDF-1.4 fake signed document"
SOURCE_PDF = b"%PDF-1.4 contrat source agence"
FAKE_AUDIT = b"%PDF-1.4 fake audit log"


class FakeProvider:
    """Implémentation du port pour les tests : chaque appel est enregistré,
    les refs sont déterministes, aucun réseau. Les templates « provider »
    vivent dans `templates` (ref → dict pdf/roles) ; `default_roles` pilote
    ce que template_summary constate (le save builder simulé)."""

    def __init__(self) -> None:
        self.templates: dict[str, dict] = {}
        self.create_template_calls: list[dict] = []
        self.summary_calls: list[str] = []
        self.archive_calls: list[str] = []
        self.create_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.download_calls: list[str] = []
        # Le « save builder » simulé : les rôles que template_summary
        # constatera (2 = principal + membre, le cas nominal des batteries).
        self.default_roles = ["Signataire 1", "Signataire 2"]

    async def create_template(
        self, *, name: str, pdf: bytes, filename: str, external_id: str
    ) -> str:
        ref = f"tpl_{len(self.templates) + 1}"
        self.templates[ref] = {
            "name": name,
            "pdf": pdf,
            "filename": filename,
            "external_id": external_id,
        }
        self.create_template_calls.append({"ref": ref, "name": name, "external_id": external_id})
        return ref

    async def template_summary(self, template_ref: str) -> TemplateSummary:
        self.summary_calls.append(template_ref)
        roles = self.templates.get(template_ref, {}).get("roles", self.default_roles)
        return TemplateSummary(fields_count=len(roles), roles=list(roles))

    async def archive_template(self, template_ref: str) -> None:
        self.archive_calls.append(template_ref)

    async def create_from_template(
        self,
        *,
        template_ref: str,
        signers: list[ProviderSigner],
        expires_at: datetime | None,
    ) -> CreatedSubmission:
        submission_ref = f"sub_{uuid.uuid4().hex[:10]}"
        self.create_calls.append(
            {
                "template_ref": template_ref,
                "signers": signers,
                "roles": [s.role for s in signers],
                "expires_at": expires_at,
                "provider_ref": submission_ref,
            }
        )
        return CreatedSubmission(
            provider_ref=submission_ref,
            signers=[
                CreatedSigner(
                    signer_id=s.signer_id,
                    provider_ref=f"sm_{i}_{submission_ref}",
                    slug=f"slug-{s.signer_id[:8]}",
                )
                for i, s in enumerate(signers)
            ],
        )

    async def cancel(self, provider_ref: str) -> None:
        self.cancel_calls.append(provider_ref)

    async def download_completed(self, provider_ref: str) -> CompletedFiles:
        self.download_calls.append(provider_ref)
        return CompletedFiles(
            document_pdf=FAKE_PDF,
            document_filename="signed-document.pdf",
            audit_pdf=FAKE_AUDIT,
            audit_filename="audit-log.pdf",
        )


@pytest.fixture
def fake_provider() -> Generator[FakeProvider, None, None]:
    fake = FakeProvider()
    provider_module.override = fake
    yield fake
    provider_module.override = None


@pytest.fixture
def signatures_enabled(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setenv("SIGNATURES_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def give_credits(db_session: AsyncSession) -> AsyncGenerator:
    """give_credits(agency_id, n) — un achat direct au ledger (le webhook
    Paddle a son propre test)."""
    from src.signatures.ledger import purchase_credits

    async def _give(agency_id: uuid.UUID, credits: int) -> None:
        await purchase_credits(
            db_session,
            agency_id,
            credits,
            paddle_event_id=f"txn_test_{uuid.uuid4().hex[:10]}",
        )
        await db_session.commit()

    yield _give
