"""Harnais signatures (méga-lot 28/07) : FakeProvider (pattern outbox —
enregistre les appels, refs déterministes, zéro réseau) + fixtures flag/
crédits."""

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
)

FAKE_PDF = b"%PDF-1.4 fake signed document"
SOURCE_PDF = b"%PDF-1.4 contrat source agence"
FAKE_AUDIT = b"%PDF-1.4 fake audit log"


class FakeProvider:
    """Implémentation du port pour les tests : chaque appel est enregistré,
    les refs sont déterministes, aucun réseau."""

    def __init__(self) -> None:
        self.create_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self.download_calls: list[str] = []

    async def create(
        self,
        *,
        document_name: str,
        document_pdf: bytes,
        document_filename: str,
        signers: list[ProviderSigner],
        expires_at: datetime | None,
    ) -> CreatedSubmission:
        submission_ref = f"sub_{uuid.uuid4().hex[:10]}"
        self.create_calls.append(
            {
                "document_name": document_name,
                "document_pdf": document_pdf,
                "document_filename": document_filename,
                "signers": signers,
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
