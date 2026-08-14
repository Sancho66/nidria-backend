"""La relance d'activation (lot 14/08) — SYNC, via job_wrapper.run_job.

LE MANQUE QU'ELLE COMBLE : un client invité qui n'activait pas n'entendait
plus jamais parler de son espace. Le lien mourait à 14 jours, en silence.
Constat prod du 14/08 : 19 clients sur 41 jamais entrés, dont six chez une
même agence avec un lien mort depuis cinq semaines — et le seul levier était
un geste manuel, dossier par dossier, auquel personne n'a pensé.

LE CALENDRIER : J+3 puis J+7 après l'invitation, puis PLUS RIEN. Un troisième
rappel serait du harcèlement, pas un service. Le palier atteint est stocké sur
l'invitation (`activation_reminder_stage`, monotone), donc le balayage est
idempotent et un tick manqué rattrape sans doubler.

CE QU'ELLE NE FAIT JAMAIS : relancer sur un lien mort (l'invitation expirée
sort du périmètre — sa réparation est le geste public
`/auth/expat/activate/resend`), relancer un compte déjà actif, ou toucher une
agence qui a coupé le réglage (`settings["activation_reminders_enabled"]`).
"""

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from shared.models.agency import Agency
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.invitation import CaseInvitation
from shared.models.journey import JourneyTemplate
from src.core.config import get_settings
from src.core.email import send_email, sender_as_agency, space_link
from src.core.email_templates import activation_reminder_email
from src.core.enums import InvitationStatus
from src.core.i18n import resolve_i18n, resolve_notification_lang_client
from src.core.job_wrapper import LogFn
from src.core.notification_prefs import activation_reminders_enabled

logger = logging.getLogger(__name__)

# Le calendrier, en jours depuis l'envoi de l'invitation. Fin de la liste = fin
# des relances (jamais de troisième).
SCHEDULE: tuple[int, ...] = (3, 7)


def send_activation_reminders(db: Session, *, log: LogFn, dry_run: bool = False) -> dict[str, Any]:
    settings = get_settings()
    now = datetime.now(UTC)
    stats: dict[str, Any] = {"due": 0, "sent": 0, "skipped_disabled": 0, "failed": 0}

    rows = db.execute(
        select(CaseInvitation, ClientCase, Agency, ExpatUser)
        .join(ClientCase, ClientCase.id == CaseInvitation.case_id)
        .join(Agency, Agency.id == ClientCase.agency_id)
        .join(ExpatUser, ExpatUser.email == CaseInvitation.email)
        .where(
            CaseInvitation.status == InvitationStatus.PENDING.value,
            # Le lien doit encore VIVRE : relancer vers un lien mort enverrait
            # le client dans le mur (c'est le geste public qui répare ça).
            CaseInvitation.expires_at > now,
            CaseInvitation.activation_reminder_stage < SCHEDULE[-1],
            ExpatUser.activated_at.is_(None),
            ClientCase.deleted_at.is_(None),
            ClientCase.is_demo.is_(False),
        )
        .order_by(CaseInvitation.created_at)
    ).all()

    for invitation, case, agency, expat in rows:
        age_days = (now - invitation.created_at).days
        # Le palier DÛ : le plus élevé atteint, jamais deux mails d'un coup.
        due_stage = max((stage for stage in SCHEDULE if age_days >= stage), default=0)
        if due_stage <= invitation.activation_reminder_stage:
            continue
        stats["due"] += 1
        if not activation_reminders_enabled(agency):
            stats["skipped_disabled"] += 1
            continue
        if dry_run:
            log(f"would remind {invitation.email} (J+{age_days}, stage {due_stage})")
            stats["sent"] += 1
            continue

        lang = resolve_notification_lang_client(expat.preferred_lang)
        journey_name = None
        if case.journey_template_id is not None:
            template = db.get(JourneyTemplate, case.journey_template_id)
            if template is not None:
                journey_name = resolve_i18n(
                    template.name_i18n, lang, agency.default_language, template.name
                )
        content = activation_reminder_email(
            agency.name,
            space_link(settings.frontend_url, f"/space/activate/{invitation.token}", agency.slug),
            max(1, (invitation.expires_at - now).days),
            journey_name,
            lang,
        )
        try:
            send_email(
                invitation.email,
                content.subject,
                content.text,
                content.html,
                sender=sender_as_agency(agency.name),
            )
        except Exception:
            # Le palier n'est PAS avancé : le balayage suivant rejoue.
            logger.exception("activation reminder failed for invitation %s", invitation.id)
            log(f"{invitation.email}: send FAILED, will be retried")
            stats["failed"] += 1
            continue
        invitation.activation_reminder_stage = due_stage
        db.commit()  # par invitation : un crash ne renvoie pas les précédentes
        stats["sent"] += 1
        log(f"activation reminder J+{due_stage} sent to {invitation.email} ({agency.slug})")

    if dry_run:
        stats["dry_run"] = True
    log(
        f"activation sweep: {stats['due']} due, {stats['sent']} sent, "
        f"{stats['skipped_disabled']} disabled, {stats['failed']} failed"
    )
    return stats


def pending_activation_count(db: Session, agency_id: uuid.UUID) -> int:
    """Combien de clients de cette agence n'ont jamais activé, lien encore
    vivant — le chiffre que l'écran agence pourra afficher (non branché)."""
    return len(
        db.execute(
            select(CaseInvitation.id)
            .join(ClientCase, ClientCase.id == CaseInvitation.case_id)
            .join(ExpatUser, ExpatUser.email == CaseInvitation.email)
            .where(
                ClientCase.agency_id == agency_id,
                CaseInvitation.status == InvitationStatus.PENDING.value,
                CaseInvitation.expires_at > datetime.now(UTC) - timedelta(days=0),
                ExpatUser.activated_at.is_(None),
                ClientCase.deleted_at.is_(None),
            )
        ).all()
    )
