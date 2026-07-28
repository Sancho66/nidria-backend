"""Ledger de crédits signature (méga-lot 28/07, lot 2).

Contrat de débit : `reserve` à l'ENVOI d'une demande (1 crédit = 1
document envoyé, quel que soit le nombre de signataires), `consume` à la
complétion, `release` sur annulation/expiration. `purchase` au webhook
Paddle (idempotent : ligne événement unique en amont + ceinture unique
sur l'entrée).

Concurrence : la ligne `signature_credit_balance` est verrouillée
(SELECT FOR UPDATE) le temps de l'écriture — deux réservations simultanées
se sérialisent, et le CHECK available >= 0 rend le négatif impossible
même si un chemin oubliait le verrou. Aucun commit ici : tout roule dans
la transaction de l'appelant (un solde insuffisant annule l'activation
d'étape entière).
"""

import asyncio
import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.rbac import Permission as PermissionRow
from shared.models.rbac import Role, RolePermission
from shared.models.signature import SignatureRequest
from shared.models.signature_credit import SignatureCreditBalance, SignatureCreditEntry
from src.core.config import get_settings
from src.core.email import send_email
from src.core.email_templates import signature_credits_low_email
from src.core.enums import SignatureCreditKind
from src.core.exceptions import ConflictError
from src.core.i18n import resolve_notification_lang_agent

logger = logging.getLogger(__name__)


class SignatureCreditsInsufficientError(ConflictError):
    """Erreur domaine typée du solde insuffisant — le front l'affiche
    (code + params) ; côté activation d'étape elle annule TOUT (l'étape ne
    s'active pas à moitié : la transaction de l'appelant rollback)."""

    def __init__(self, available: int) -> None:
        super().__init__(
            "Not enough signature credits to send this request.",
            code="signatures.credits_insufficient",
            params={"available": available, "needed": 1},
        )


async def _locked_balance(db: AsyncSession, agency_id: uuid.UUID) -> SignatureCreditBalance:
    """La ligne de solde, verrouillée pour la transaction (création au
    premier usage — 0/0)."""
    row = (
        await db.execute(
            select(SignatureCreditBalance)
            .where(SignatureCreditBalance.agency_id == agency_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        row = SignatureCreditBalance(agency_id=agency_id, available=0, reserved=0)
        db.add(row)
        await db.flush()
        # Re-SELECT verrouillé : deux créations simultanées se départagent
        # sur l'unique (agency_id), le perdant relit la ligne du gagnant.
        row = (
            await db.execute(
                select(SignatureCreditBalance)
                .where(SignatureCreditBalance.agency_id == agency_id)
                .with_for_update()
            )
        ).scalar_one()
    return row


def _entry(
    db: AsyncSession,
    agency_id: uuid.UUID,
    kind: SignatureCreditKind,
    amount: int,
    *,
    request: SignatureRequest | None = None,
    paddle_event_id: str | None = None,
    details: dict[str, object] | None = None,
) -> None:
    db.add(
        SignatureCreditEntry(
            agency_id=agency_id,
            kind=kind.value,
            amount=amount,
            signature_request_id=request.id if request is not None else None,
            paddle_event_id=paddle_event_id,
            details=details or {},
        )
    )


async def balance(db: AsyncSession, agency_id: uuid.UUID) -> tuple[int, int]:
    """(available, reserved) — 0/0 sans ligne."""
    row = (
        await db.execute(
            select(SignatureCreditBalance).where(SignatureCreditBalance.agency_id == agency_id)
        )
    ).scalar_one_or_none()
    return (row.available, row.reserved) if row is not None else (0, 0)


async def derived_balance(db: AsyncSession, agency_id: uuid.UUID) -> tuple[int, int]:
    """Le solde REDÉRIVÉ des écritures — la vérité comptable. Le test
    d'invariant épingle balance() == derived_balance() sur tous les flux."""
    rows: dict[str, int] = {
        kind: total
        for kind, total in (
            await db.execute(
                select(
                    SignatureCreditEntry.kind,
                    func.coalesce(func.sum(SignatureCreditEntry.amount), 0),
                )
                .where(SignatureCreditEntry.agency_id == agency_id)
                .group_by(SignatureCreditEntry.kind)
            )
        ).all()
    }
    purchases = int(rows.get(SignatureCreditKind.PURCHASE.value, 0))
    reserves = int(rows.get(SignatureCreditKind.RESERVE.value, 0))
    consumes = int(rows.get(SignatureCreditKind.CONSUME.value, 0))
    releases = int(rows.get(SignatureCreditKind.RELEASE.value, 0))
    available = purchases - reserves + releases
    reserved = reserves - consumes - releases
    return available, reserved


async def purchase_credits(
    db: AsyncSession,
    agency_id: uuid.UUID,
    credits: int,
    *,
    paddle_event_id: str,
    details: dict[str, object] | None = None,
) -> None:
    """Achat (webhook Paddle). Idempotent : la ceinture unique sur
    paddle_event_id fait d'un rejeu un no-op silencieux (l'événement
    Paddle est déjà dédupliqué en amont — doctrine billing)."""
    existing = (
        await db.execute(
            select(SignatureCreditEntry.id).where(
                SignatureCreditEntry.paddle_event_id == paddle_event_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return
    row = await _locked_balance(db, agency_id)
    row.available += credits
    _entry(
        db,
        agency_id,
        SignatureCreditKind.PURCHASE,
        credits,
        paddle_event_id=paddle_event_id,
        details=details,
    )


async def reserve_credit(db: AsyncSession, agency_id: uuid.UUID, request: SignatureRequest) -> None:
    """1 crédit réservé pour CETTE demande. Solde insuffisant → erreur
    typée, rien d'écrit (la transaction de l'appelant rollback)."""
    row = await _locked_balance(db, agency_id)
    if row.available < 1:
        raise SignatureCreditsInsufficientError(row.available)
    before = row.available
    row.available -= 1
    row.reserved += 1
    _entry(db, agency_id, SignatureCreditKind.RESERVE, 1, request=request)
    await _maybe_low_balance_alert(db, agency_id, before=before, after=row.available)


async def _live_reserve_exists(db: AsyncSession, request: SignatureRequest) -> bool:
    """Une réservation encore vivante pour cette demande ? (reserve −
    consume − release, par demande) — la garde d'idempotence de consume/
    release face aux webhooks rejoués et aux courses annulation/expiration."""
    rows: dict[str, int] = {
        kind: total
        for kind, total in (
            await db.execute(
                select(SignatureCreditEntry.kind, func.count())
                .where(SignatureCreditEntry.signature_request_id == request.id)
                .group_by(SignatureCreditEntry.kind)
            )
        ).all()
    }
    reserves = int(rows.get(SignatureCreditKind.RESERVE.value, 0))
    settled = int(rows.get(SignatureCreditKind.CONSUME.value, 0)) + int(
        rows.get(SignatureCreditKind.RELEASE.value, 0)
    )
    return reserves > settled


async def consume_credit(db: AsyncSession, agency_id: uuid.UUID, request: SignatureRequest) -> None:
    """Complétion : la réservation devient consommation définitive.
    Idempotent (webhook rejoué → no-op)."""
    if not await _live_reserve_exists(db, request):
        return
    row = await _locked_balance(db, agency_id)
    row.reserved -= 1
    _entry(db, agency_id, SignatureCreditKind.CONSUME, 1, request=request)


async def release_credit(
    db: AsyncSession, agency_id: uuid.UUID, request: SignatureRequest, *, reason: str
) -> None:
    """Annulation / expiration : le crédit réservé redevient disponible.
    Idempotent (double annulation, course avec l'expiration → un seul
    release)."""
    if not await _live_reserve_exists(db, request):
        return
    row = await _locked_balance(db, agency_id)
    row.reserved -= 1
    row.available += 1
    _entry(
        db, agency_id, SignatureCreditKind.RELEASE, 1, request=request, details={"reason": reason}
    )


# --- seuil de solde bas (notification v4) -------------------------------------------


def _low_threshold(agency: Agency | None) -> int:
    settings = get_settings()
    raw = ((agency.settings if agency else None) or {}).get("signature_credits_low_threshold")
    if isinstance(raw, int) and raw >= 0:
        return raw
    return settings.signature_credits_low_threshold_default


async def _maybe_low_balance_alert(
    db: AsyncSession, agency_id: uuid.UUID, *, before: int, after: int
) -> None:
    """Alerte au FRANCHISSEMENT du seuil (before >= seuil > after) — un
    mail par franchissement, jamais un par réservation (le rachat qui
    repasse au-dessus réarme naturellement). Envoi via la machinerie v4
    (templates + outbox mocké), inline best-effort : le franchissement est
    détecté dans la transaction, le mail ne la bloque jamais."""
    agency = await db.get(Agency, agency_id)
    threshold = _low_threshold(agency)
    if not (before >= threshold > after):
        return
    recipients = (
        (
            await db.execute(
                select(Agent.email)
                .join(Role, Role.id == Agent.role_id)
                .join(RolePermission, RolePermission.role_id == Role.id)
                .join(PermissionRow, PermissionRow.id == RolePermission.permission_id)
                .where(
                    Agent.agency_id == agency_id,
                    Agent.deactivated_at.is_(None),
                    Agent.is_external.is_(False),
                    PermissionRow.key == "agency.manage",
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    if not recipients:
        return
    lang = resolve_notification_lang_agent(agency.default_language if agency else None)
    content = signature_credits_low_email(agency.name if agency else "", after, threshold, lang)
    for email_addr in recipients:
        try:
            await asyncio.to_thread(
                send_email, email_addr, content.subject, content.text, content.html
            )
        except Exception:  # noqa: BLE001 — best-effort, jamais bloquant
            logger.warning("low-balance mail failed to=%s", email_addr, exc_info=True)
