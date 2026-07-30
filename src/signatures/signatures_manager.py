"""Cycle de vie des demandes de signature (méga-lot 28/07).

Positions tenues :
- FLAG D'ABORD : `signatures_enabled` faux (le défaut) → chaque entrée de
  ce manager est un no-op strict. Rien ne se matérialise, rien n'appelle
  le provider, rien ne touche un crédit.
- 1 demande = 1 document envoyé (N signataires dessus) = 1 crédit
  (réservé à l'envoi, consommé à la complétion, libéré sinon — lot 2).
- La complétude roule sur les RAILS EXISTANTS : chaque signataire est lié
  à SA ligne case_step_requirement (snapshot signature_required) qui reste
  `pending` jusqu'à SA signature — step_all_met, relances auto, ciblage
  membre et filtrage espace client fonctionnent sans une ligne de code.
- PERSONNE TARDIVE (décision, au rapport) : le port ne sait pas ajouter un
  signataire à une soumission vivante (DocuSeal ne le permet pas) → la
  personne ajoutée après l'envoi reçoit SA PROPRE demande (1 signataire),
  qui coûte son propre crédit à l'envoi — « 1 crédit = 1 document envoyé »
  reste vrai au sens strict.
- Aucun commit ici : tout roule dans la transaction de l'appelant
  (activation d'étape, add_person, webhook) — un échec provider ou un
  solde insuffisant annule TOUT (l'étape ne s'active pas à moitié).
"""

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import case as sa_case
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.case_step_progress import CaseStepProgress
from shared.models.case_step_requirement import CaseStepRequirement
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.signature import SignatureRequest, SignatureSigner
from src.core import storage
from src.core.config import get_settings
from src.core.enums import (
    ActorType,
    CasePersonKind,
    SignatureProviderKind,
    SignatureRequestStatus,
    SignatureSignerStatus,
)
from src.core.exceptions import ValidationError
from src.signatures.flags import signatures_effectively_enabled
from src.signatures.provider import AGENCY_ROLE, ProviderSigner, get_provider
from src.signatures.signatures_repository import SignaturesRepository

if TYPE_CHECKING:
    from src.progress.progress_manager import PendingMail

logger = logging.getLogger(__name__)


def _placeholder_email(signer_id: uuid.UUID) -> str:
    """Une personne sans compte n'a pas d'adresse ; le provider exige un
    champ email par signataire. Adresse technique JAMAIS écrite (aucun
    email provider — send_email:false — ni nôtre : elle n'entre dans aucun
    chemin d'envoi v4). Le pattern demo+@nidria.app du seed, adapté."""
    return f"signer+{signer_id.hex}@nidria.app"


class SignaturesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = SignaturesRepository(db)

    # --- envoi (activation d'étape + personne tardive) ----------------------------

    async def send_for_progress(self, case: ClientCase, row: CaseStepProgress) -> int:
        """À l'ACTIVATION d'une étape : une demande par document signable,
        signataires = les personnes des lignes concrètes signables (le
        scope a déjà fait son travail à la matérialisation). Idempotent :
        une personne déjà assise sur une demande vivante de la même
        (étape, référence) n'est jamais réassise. Retourne le nombre de
        demandes envoyées."""
        agency = await self.db.get(Agency, case.agency_id)
        if not signatures_effectively_enabled(agency):
            return 0
        rows = (
            (
                await self.db.execute(
                    select(CaseStepRequirement)
                    .join(CasePerson, CasePerson.id == CaseStepRequirement.person_id)
                    .where(
                        CaseStepRequirement.case_step_progress_id == row.id,
                        CaseStepRequirement.signature_required.is_(True),
                    )
                    # Ordre de matérialisation — principal d'abord, puis
                    # ancienneté de la PERSONNE (les lignes d'une même
                    # activation partagent leur created_at : le now() de
                    # Postgres est figé par transaction) : c'est LUI que
                    # suit la convention de rôles « Signataire N ».
                    .order_by(
                        sa_case((CasePerson.kind == CasePersonKind.PRINCIPAL.value, 0), else_=1),
                        CasePerson.created_at,
                        CasePerson.id,
                    )
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0
        by_reference: dict[str, list[CaseStepRequirement]] = {}
        for req_row in rows:
            by_reference.setdefault(req_row.reference, []).append(req_row)
        sent = 0
        for reference, group in by_reference.items():
            seated = await self.repo.live_signer_person_ids(row.id, reference)
            fresh = [r for r in group if r.person_id not in seated]
            if not fresh:
                continue
            await self._send_one(case, row, reference, fresh)
            sent += 1
        return sent

    async def send_for_late_person(
        self, case: ClientCase, person: CasePerson, created_rows: list[CaseStepRequirement]
    ) -> int:
        """Fin du gel de composition, versant signatures : la personne
        ajoutée après l'activation reçoit SA demande par document signable
        (recréation, pas d'ajout au vol — voir docstring module)."""
        agency = await self.db.get(Agency, case.agency_id)
        if not signatures_effectively_enabled(agency):
            return 0
        sent = 0
        for req_row in created_rows:
            if not req_row.signature_required:
                continue
            progress = await self.db.get(CaseStepProgress, req_row.case_step_progress_id)
            if progress is None:
                continue
            seated = await self.repo.live_signer_person_ids(progress.id, req_row.reference)
            if person.id in seated:
                continue
            await self._send_one(
                case, progress, req_row.reference, [req_row], partial_recreation=True
            )
            sent += 1
        return sent

    async def _send_one(
        self,
        case: ClientCase,
        row: CaseStepProgress,
        reference: str,
        req_rows: list[CaseStepRequirement],
        *,
        partial_recreation: bool = False,
    ) -> SignatureRequest:
        """Crée la demande (draft) + ses signataires, débite (lot 2) puis
        appelle le port — refs opaques posées, statut SENT. Dans la
        transaction de l'appelant : tout échec annule tout."""
        settings = get_settings()
        definition_id = next(
            (r.step_requirement_id for r in req_rows if r.step_requirement_id), None
        )
        expires_at = datetime.now(UTC) + timedelta(days=settings.signature_request_expires_days)
        request = self.repo.add_request(
            case_id=case.id,
            case_step_progress_id=row.id,
            step_requirement_id=definition_id,
            reference=reference,
            provider=SignatureProviderKind.DOCUSEAL.value,
            level=req_rows[0].signature_level,
            status=SignatureRequestStatus.DRAFT.value,
            expires_at=expires_at,
        )
        await self.db.flush()
        signers: list[SignatureSigner] = []
        for req_row in req_rows:
            signers.append(
                self.repo.add_signer(
                    signature_request_id=request.id,
                    case_person_id=req_row.person_id,
                    case_step_requirement_id=req_row.id,
                    status=SignatureSignerStatus.PENDING.value,
                )
            )
        await self.db.flush()

        # Lot 2 — réservation du crédit (1 par document envoyé), AVANT tout
        # appel provider : solde insuffisant → erreur typée, rollback total.
        from src.signatures.ledger import reserve_credit

        await reserve_credit(self.db, case.agency_id, request)

        # Méga-lot modèles (29/07) — on n'envoie JAMAIS un modèle absent ou
        # sans zones (défense structurelle : l'assignation refuse déjà le
        # modèle manquant ; le builder non sauvegardé se refuse ICI, à
        # l'envoi). Constat sonde : le provider ne valide PAS les rôles —
        # plus de signataires que de rôles configurés = signataire fantôme
        # sans zones, donc la garde de cohérence vit chez nous.
        from shared.models.document_template import DocumentTemplate

        template_id = req_rows[0].document_template_id
        template = await self.db.get(DocumentTemplate, template_id) if template_id else None
        if template is None:
            raise ValidationError(
                f"Signable requirement {reference!r} has no document template.",
                code="journey.signature_document_missing",
                params={"reference": reference},
            )
        if not template.fields_configured:
            raise ValidationError(
                f"Document template {template.name!r} has no signature zones yet; "
                "save them in the builder first.",
                code="signatures.template_fields_missing",
                params={"reference": reference, "template_name": template.name},
            )
        if template.roles_count < len(signers):
            raise ValidationError(
                f"Document template {template.name!r} has {template.roles_count} signer "
                f"role(s) but this step needs {len(signers)}.",
                code="signatures.template_roles_insufficient",
                params={
                    "reference": reference,
                    "template_name": template.name,
                    "roles_count": template.roles_count,
                    "signers_count": len(signers),
                },
            )
        # Mini-complément (30/07) — le sens INVERSE : plus de rôles que de
        # personnes à l'ACTIVATION = un rôle fantôme dont les zones ne
        # seront jamais signées (document final à zone vide). Refus nommé.
        # Désarmée pour la recréation tardive : SA demande partielle (1
        # signataire sur « Signataire 1 ») est le design assumé.
        if not partial_recreation and template.roles_count > len(signers):
            raise ValidationError(
                f"Document template {template.name!r} has {template.roles_count} signer "
                f"role(s) but this step only seats {len(signers)} — the extra zones "
                "would never be signed.",
                code="signatures.template_roles_exceed_persons",
                params={
                    "reference": reference,
                    "template_name": template.name,
                    "roles_count": template.roles_count,
                    "signers_count": len(signers),
                },
            )
        # Contreseing (lot 30/07) : le siège agence se résout AVANT tout
        # appel provider (422 nommé sans candidat, rollback total) — même
        # sur la demande PARTIELLE d'une personne tardive : SON document
        # porte aussi la signature de l'agence (verdict : le livrable final
        # est complet pour chaque document envoyé).
        countersigner: Agent | None = None
        if template.agency_countersigns:
            countersigner = await self._resolve_countersigner(case)
            agency_signer = self.repo.add_signer(
                signature_request_id=request.id,
                case_person_id=None,
                agent_id=countersigner.id,
                case_step_requirement_id=None,
                status=SignatureSignerStatus.PENDING.value,
            )
            await self.db.flush()
            signers.append(agency_signer)
        provider_signers = await self._provider_signers(case, signers)
        # Convention sonde : « Signataire N », 1-based, dans l'ordre de
        # matérialisation (le principal d'abord — l'ordre des lignes) ;
        # le contreseing porte le rôle STABLE « Agence », groupe d'ordre 1
        # (les clients en 0, parallèles — sonde 30/07 : groupes supportés).
        client_index = 0
        for provider_signer, signer_row in zip(provider_signers, signers, strict=True):
            if signer_row.agent_id is not None:
                provider_signer.role = AGENCY_ROLE
                provider_signer.order = 1
            else:
                client_index += 1
                provider_signer.role = f"Signataire {client_index}"
                provider_signer.order = 0
        created = await get_provider().create_from_template(
            template_ref=template.provider_template_ref,
            signers=provider_signers,
            expires_at=expires_at,
        )
        request.provider_ref = created.provider_ref
        by_id = {str(s.id): s for s in signers}
        for created_signer in created.signers:
            signer = by_id.get(created_signer.signer_id)
            if signer is None:  # défensif : le provider renvoie nos ids
                logger.warning("unknown signer id from provider: %s", created_signer.signer_id)
                continue
            signer.provider_ref = created_signer.provider_ref
            signer.provider_slug = created_signer.slug
        request.status = SignatureRequestStatus.SENT.value
        request.sent_at = datetime.now(UTC)
        self._log_activity(case, "signature.request_sent", request)
        return request

    async def _resolve_countersigner(self, case: ClientCase) -> Agent:
        """Contreseing (lot 30/07) : l'agent assigné au dossier, sinon le
        PREMIER porteur d'agency.manage de l'agence — ordre STABLE
        (created_at puis id : l'ancienneté, jamais l'aléa). Aucun candidat
        → 422 nommé (un contreseing sans signataire n'est pas envoyable)."""
        from shared.models.rbac import Permission as PermissionRow
        from shared.models.rbac import RolePermission
        from src.core.rbac.permissions import Permission

        if case.owner_agent_id is not None:
            owner = await self.db.get(Agent, case.owner_agent_id)
            if owner is not None and owner.deactivated_at is None:
                return owner
        holder = (
            await self.db.execute(
                select(Agent)
                .join(RolePermission, RolePermission.role_id == Agent.role_id)
                .join(PermissionRow, PermissionRow.id == RolePermission.permission_id)
                .where(
                    Agent.agency_id == case.agency_id,
                    Agent.deactivated_at.is_(None),
                    PermissionRow.key == Permission.AGENCY_MANAGE.value,
                )
                .order_by(Agent.created_at, Agent.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if holder is None:
            raise ValidationError(
                "No agent available to countersign (no case owner, no agency.manage holder).",
                code="signatures.no_countersigner",
            )
        return holder

    async def _provider_signers(
        self, case: ClientCase, signers: list[SignatureSigner]
    ) -> list[ProviderSigner]:
        out: list[ProviderSigner] = []
        for signer in signers:
            if signer.agent_id is not None:
                agent = await self.db.get(Agent, signer.agent_id)
                out.append(
                    ProviderSigner(
                        signer_id=str(signer.id),
                        name=(
                            f"{agent.first_name} {agent.last_name}".strip() if agent else "Agence"
                        ),
                        email=(agent.email if agent else None) or _placeholder_email(signer.id),
                    )
                )
                continue
            person = await self.db.get(CasePerson, signer.case_person_id)
            name = (person.full_name if person else None) or ""
            email: str | None = None
            if person is not None and person.expat_user_id is not None:
                expat = await self.db.get(ExpatUser, person.expat_user_id)
                if expat is not None:
                    email = expat.email
                    if not name:
                        name = f"{expat.first_name} {expat.last_name}".strip()
            out.append(
                ProviderSigner(
                    signer_id=str(signer.id),
                    name=name or "Signataire",
                    email=email or _placeholder_email(signer.id),
                )
            )
        return out

    # --- annulation ----------------------------------------------------------------

    async def cancel_request(self, case: ClientCase, request: SignatureRequest) -> None:
        """Annule une demande vivante : provider d'abord (un échec annule
        tout), puis release du crédit réservé (lot 2) et statut CANCELLED.
        Les lignes d'exigence des signataires restent pending — l'étape
        redevient simplement « en attente », comme une pièce retirée."""
        if request.status in (
            SignatureRequestStatus.CANCELLED.value,
            SignatureRequestStatus.EXPIRED.value,
            SignatureRequestStatus.COMPLETED.value,
        ):
            return
        if request.provider_ref:
            await get_provider().cancel(request.provider_ref)
        from src.signatures.ledger import release_credit

        await release_credit(self.db, case.agency_id, request, reason="cancelled")
        request.status = SignatureRequestStatus.CANCELLED.value
        request.cancelled_at = datetime.now(UTC)
        self._log_activity(case, "signature.request_cancelled", request)

    # --- helpers --------------------------------------------------------------------

    def _log_activity(self, case: ClientCase, action_type: str, request: SignatureRequest) -> None:
        from src.activity.activity_manager import ActivityManager

        ActivityManager(self.db).log_action(
            case_id=case.id,
            actor_type=ActorType.SYSTEM,
            actor_id=None,
            action_type=action_type,
            details={"signature_request_id": str(request.id), "reference": request.reference},
        )


class SignaturesWebhookManager:
    """Traitement des événements provider (méga-lot 28/07, lot 3).

    Idempotence par CONVERGENCE (pas de table d'événements : DocuSeal ne
    porte pas d'event_id unique) : chaque handler est un no-op quand l'état
    visé est déjà atteint — un form.completed rejoué ne re-signe rien, un
    consume rejoué est bloqué par la garde du ledger, un release doublé
    aussi. L'authenticité est vérifiée AU ROUTER (secret partagé, méthode
    DocuSeal constatée), jamais ici."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = SignaturesRepository(db)

    async def handle_event(self, event_type: str, data: dict[str, object]) -> str:
        if not get_settings().signatures_enabled:
            return "ignored"
        if event_type == "form.completed":
            return await self._on_form_completed(data)
        if event_type == "form.declined":
            return await self._on_form_declined(data)
        if event_type in ("submission.expired", "submission.archived"):
            return await self._on_submission_ended(event_type, data)
        return "ignored"

    async def _signer_from(self, data: dict[str, object]) -> SignatureSigner | None:
        """external_id = NOTRE signature_signer.id (posé à la création) —
        la résolution ne parse jamais rien de provider-spécifique."""
        raw = data.get("external_id")
        if not raw:
            return None
        try:
            signer_id = uuid.UUID(str(raw))
        except ValueError:
            return None
        return await self.repo.get_signer(signer_id)

    async def _on_form_completed(self, data: dict[str, object]) -> str:
        signer = await self._signer_from(data)
        if signer is None:
            logger.warning(
                "docuseal form.completed for unknown signer: %r", data.get("external_id")
            )
            return "ignored"
        request = await self.repo.get_request(signer.signature_request_id)
        if request is None:
            return "ignored"
        case = await self.db.get(ClientCase, request.case_id)
        if case is None:
            return "ignored"
        # Auto-clôture (lot 1.4) : la signature complète une étape
        # « validée par : Personne » EXACTEMENT comme une pièce — snapshot
        # avant l'écriture, recompute après (auto→DONE / mail
        # prêt-à-valider sur la transition pending→met), le geste de
        # update_person recopié.
        from src.progress.progress_manager import ProgressManager

        progress_mgr = ProgressManager(self.db)
        before = await progress_mgr.snapshot_active_completion(case)
        if signer.status != SignatureSignerStatus.SIGNED.value:
            signer.status = SignatureSignerStatus.SIGNED.value
            signer.signed_at = datetime.now(UTC)
            # La ligne d'exigence de CETTE personne passe fournie — la
            # complétude (step_all_met, relances, espace client) roule sur
            # les rails existants, comme une pièce déposée.
            if signer.case_step_requirement_id is not None:
                req_row = await self.db.get(CaseStepRequirement, signer.case_step_requirement_id)
                if req_row is not None and req_row.status != "provided":
                    req_row.status = "provided"
                    req_row.provided_at = datetime.now(UTC)
        signers = await self.repo.list_signers(request.id)
        still_pending = [s for s in signers if s.status == SignatureSignerStatus.PENDING.value]
        # Contreseing (lot 30/07) : le DERNIER client vient de signer et le
        # siège agence attend → le tour de l'agence arrive, notification à
        # l'agent résolu (sa langue = défaut agence). Le slug ne transite
        # jamais par mail — l'API le sert au seul agent concerné.
        countersign_turn_mail = None
        if (
            still_pending
            and all(s.agent_id is not None for s in still_pending)
            and signer.agent_id is None
        ):
            countersign_turn_mail = await self._countersign_turn_mail(
                case, request, still_pending[0]
            )
        if still_pending and request.status == SignatureRequestStatus.SENT.value:
            request.status = SignatureRequestStatus.PARTIALLY_SIGNED.value
        completion_mails: list[PendingMail] = []
        if not still_pending and request.status != SignatureRequestStatus.COMPLETED.value:
            completion_mails = await self._complete(request)
        pending_mails = await progress_mgr.recompute_active(case, before)
        pending_mails.extend(completion_mails)
        if countersign_turn_mail is not None:
            pending_mails.append(countersign_turn_mail)
        await self.db.commit()
        await progress_mgr.send_pending(pending_mails)
        return "processed"

    async def _countersign_turn_mail(
        self, case: ClientCase, request: SignatureRequest, agency_seat: SignatureSigner
    ) -> "PendingMail | None":
        from src.core.email_templates import countersign_turn_email
        from src.core.i18n import case_label_for_notif, resolve_notification_lang_agent
        from src.progress.progress_manager import PendingMail
        from src.progress.progress_repository import ProgressRepository

        agent = await self.db.get(Agent, agency_seat.agent_id)
        if agent is None or not agent.email:
            return None
        repo = ProgressRepository(self.db)
        lang = resolve_notification_lang_agent(await repo.agency_default_language(case.agency_id))
        first, last, j_name, j_i18n = await repo.get_case_label_parts(case)
        case_label = case_label_for_notif(first, last, j_name, j_i18n, lang)
        link = f"{get_settings().frontend_url}/app/cases/{case.id}"
        return PendingMail(
            to=agent.email,
            content=countersign_turn_email(case_label, request.reference, link, lang),
        )

    async def _complete(self, request: SignatureRequest) -> "list[PendingMail]":
        """Tous signés : consume du crédit + téléchargement IMMÉDIAT du PDF
        signé et du dossier de preuve (leurs URLs expirent — on stocke les
        OCTETS, jamais une URL), rangement GAP-B en livrable du dossier.
        Retourne les notifications « document signé » (envoi post-commit)."""
        case = await self.db.get(ClientCase, request.case_id)
        if case is None:
            return []
        from src.signatures.ledger import consume_credit

        await consume_credit(self.db, case.agency_id, request)
        request.status = SignatureRequestStatus.COMPLETED.value
        request.completed_at = datetime.now(UTC)
        document_id: uuid.UUID | None = None
        if request.provider_ref:
            files = await get_provider().download_completed(request.provider_ref)
            document_id = await self._store_deliverable(
                case, request, files.document_filename, files.document_pdf
            )
            if files.audit_pdf is not None and files.audit_filename:
                await self._store_deliverable(case, request, files.audit_filename, files.audit_pdf)
        if document_id is not None:
            signers = await self.repo.list_signers(request.id)
            for signer in signers:
                if signer.case_step_requirement_id is None:
                    continue
                req_row = await self.db.get(CaseStepRequirement, signer.case_step_requirement_id)
                if req_row is not None:
                    req_row.document_id = document_id
        SignaturesManager(self.db)._log_activity(case, "signature.request_completed", request)
        return await self._document_signed_mails(case, request)

    async def _document_signed_mails(
        self, case: ClientCase, request: SignatureRequest
    ) -> "list[PendingMail]":
        """Lot notification (30/07) : à la COMPLÉTION (jamais partielle),
        chaque signataire CLIENT à l'email connu est prévenu dans SA langue
        — jamais de pièce jointe, le lien mène à l'espace (/space : le
        deep link étape/documents n'existe pas encore côté front, constat).
        La face AGENCE aussi (constat : rien de direct n'existait), au
        propriétaire du dossier, langue par défaut agence. Un signataire
        sans email/compte est sauté en silence."""
        from src.core.email import space_link
        from src.core.email_templates import (
            document_signed_agency_email,
            document_signed_client_email,
        )
        from src.core.i18n import (
            case_label_for_notif,
            resolve_notification_lang_agent,
            resolve_notification_lang_client,
        )
        from src.progress.progress_manager import PendingMail
        from src.progress.progress_repository import ProgressRepository

        mails: list[PendingMail] = []
        agency = await self.db.get(Agency, case.agency_id)
        agency_name = agency.name if agency else "Votre agence"
        agency_slug = agency.slug if agency else None
        client_url = space_link(get_settings().frontend_url, "/space", agency_slug)
        signers = await self.repo.list_signers(request.id)
        seen_emails: set[str] = set()
        for signer in signers:
            if signer.case_person_id is None:
                continue  # le siège agence est servi par le mail agence
            person = await self.db.get(CasePerson, signer.case_person_id)
            if person is None or person.expat_user_id is None:
                continue  # personne sans compte/email : silencieux (garde-fou)
            expat = await self.db.get(ExpatUser, person.expat_user_id)
            if expat is None or not expat.email or expat.email in seen_emails:
                continue
            seen_emails.add(expat.email)
            lang = resolve_notification_lang_client(expat.preferred_lang)
            mails.append(
                PendingMail(
                    to=expat.email,
                    content=document_signed_client_email(
                        agency_name, request.reference, client_url, lang
                    ),
                )
            )
        if case.owner_agent_id is not None:
            owner = await self.db.get(Agent, case.owner_agent_id)
            if owner is not None and owner.email and owner.deactivated_at is None:
                repo = ProgressRepository(self.db)
                lang = resolve_notification_lang_agent(
                    await repo.agency_default_language(case.agency_id)
                )
                first, last, j_name, j_i18n = await repo.get_case_label_parts(case)
                case_label = case_label_for_notif(first, last, j_name, j_i18n, lang)
                app_link = f"{get_settings().frontend_url}/app/cases/{case.id}"
                mails.append(
                    PendingMail(
                        to=owner.email,
                        content=document_signed_agency_email(
                            case_label, request.reference, app_link, lang
                        ),
                    )
                )
        return mails

    async def _store_deliverable(
        self, case: ClientCase, request: SignatureRequest, filename: str, content: bytes
    ) -> uuid.UUID:
        """Rangement dans le système documentaire du dossier (GAP-B) : un
        LIVRABLE produit pour le client (kind=deliverable, visible dossier
        entier — person_id NULL, décision B), acteur SYSTEM porté par l'id
        de la demande (uploaded_by_id est NOT NULL ; l'id de la demande est
        la provenance exacte, commenté ici même)."""
        from src.documents.documents_repository import DocumentsRepository

        document_id = uuid.uuid4()
        path = f"{case.id}/{document_id}/{storage.sanitize_filename(filename)}"
        await asyncio.to_thread(storage.upload, path, content, "application/pdf")
        DocumentsRepository(self.db).add_document(
            id=document_id,
            case_id=case.id,
            step_progress_id=request.case_step_progress_id,
            filename=filename,
            storage_path=path,
            uploaded_by_type=ActorType.SYSTEM.value,
            uploaded_by_id=request.id,
            kind="deliverable",
            person_id=None,
        )
        return document_id

    async def _on_form_declined(self, data: dict[str, object]) -> str:
        signer = await self._signer_from(data)
        if signer is None:
            return "ignored"
        if signer.status == SignatureSignerStatus.PENDING.value:
            signer.status = SignatureSignerStatus.DECLINED.value
            await self.db.commit()
        return "processed"

    async def _on_submission_ended(self, event_type: str, data: dict[str, object]) -> str:
        """submission.expired / submission.archived : la demande meurt, le
        crédit réservé revient (release idempotent). Une demande déjà
        complétée/annulée ne bouge plus."""
        submission_id = str(data.get("id") or "")
        if not submission_id:
            return "ignored"
        request = await self.repo.get_request_by_provider_ref(submission_id)
        if request is None:
            return "ignored"
        if request.status in (
            SignatureRequestStatus.COMPLETED.value,
            SignatureRequestStatus.CANCELLED.value,
            SignatureRequestStatus.EXPIRED.value,
        ):
            return "processed"
        case = await self.db.get(ClientCase, request.case_id)
        if case is None:
            return "ignored"
        from src.signatures.ledger import release_credit

        reason = "expired" if event_type == "submission.expired" else "archived"
        await release_credit(self.db, case.agency_id, request, reason=reason)
        request.status = (
            SignatureRequestStatus.EXPIRED.value
            if event_type == "submission.expired"
            else SignatureRequestStatus.CANCELLED.value
        )
        if request.status == SignatureRequestStatus.CANCELLED.value:
            request.cancelled_at = datetime.now(UTC)
        SignaturesManager(self.db)._log_activity(case, f"signature.request_{reason}", request)
        await self.db.commit()
        return "processed"
