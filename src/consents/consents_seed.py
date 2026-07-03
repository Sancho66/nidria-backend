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
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.consent import ConsentDocument
from src.consents.consents_texts import CANONICAL_DOCUMENTS


def content_sha256(content_md: str) -> str:
    return hashlib.sha256(content_md.encode("utf-8")).hexdigest()


async def seed_consent_documents(db: AsyncSession) -> None:
    """Reconcile the stored documents with the canonical texts."""
    changed = False
    for doc_type, content in CANONICAL_DOCUMENTS.items():
        canonical_hash = content_sha256(content)
        rows = list(
            (
                await db.execute(select(ConsentDocument).where(ConsentDocument.type == doc_type))
            ).scalars()
        )
        actives = [row for row in rows if row.is_active]
        latest_active = max(actives, key=lambda row: row.version, default=None)
        if latest_active is not None and latest_active.content_hash == canonical_hash:
            continue
        for row in actives:
            row.is_active = False
        db.add(
            ConsentDocument(
                type=doc_type,
                version=max((row.version for row in rows), default=0) + 1,
                content_md=content,
                content_hash=canonical_hash,
                published_at=datetime.now(UTC),
                is_active=True,
            )
        )
        changed = True
    if changed:
        await db.commit()
