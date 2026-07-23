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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.consent import ConsentDocument
from src.consents.consents_texts import CANONICAL_DOCUMENTS


def content_sha256(content_md: str) -> str:
    return hashlib.sha256(content_md.encode("utf-8")).hexdigest()


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
    content_hash = content_sha256(content)
    rows = list(
        (
            await db.execute(
                select(ConsentDocument).where(
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


async def seed_consent_documents(db: AsyncSession) -> None:
    """Reconcile the stored CANONICAL documents with the texts in code."""
    changed = False
    for doc_type, content in CANONICAL_DOCUMENTS.items():
        if await publish_if_changed(db, doc_type, content):
            changed = True
    if changed:
        await db.commit()
