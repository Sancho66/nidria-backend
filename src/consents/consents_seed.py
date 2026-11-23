"""Consent documents seed/reconcile (point 16). The canonical texts live
in consents_texts.py (CODE = source of truth, same rule as the permission
catalogue): editing a text there and deploying IS the publication act.

For each type: no document at all → insert version 1; otherwise, if the
latest ACTIVE version's hash differs from the canonical text → publish
version max+1 and deactivate the previous actives, which re-gates every
concerned actor at their next request (passation A2.8: any text change is
a NEW version; a version that was accepted is never modified, so the
acceptance trace and its content_hash stay evidentiary). Idempotent:
identical text → zero write."""

import hashlib
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.consent import ConsentDocument
from src.consents.consents_texts import CANONICAL_DOCUMENTS

if TYPE_CHECKING:
    from shared.models.agency import Agency


def content_sha256(content_md: str) -> str:
    return hashlib.sha256(content_md.encode("utf-8")).hexdigest()


async def lock_document(db: AsyncSession, doc_type: str, agency_id: uuid.UUID | None) -> None:
    """Serialize publication, including when this owner's first version does not yet exist."""
    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"consent:{agency_id}:{doc_type}"},
    )


async def publish_if_changed(
    db: AsyncSession, doc_type: str, content: str, agency_id: uuid.UUID | None = None
) -> bool:
    """Publish a NEW version of (doc_type, agency_id) when `content` differs
    from the latest active one; no-op if identical. Returns True on publish.

    THE single publication act, shared by the canonical seed (agency_id
    None) and by an agency writing its own text — so an agency's document
    gets the exact same versioning, hash and automatic re-gating as
    Nidria's, and neither path can drift from the other. Does NOT commit:
    the caller owns the transaction. Versions are numbered per owner, and
    a published version is never modified — the acceptance trace and its
    content_hash stay evidentiary."""
    await lock_document(db, doc_type, agency_id)
    content_hash = content_sha256(content)
    rows = list(
        (
            await db.execute(
                select(ConsentDocument)
                .execution_options(populate_existing=True)
                .where(
                    ConsentDocument.type == doc_type,
                    (
                        ConsentDocument.agency_id.is_(None)
                        if agency_id is None
                        else ConsentDocument.agency_id == agency_id
                    ),
                )
            )
        ).scalars()
    )
    actives = [row for row in rows if row.is_active]
    latest_active = max(actives, key=lambda row: row.version, default=None)
    if latest_active is not None and latest_active.content_hash == content_hash:
        return False
    for row in actives:
        row.is_active = False
    db.add(
        ConsentDocument(
            type=doc_type,
            version=max((row.version for row in rows), default=0) + 1,
            content_md=content,
            content_hash=content_hash,
            published_at=datetime.now(UTC),
            is_active=True,
            agency_id=agency_id,
        )
    )
    return True


async def withdraw_agency_document(db: AsyncSession, doc_type: str, agency_id: uuid.UUID) -> bool:
    """Deactivate an agency's own version of a document: its clients fall
    back to the canonical Nidria text at their next request. The rows are
    kept (never deleted) — past acceptances point at them and must stay
    resolvable. Does NOT commit."""
    await lock_document(db, doc_type, agency_id)
    actives = [
        row
        for row in (
            await db.execute(
                select(ConsentDocument).where(
                    ConsentDocument.type == doc_type,
                    ConsentDocument.agency_id == agency_id,
                    ConsentDocument.is_active.is_(True),
                )
            )
        ).scalars()
    ]
    for row in actives:
        row.is_active = False
    return bool(actives)


async def _own_active_doc(
    db: AsyncSession, doc_type: str, agency_id: uuid.UUID
) -> ConsentDocument | None:
    return (
        await db.execute(
            select(ConsentDocument).where(
                ConsentDocument.type == doc_type,
                ConsentDocument.agency_id == agency_id,
                ConsentDocument.is_active.is_(True),
            )
        )
    ).scalar_one_or_none()


# The immutable French templates from the August publications. Mask just the
# identity that was interpolated; a single customised clause changes the digest.
_LEGACY_MODELS = {
    "client_terms": (
        (
            "Cet espace est édité par ",
            ".\n\n1.",
            "525978836549cd94067334ac45146e9ee13ed5b4662f3c20e1d494a7be4f10d4",
        ),
        (
            "Éditeur de l'espace : ",
            "\n\n1.",
            "d1e791f493fcc1380e806b37fce675af919fa5b326f71af301bcd2ddc669c700",
        ),
    ),
    "client_privacy": (
        (
            "le cadre de votre dossier, est ",
            ".\n2.",
            "e8ec580c75cf3d09def7661cd98aa418c61e02fece8aa091bcde2e9b85b432e2",
        ),
        (
            "{agency_name} \u2014 ",
            " \u2014 est responsable",
            "eb85e6435ac9821f130f70b615202a7b55e535123f63b4bfadd450da5d38d4aa",
        ),
    ),
}
_OLD_CANONICAL = {
    "client_terms": "6b98577327ff51f7a3cfadf97ef546b6d00cf3c5584a9be2d32f18d19545c034",
    "client_privacy": "322b2e4ff887158e6336074a281023000375e94b9a59bff12e793d6bbe3287fb",
}


def is_legacy_client_model(doc_type: str, content: str) -> bool:
    """Match previously shipped bodies outside the one interpolated profile segment."""
    if content_sha256(content) == _OLD_CANONICAL[doc_type]:
        return True
    for prefix, suffix, expected in _LEGACY_MODELS[doc_type]:
        left, start, rest = content.partition(prefix)
        _, end, right = rest.partition(suffix)
        if start and end and content_sha256(left + start + "IDENTITY" + end + right) == expected:
            return True
    return False


async def ensure_agency_default_terms(db: AsyncSession, agency: "Agency") -> bool:
    """Install missing defaults and replace only known, previously shipped client templates."""
    from src.consents.agency_template import generate_client_privacy, generate_client_terms
    from src.core.enums import ConsentDocumentType

    changed = False
    for doc_type, generator in (
        (ConsentDocumentType.CLIENT_TERMS.value, generate_client_terms),
        (ConsentDocumentType.CLIENT_PRIVACY.value, generate_client_privacy),
    ):
        await lock_document(db, doc_type, agency.id)
        existing = await _own_active_doc(db, doc_type, agency.id)
        if existing is not None and not is_legacy_client_model(doc_type, existing.content_md):
            continue
        if await publish_if_changed(db, doc_type, generator(agency), agency_id=agency.id):
            changed = True
    return changed


async def seed_consent_documents(db: AsyncSession) -> None:
    """Reconcile the stored CANONICAL documents with the texts in code."""
    changed = False
    for doc_type, content in CANONICAL_DOCUMENTS.items():
        if await publish_if_changed(db, doc_type, content):
            changed = True
    if changed:
        await db.commit()


async def seed_agency_default_terms(db: AsyncSession) -> None:
    """At boot, give EVERY agency its own client documents (lot 13/08) — the
    Nidria fallback dies for existing agencies too. Idempotent: an agency
    that already has its own docs is skipped; a freshly-generated one is
    published once (its clients re-consent on the agency-named text). Runs
    AFTER the canonical seed."""
    from shared.models.agency import Agency

    agencies = (await db.execute(select(Agency).order_by(Agency.id))).scalars().all()
    changed = False
    for agency in agencies:
        if await ensure_agency_default_terms(db, agency):
            changed = True
    if changed:
        await db.commit()
