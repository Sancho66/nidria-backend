"""Endpoints signatures (méga-lot 28/07, lot 3).

- POST /webhooks/docuseal — PUBLIC (ligne auditable) ; l'authenticité est
  le SECRET PARTAGÉ en header (méthode DocuSeal constatée : en-têtes
  personnalisés configurés dans leur console webhook — pas de HMAC du
  corps comme Paddle). Comparaison constante, 401 sinon ; secret non
  configuré = fermé (401), flag éteint = 200 « ignored » silencieux (rien
  n'existe côté domaine, et un 4xx ferait re-livrer DocuSeal à l'infini).
- GET /expat/cases/{case_id}/signatures — EXPAT : les tâches de signature
  de LA personne qui regarde (harnais de filtrage existant réutilisé :
  _get_viewing_case + ciblage par SA case_person ; membre comme principal
  ne voient QUE leurs lignes — le slug est personnel). Flag éteint →
  liste vide (l'espace ne montre rien d'une feature inexistante).
"""

import hmac
import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.expat_user import ExpatUser
from shared.models.signature import SignatureRequest, SignatureSigner
from src.core.config import get_settings
from src.core.dependencies import get_current_agent, get_current_expat, get_db
from src.core.enums import Audience, CasePersonKind
from src.core.exceptions import UnauthorizedError
from src.core.rbac.baseline import RouteBinding
from src.core.rbac.permissions import Permission
from src.signatures.signatures_manager import SignaturesWebhookManager
from src.signatures.signatures_schema import (
    ExpatSignatureResponse,
    SignatureCreditEntriesResponse,
    SignatureCreditEntryResponse,
    SignatureCreditPackResponse,
    SignatureCreditsResponse,
    WebhookAckResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["signatures"])

BINDINGS = [
    # PUBLIC produit (auditable) — l'authenticité est le secret partagé,
    # vérifié dans le handler sur la requête brute.
    RouteBinding("POST", "/webhooks/docuseal", Audience.PUBLIC),
    RouteBinding("GET", "/expat/cases/{case_id}/signatures", Audience.EXPAT),
    # TEMPS 2 : solde lisible par TOUT agent (l'activation d'étape peut
    # échouer sur le solde — précédent trial_ends_at) ; les écritures sont
    # une surface facturation → agency.manage.
    RouteBinding("GET", "/agencies/me/signature-credits", Audience.AGENT),
    RouteBinding(
        "GET",
        "/agencies/me/signature-credits/entries",
        Audience.AGENT,
        Permission.AGENCY_MANAGE,
    ),
]

DbDep = Annotated[AsyncSession, Depends(get_db)]
ExpatDep = Annotated[ExpatUser, Depends(get_current_expat)]
AgentDep = Annotated[Agent, Depends(get_current_agent)]

# Le header porteur du secret partagé — configuré à l'identique dans la
# console webhook DocuSeal (leur mécanisme : en-têtes personnalisés).
SECRET_HEADER = "X-Docuseal-Secret"


@router.post("/webhooks/docuseal", response_model=WebhookAckResponse)
async def docuseal_webhook(request: Request, db: DbDep) -> WebhookAckResponse:
    settings = get_settings()
    if not settings.signatures_enabled:
        # Flag éteint : rien n'existe côté domaine — 200 pour ne pas faire
        # boucler leurs re-livraisons, aucun traitement.
        return WebhookAckResponse(status="ignored")
    secret = settings.docuseal_webhook_secret
    provided = request.headers.get(SECRET_HEADER)
    # Fermé par défaut : secret absent de NOTRE config = 401 (jamais un
    # webhook accepté sans authenticité vérifiable).
    if not secret or not provided or not hmac.compare_digest(provided, secret):
        raise UnauthorizedError("Invalid webhook secret.")
    try:
        payload = json.loads(await request.body())
    except json.JSONDecodeError:
        return WebhookAckResponse(status="ignored")
    event_type = str(payload.get("event_type") or "")
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return WebhookAckResponse(status="ignored")
    status = await SignaturesWebhookManager(db).handle_event(event_type, data)
    return WebhookAckResponse(status=status)


@router.get("/expat/cases/{case_id}/signatures", response_model=list[ExpatSignatureResponse])
async def my_signatures(
    case_id: uuid.UUID, expat: ExpatDep, db: DbDep
) -> list[ExpatSignatureResponse]:
    if not get_settings().signatures_enabled:
        return []
    from src.expat.expat_manager import ExpatPortalManager
    from src.signatures.flags import signatures_effectively_enabled

    # Le harnais d'accès existant : principal OU membre du dossier, 404
    # non-révélateur sinon. viewing_person est None pour le PRINCIPAL —
    # on résout alors SA case_person : le slug est PERSONNEL, personne ne
    # voit ni n'obtient la ligne d'un autre (le principal inclus).
    case, agency, viewing_person = await ExpatPortalManager(db)._get_viewing_case(expat, case_id)
    if not signatures_effectively_enabled(agency):
        return []  # sous-interrupteur agence coupé : la face client se tait
    person_id = viewing_person.id if viewing_person is not None else None
    if person_id is None:
        person_id = (
            await db.execute(
                select(CasePerson.id).where(
                    CasePerson.case_id == case.id,
                    CasePerson.kind == CasePersonKind.PRINCIPAL.value,
                    CasePerson.expat_user_id == expat.id,
                )
            )
        ).scalar_one_or_none()
    if person_id is None:
        return []
    rows = (
        await db.execute(
            select(SignatureSigner, SignatureRequest)
            .join(SignatureRequest, SignatureRequest.id == SignatureSigner.signature_request_id)
            .where(
                SignatureRequest.case_id == case.id,
                SignatureSigner.case_person_id == person_id,
            )
            .order_by(SignatureRequest.created_at)
        )
    ).all()
    # LOT 6 (point 3) : le « Signé n/m » de chaque demande — le principal
    # voit « en attente des autres signataires ». Une requête groupée.
    counts: dict[uuid.UUID, tuple[int, int]] = {}
    if rows:
        from sqlalchemy import case as sa_case

        signed_expr = sa_case((SignatureSigner.status == "signed", 1), else_=0)
        count_rows = await db.execute(
            select(
                SignatureSigner.signature_request_id,
                func.sum(signed_expr),
                func.count(),
            )
            .where(SignatureSigner.signature_request_id.in_([req.id for _s, req in rows]))
            .group_by(SignatureSigner.signature_request_id)
        )
        counts = {rid: (int(signed), int(total)) for rid, signed, total in count_rows}
    return [
        ExpatSignatureResponse(
            signer_id=signer.id,
            request_id=request.id,
            requirement_id=signer.case_step_requirement_id,
            reference=request.reference,
            status=signer.status,
            request_status=request.status,
            embed_slug=signer.provider_slug,
            expires_at=request.expires_at,
            signed_at=signer.signed_at,
            request_signed_count=counts.get(request.id, (0, 0))[0],
            request_signer_total=counts.get(request.id, (0, 0))[1],
        )
        for signer, request in rows
    ]


@router.get("/agencies/me/signature-credits", response_model=SignatureCreditsResponse)
async def my_signature_credits(agent: AgentDep, db: DbDep) -> SignatureCreditsResponse:
    """Solde + seuil + grille des packs — lisible par tout agent (voir
    BINDINGS). Répond aussi flag éteint (0/0, grille vide ou non — le front
    gate l'affichage sur agencies/me.signatures_enabled)."""
    from src.signatures import ledger

    available, reserved = await ledger.balance(db, agent.agency_id)
    agency = await db.get(Agency, agent.agency_id)
    packs = sorted(get_settings().signature_credit_packs.items(), key=lambda kv: kv[1])
    return SignatureCreditsResponse(
        available=available,
        reserved=reserved,
        low_threshold=ledger._low_threshold(agency),
        packs=[
            SignatureCreditPackResponse(price_id=price_id, credits=credits)
            for price_id, credits in packs
        ],
    )


@router.get("/agencies/me/signature-credits/entries", response_model=SignatureCreditEntriesResponse)
async def my_signature_credit_entries(
    agent: AgentDep,
    db: DbDep,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SignatureCreditEntriesResponse:
    """Le ledger paginé (écran facturation, agency.manage) — plus récent
    d'abord, id en départage (leçon Prism : pagination stable)."""
    from shared.models.signature_credit import SignatureCreditEntry

    base = select(SignatureCreditEntry).where(SignatureCreditEntry.agency_id == agent.agency_id)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await db.execute(
                base.order_by(
                    SignatureCreditEntry.created_at.desc(), SignatureCreditEntry.id.desc()
                )
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    return SignatureCreditEntriesResponse(
        items=[SignatureCreditEntryResponse.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )
