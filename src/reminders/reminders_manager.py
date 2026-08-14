import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.expat_user import ExpatUser
from shared.models.message_template import MessageTemplate
from shared.models.reminder import Reminder
from src.activity.activity_manager import ActivityManager
from src.cases.client_space import client_space_is_active
from src.core.config import get_settings
from src.core.email import space_link
from src.core.enums import (
    ActorType,
    RecipientType,
    ReminderChannel,
    ReminderStatus,
    StepStatus,
)
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.core.i18n import (
    format_date_for_lang,
    resolve_i18n,
    resolve_notification_lang_agent,
    resolve_notification_lang_client,
)
from src.reminders.reminder_tokens import (
    AGENCY_TOKENS,
    DEPRECATED_ALIASES,
    RECIPIENT_TOKENS,
    STEP_TOKENS,
    VARIABLE_PATTERN,
    agency_value,
    canonical_names,
    catalogue_index,
    deprecated_tokens,
    render_with_examples,
    unknown_tokens,
    used_tokens,
)
from src.reminders.reminders_repository import RemindersRepository
from src.reminders.reminders_schema import (
    DeprecatedToken,
    MessageTemplateCreateRequest,
    MessageTemplateUpdateRequest,
    ReminderCreateRequest,
    ReminderPreviewRequest,
    ReminderPreviewResponse,
    ReminderResponse,
    ReminderUpdateRequest,
    UnresolvableToken,
)
from src.reminders.reminders_targeting import targeted_member
from src.usage.usage_manager import UsageManager

logger = logging.getLogger(__name__)

# A reminder pinned on a step that is DONE is FALSE, not merely late: its whole
# point is « votre étape n'a pas progressé ». The rule lives here, at the back,
# on EVERY path that leads to a send — unit approve (refuse, below), bulk
# approve (ignore + count), and the dispatch itself (skip + cancel, see
# reminders_jobs). The front already filters the selection; the back must not
# depend on it. Constat du 13/08: 7 of the 85 reminders waiting at
# domiciliation-bulgarie aimed at a step since completed.
_STEP_DONE_REFUSAL = (
    "This reminder targets a step that is already done — a follow-up saying the "
    "step has not progressed would be false. Unlink the step or cancel the reminder."
)


@dataclass(frozen=True)
class _Addressee:
    """QUI lira ce rappel — miroir du routage du dispatch, résolu UNE fois pour
    tous les jetons qui en dépendent (son nom, sa langue, son espace).

    `client` n'est rempli que si le destinataire est un client, c'est-à-dire
    s'il a un espace où aller : un prestataire ou le gestionnaire ont un nom et
    une langue, pas d'espace.

    `undecided` distingue « pas encore choisi » de « introuvable ». L'aperçu
    peut travailler sur un brouillon dont le prestataire n'est pas tranché : on
    ne résout pas, mais on ne refuse pas non plus — le spécimen prend le
    relais. Au FIGEAGE ce cas n'existe pas (`_validate_recipient` exige l'id
    avant tout rendu), donc un nom absent y est une vraie impossibilité."""

    display_name: str | None
    first_name: str | None
    lang: str
    client: ExpatUser | None
    undecided: bool = False


class RemindersManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = RemindersRepository(db)
        self.activity = ActivityManager(db)

    def _log(
        self,
        case_id: uuid.UUID,
        agent: Agent,
        action_type: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.activity.log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            action_type=action_type,
            details=details,
        )

    # --- message templates ----------------------------------------------------------

    async def list_message_templates(self, agent: Agent) -> list[MessageTemplate]:
        return await self.repo.list_message_templates(agent.agency_id)

    async def create_message_template(
        self, agent: Agent, payload: MessageTemplateCreateRequest
    ) -> MessageTemplate:
        template = self.repo.add_message_template(
            agency_id=agent.agency_id,
            name=payload.name,
            body=payload.body,
            # Étiquettes de recherche, jamais des règles : un modèle marqué
            # « email » reste applicable à un WhatsApp.
            language=payload.language,
            channel=payload.channel.value if payload.channel is not None else None,
        )
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def update_message_template(
        self, agent: Agent, template_id: uuid.UUID, payload: MessageTemplateUpdateRequest
    ) -> MessageTemplate:
        template = await self.repo.get_message_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Message template not found.")
        # `exclude_unset` distingue ABSENT (inchangé) de `null` (effacé) —
        # c'est ce qui permet de RETIRER une étiquette. Mais `name` et `body`
        # sont NOT NULL : un `null` explicite sur eux passait la validation
        # et faisait tomber l'insert en 500. On l'ignore, comme une absence.
        patch = payload.model_dump(exclude_unset=True)
        for field, value in patch.items():
            if value is None and field in ("name", "body"):
                continue
            setattr(template, field, value)
        await self.db.commit()
        await self.db.refresh(template)
        return template

    async def delete_message_template(self, agent: Agent, template_id: uuid.UUID) -> None:
        template = await self.repo.get_message_template_in_agency(agent.agency_id, template_id)
        if template is None:
            raise NotFoundError("Message template not found.")
        await self.repo.delete_row(template)
        await self.db.commit()

    # --- interpolation (server-side, at creation/edition) ------------------------------

    async def _targeted_member(
        self, case_id: uuid.UUID, step_progress_id: uuid.UUID | None
    ) -> tuple[CasePerson, ExpatUser] | None:
        """La personne que les exigences EN ATTENTE de l'étape désignent
        SEULE, si elle a un accès utilisable (compte lié + email) — sinon
        None, et le principal reste la voix du dossier.

        UNE vérité, trois lecteurs : le dispatch (`reminders_jobs._recipient`),
        l'écran d'approbation (« sera envoyé à… ») et le figeage, qui doit
        écrire dans la langue de CELLE qui lira."""
        if step_progress_id is None:
            return None
        requirements = await self.repo.list_step_requirements_for_progress(step_progress_id)
        persons = await self.repo.persons_by_id_for_case(case_id)
        person = targeted_member(requirements, persons)
        if person is None or person.expat_user_id is None:
            return None
        member = await self.repo.get_expat(person.expat_user_id)
        if member is None or not member.email:
            return None
        return person, member

    async def _addressee(
        self,
        case: ClientCase,
        step_progress_id: uuid.UUID | None,
        recipient_type: RecipientType,
        recipient_external_id: uuid.UUID | None,
        agency: Agency,
    ) -> _Addressee:
        """Le destinataire RÉEL du rappel — celui que le dispatch servira.

        Trois routes, les mêmes que `reminders_jobs._recipient` :
        - EXTERNAL : le contact du dossier, dans la langue de l'AGENCE (la
          règle du flux manuel : un prestataire ne lit pas dans la langue du
          client) ;
        - AGENT : le gestionnaire, par escalade — le dispatch réécrit
          `recipient_type` sur la ligne, donc une édition ultérieure repasse
          bien par ici ;
        - EXPAT : le membre que l'étape désigne quand elle n'en désigne qu'un
          (routage du 18/07), le principal sinon, dans SA langue à elle."""
        if recipient_type is RecipientType.EXTERNAL:
            lang = resolve_notification_lang_agent(agency.default_language)
            if recipient_external_id is None:
                return _Addressee(None, None, lang, None, undecided=True)
            contact = await self.repo.get_external_contact_in_case(case.id, recipient_external_id)
            if contact is None:
                return _Addressee(None, None, lang, None)
            # Un contact externe n'a QU'UN nom (« Maître Dupont »), et c'est
            # aussi la bonne façon de l'aborder : le prénom retombe dessus
            # plutôt que de découper une chaîne au petit bonheur.
            return _Addressee(contact.name, contact.name, lang, None)

        if recipient_type is RecipientType.AGENT:
            lang = resolve_notification_lang_agent(agency.default_language)
            owner = await self.repo.get_owner_agent(case.id)
            if owner is None:
                return _Addressee(None, None, lang, None)
            return _Addressee(
                f"{owner.first_name} {owner.last_name}".strip(), owner.first_name, lang, None
            )

        hit = await self._targeted_member(case.id, step_progress_id)
        if hit is not None:
            person, member = hit
            # Le nom affiché suit l'écran d'approbation (« sera envoyé à… ») :
            # le nom porté par la personne du dossier d'abord, le compte
            # ensuite. Le prénom, lui, vient du compte — c'est le seul champ
            # qui en soit vraiment un.
            return _Addressee(
                person.full_name or f"{member.first_name} {member.last_name}".strip(),
                member.first_name,
                resolve_notification_lang_client(member.preferred_lang),
                member,
            )
        principal = await self.repo.get_expat(case.principal_expat_user_id)
        assert principal is not None
        return _Addressee(
            f"{principal.first_name} {principal.last_name}".strip(),
            principal.first_name,
            resolve_notification_lang_client(principal.preferred_lang),
            principal,
        )

    async def _resolve_values(
        self,
        case: ClientCase,
        needed: set[str],
        step_progress_id: uuid.UUID | None,
        scheduled_at: datetime,
        recipient_type: RecipientType,
        recipient_external_id: uuid.UUID | None,
    ) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """(valeurs résolues, [(jeton, raison)] non résolubles). NE LÈVE
        JAMAIS : le figeage lève à partir de ce résultat, l'aperçu le sert
        tel quel. UNE seule résolution pour les deux, donc l'aperçu ne peut
        pas flatter ce que le figeage produira — même doctrine que l'aperçu
        des conditions générales.

        Les raisons sont des slugs stables, pas des phrases : le front les
        traduit, il ne les affiche pas brutes."""
        values: dict[str, str] = {}
        failures: list[tuple[str, str]] = []

        # Un alias déprécié se résout PAR son canonique : une seule logique,
        # deux orthographes. La recopie vers l'alias se fait tout en bas.
        canonical = canonical_names(needed)

        if canonical & {"case_client_name", "case_client_first_name"}:
            principal = await self.repo.get_expat(case.principal_expat_user_id)
            assert principal is not None
            if "case_client_name" in canonical:
                values["case_client_name"] = f"{principal.first_name} {principal.last_name}"
            if "case_client_first_name" in canonical:
                values["case_client_first_name"] = principal.first_name

        # L'agence sert trois choses : ses propres jetons, le slug du lien
        # d'espace (blanche-marque) et la langue d'un destinataire qui n'est
        # pas un client. Une seule lecture pour les trois.
        agency: Agency | None = None
        if canonical & (set(AGENCY_TOKENS) | set(RECIPIENT_TOKENS)):
            agency = await self.repo.get_agency(case.agency_id)
            assert agency is not None

        # QUI lira, et dans quelle langue — résolu une fois pour les cinq
        # jetons qui en dépendent : son nom, son prénom, le nom de l'étape et
        # la date d'échéance (leur langue), le lien d'espace (son activation).
        addressee = _Addressee(None, None, "", None)
        if agency is not None and canonical & set(RECIPIENT_TOKENS):
            addressee = await self._addressee(
                case, step_progress_id, recipient_type, recipient_external_id, agency
            )
        lang = addressee.lang

        for name, value in (
            ("recipient_name", addressee.display_name),
            ("recipient_first_name", addressee.first_name),
        ):
            if name not in canonical:
                continue
            if value:
                values[name] = value
            elif not addressee.undecided:
                # Le destinataire est désigné mais introuvable (contact effacé,
                # dossier sans gestionnaire) : écrire « Bonjour  » serait pire
                # qu'un refus. Au figeage ce cas est théorique — la validation
                # du destinataire précède tout rendu.
                failures.append((name, "recipient_unresolvable"))

        if canonical & set(STEP_TOKENS):
            progress = (
                None
                if step_progress_id is None
                else await self.repo.get_progress_in_case(case.id, step_progress_id)
            )
            if progress is None:
                for name in STEP_TOKENS:
                    if name in canonical:
                        failures.append((name, "step_required"))
            else:
                template_step = await self.repo.get_template_step(progress.template_step_id)
                assert template_step is not None
                if "step_name" in canonical:
                    # Le nom d'étape est TRADUIT en base (bloc i18n) : il se
                    # rend dans la langue du destinataire, repli sur la langue
                    # de l'agence puis sur le scalaire — jamais un vide, jamais
                    # une clé brute. (Le scalaire est NOT NULL, donc la chaîne
                    # de replis se termine toujours sur du texte.)
                    assert agency is not None
                    values["step_name"] = (
                        resolve_i18n(
                            template_step.name_i18n,
                            lang,
                            agency.default_language,
                            template_step.name,
                        )
                        or template_step.name
                    )
                if "days_left" in canonical:
                    started_at = await self.repo.get_step_started_at(case.id, progress.id)
                    if template_step.estimated_days is None:
                        failures.append(("days_left", "estimated_days_required"))
                    elif started_at is None:
                        failures.append(("days_left", "step_not_started"))
                    else:
                        elapsed = (scheduled_at.date() - started_at.date()).days
                        values["days_left"] = str(max(0, template_step.estimated_days - elapsed))
                if "step_due_date" in canonical:
                    if progress.due_at is None:
                        # L'échéance FERME est optionnelle sur une étape : sans
                        # elle, ce jeton n'a rien à dire. Même discipline que
                        # {days_left} — on refuse, on ne devine pas (le compteur
                        # estimé est une AUTRE information, pas un repli).
                        failures.append(("step_due_date", "due_date_required"))
                    else:
                        # Le JOUR de l'échéance, lu en UTC comme le compteur
                        # jours-restants des deux faces (`_deadline_counter`) —
                        # une même échéance ne peut pas tomber un jour dans la
                        # timeline et la veille dans la relance.
                        values["step_due_date"] = format_date_for_lang(
                            progress.due_at.astimezone(UTC).date(), lang
                        )

        if "client_space_link" in canonical:
            assert agency is not None
            if addressee.client is None:
                # Un prestataire ou le gestionnaire n'ont pas d'espace client :
                # le lien les mènerait à une page de connexion qu'ils ne
                # passeront pas. On refuse plutôt que de l'écrire.
                failures.append(("client_space_link", "recipient_not_client"))
            elif not client_space_is_active(addressee.client):
                # LA condition du jeton : tant que l'espace n'est pas activé,
                # le lien mène à un mur. Même prédicat que l'exclusion des
                # jobs de notification — l'invitation, elle, est le chemin de
                # CE client-là, et c'est le lot activation qui la porte.
                failures.append(("client_space_link", "client_space_inactive"))
            else:
                values["client_space_link"] = space_link(
                    get_settings().frontend_url, "/space", agency.slug
                )

        if canonical & set(AGENCY_TOKENS):
            assert agency is not None
            for name in AGENCY_TOKENS:
                if name not in canonical:
                    continue
                value = agency_value(name, agency)
                if value is None:
                    # Un champ d'agence vide gèlerait un TROU dans un message
                    # envoyé une fois — même refus qu'une variable de dossier.
                    failures.append((name, "agency_field_empty"))
                else:
                    values[name] = value

        # Les alias dépréciés reçoivent la valeur de leur canonique : le texte
        # que l'agence a enregistré il y a trois semaines rend exactement comme
        # avant, sans qu'une seule ligne de résolution soit dupliquée.
        for alias, target in DEPRECATED_ALIASES.items():
            if alias in needed and target in values:
                values[alias] = values[target]

        # Un refus nomme le jeton que l'agence a ÉCRIT, pas le canonique
        # interne qu'elle ne connaît pas. (Aucun canonique aliasé ne peut
        # échouer aujourd'hui — le titulaire du dossier existe toujours ; la
        # règle est là pour le jour où un alias pointera un jeton qui refuse.)
        alias_of = {target: alias for alias, target in DEPRECATED_ALIASES.items()}
        failures = [
            (alias_of[name], reason) if name not in needed and name in alias_of else (name, reason)
            for name, reason in failures
        ]

        # L'ORDRE DU CATALOGUE décide lequel des refus est nommé par le 422 —
        # pas l'ordre dans lequel ce code résout. Verdict stable, et qui le
        # reste quand un jeton s'ajoute au milieu du catalogue.
        failures.sort(key=lambda failure: catalogue_index(failure[0]))
        return values, failures

    async def _render(
        self,
        case: ClientCase,
        raw: str,
        step_progress_id: uuid.UUID | None,
        scheduled_at: datetime,
        recipient_type: RecipientType,
        recipient_external_id: uuid.UUID | None,
    ) -> str:
        """Freeze the variables into the approved text. {days_left} is
        PROJECTED AT scheduled_at (estimated_days − days between the
        step's start and the planned send date, floor 0) — the approver
        reads a text that is exact AT SEND TIME. Unsolvable variable →
        422 naming it.

        Le DESTINATAIRE entre ici parce que cinq jetons en dépendent :
        {recipient_name} et {recipient_first_name} le nomment, {step_name} et
        {step_due_date} s'écrivent dans sa langue, {client_space_link} n'existe
        que s'il a un espace où aller.

        Un jeton INCONNU ({tva}) n'est pas une erreur ici : il traverse et se
        fige verbatim, comme dans les conditions. C'est l'aperçu qui le
        signale À L'ÉDITION (`preview_reminder`), avant que le texte ne soit
        pris — un message parti ne se rattrape pas."""
        needed = set(VARIABLE_PATTERN.findall(raw))
        if not needed:
            return raw
        values, failures = await self._resolve_values(
            case, needed, step_progress_id, scheduled_at, recipient_type, recipient_external_id
        )
        if failures:
            variable, reason = failures[0]
            raise ValidationError(
                f"{{{variable}}} cannot be resolved ({reason}).",
                code="reminder.variable_unresolvable",
                params={"variable": variable, "reason": reason},
            )

        rendered = raw
        for key, value in values.items():
            rendered = rendered.replace(f"{{{key}}}", value)
        return rendered

    async def preview_reminder(
        self, agent: Agent, payload: ReminderPreviewRequest
    ) -> ReminderPreviewResponse:
        """« Ce que votre client lira », calculé sur le BROUILLON — la MÊME
        résolution que le figeage, donc l'aperçu ne peut pas mentir.

        Avec un dossier en face : les vraies valeurs. Sans (un modèle de
        message, une modale encore vide) : les spécimens du catalogue. Les
        jetons inconnus sont nommés ici parce qu'ils seront GELÉS verbatim,
        et les non-résolubles parce qu'ils lèveraient un 422 au figeage — un
        refus ne doit jamais surprendre au moment d'enregistrer."""
        agency = await self.repo.get_agency(agent.agency_id)
        unknown = unknown_tokens(payload.content)
        needed = set(used_tokens(payload.content))

        values: dict[str, str] = {}
        failures: list[tuple[str, str]] = []
        if payload.case_id is not None and needed:
            case = await self.repo.get_case_in_agency(agent.agency_id, payload.case_id)
            if case is None:
                raise NotFoundError("Case not found.", code="case.not_found")
            values, failures = await self._resolve_values(
                case,
                needed,
                payload.step_progress_id,
                payload.scheduled_at or datetime.now(UTC),
                payload.recipient_type,
                payload.recipient_external_id,
            )

        # Les jetons résolus prennent leur VRAIE valeur ; ceux qui manquent
        # (pas de dossier, ou non résolubles) retombent sur leur spécimen,
        # pour que l'agence lise toujours une phrase entière et pas un trou.
        rendered = payload.content
        for name, value in values.items():
            rendered = rendered.replace("{" + name + "}", value)
        rendered = render_with_examples(rendered, agency)

        return ReminderPreviewResponse(
            rendered=rendered,
            unknown_tokens=unknown,
            unresolvable_tokens=[
                UnresolvableToken(token="{" + name + "}", name=name, reason=reason)
                for name, reason in failures
            ],
            deprecated_tokens=[
                DeprecatedToken(
                    token=token, name=name, resolves_to=resolves_to, suggested=suggested
                )
                for token, name, resolves_to, suggested in deprecated_tokens(payload.content)
            ],
        )

    # --- reminder creation -----------------------------------------------------------------

    async def _validate_recipient(
        self,
        case: ClientCase,
        channel: ReminderChannel | str,
        recipient_type: RecipientType | str,
        recipient_external_id: uuid.UUID | None,
    ) -> None:
        recipient = RecipientType(recipient_type)
        channel_value = ReminderChannel(channel)
        if recipient is RecipientType.EXPAT:
            if recipient_external_id is not None:
                raise ValidationError(
                    "recipient_external_id must be empty for recipient_type 'expat'."
                )
            return
        if recipient_external_id is None:
            raise ValidationError(
                "recipient_external_id is required for recipient_type 'external'."
            )
        contact = await self.repo.get_external_contact_in_case(case.id, recipient_external_id)
        if contact is None:
            raise ValidationError("Recipient external contact must belong to this case.")
        if channel_value is ReminderChannel.MAIL and not contact.email:
            raise ValidationError("The external contact has no email address.")

    async def create_reminder(
        self, agent: Agent, case_id: uuid.UUID, payload: ReminderCreateRequest
    ) -> Reminder:
        case = await self.repo.get_case_in_agency(agent.agency_id, case_id)
        if case is None:
            raise NotFoundError("Case not found.")
        await self._validate_recipient(
            case, payload.channel, payload.recipient_type, payload.recipient_external_id
        )
        if payload.step_progress_id is not None and (
            await self.repo.get_progress_in_case(case.id, payload.step_progress_id) is None
        ):
            raise ValidationError("step_progress_id does not belong to this case.")

        if payload.message_template_id is not None:
            template = await self.repo.get_message_template_in_agency(
                agent.agency_id, payload.message_template_id
            )
            if template is None:
                raise ValidationError("Message template not found in this agency.")
            raw = template.body
        elif payload.message_body is not None:
            raw = payload.message_body
        else:
            raise ValidationError("Either message_template_id or message_body is required.")

        body = await self._render(
            case,
            raw,
            payload.step_progress_id,
            payload.scheduled_at,
            payload.recipient_type,
            payload.recipient_external_id,
        )
        reminder = self.repo.add_reminder(
            case_id=case.id,
            step_progress_id=payload.step_progress_id,
            message_template_id=payload.message_template_id,
            channel=payload.channel.value,
            scheduled_at=payload.scheduled_at,
            recipient_type=payload.recipient_type.value,
            recipient_external_id=payload.recipient_external_id,
            message_body=body,
        )
        await self.db.flush()
        self._log(
            case.id,
            agent,
            "reminder.created",
            {"reminder_id": str(reminder.id), "channel": reminder.channel},
        )
        await UsageManager(self.db).emit_for_case(
            case, "reminder.scheduled", actor_type=ActorType.AGENT, actor_id=agent.id
        )
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    # --- read -------------------------------------------------------------------------------

    async def get_reminder(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        reminder = await self.repo.get_reminder_in_agency(agent.agency_id, reminder_id)
        if reminder is None:
            raise NotFoundError("Reminder not found.")
        return reminder

    async def _targets_a_done_step(self, reminder: Reminder) -> bool:
        """True when the reminder is pinned on a step already validated. A
        reminder without a linked step (a free note, a generic follow-up) is
        never concerned — nothing claims a step stalled."""
        if reminder.step_progress_id is None:
            return False
        progress = await self.repo.get_progress_in_case(reminder.case_id, reminder.step_progress_id)
        return progress is not None and progress.status == StepStatus.DONE.value

    async def list_reminders(
        self, agent: Agent, filters: dict[str, Any], page: int, page_size: int
    ) -> tuple[list[Reminder], int]:
        return await self.repo.list_reminders(agent.agency_id, filters, page, page_size)

    # --- the approval screen says the REAL recipient -------------------------------------

    async def to_response(self, reminder: Reminder) -> ReminderResponse:
        response = ReminderResponse.model_validate(reminder)
        response.resolved_recipient = await self._resolved_recipient(reminder)
        return response

    async def to_responses(self, reminders: list[Reminder]) -> list[ReminderResponse]:
        return [await self.to_response(reminder) for reminder in reminders]

    async def _resolved_recipient(self, reminder: Reminder) -> str | None:
        """Mirror of the dispatch routing (reminders_targeting), for
        DISPLAY: who will actually receive this reminder. Best-effort —
        a resolution hiccup shows None, never a 500 on the calendar."""
        try:
            if reminder.recipient_type == RecipientType.EXTERNAL.value:
                contact = (
                    await self.repo.get_external_contact_in_case(
                        reminder.case_id, reminder.recipient_external_id
                    )
                    if reminder.recipient_external_id
                    else None
                )
                return contact.name if contact is not None else None
            if reminder.recipient_type == RecipientType.AGENT.value:
                return await self.repo.get_owner_display(reminder.case_id)
            # EXPAT: the targeted member with access, else the principal.
            hit = await self._targeted_member(reminder.case_id, reminder.step_progress_id)
            if hit is not None:
                person, member = hit
                return person.full_name or f"{member.first_name} {member.last_name}"
            return await self.repo.get_principal_display(reminder.case_id)
        except Exception:  # noqa: BLE001 — display data, never a 500
            logger.exception("reminder recipient resolution failed for %s", reminder.id)
            return None

    # --- state machine -------------------------------------------------------------------------

    async def update_reminder(
        self, agent: Agent, reminder_id: uuid.UUID, payload: ReminderUpdateRequest
    ) -> Reminder:
        reminder = await self.get_reminder(agent, reminder_id)
        if reminder.status not in (
            ReminderStatus.TO_APPROVE.value,
            ReminderStatus.APPROVED.value,
        ):
            raise ConflictError("Only to_approve or approved reminders can be edited.")
        case = await self.repo.get_case_in_agency(agent.agency_id, reminder.case_id)
        assert case is not None

        data = payload.model_dump(exclude_unset=True)
        was_approved = reminder.status == ReminderStatus.APPROVED.value

        new_channel = data.get("channel", ReminderChannel(reminder.channel))
        new_recipient_type = data.get("recipient_type", RecipientType(reminder.recipient_type))
        new_external_id = data.get("recipient_external_id", reminder.recipient_external_id)
        await self._validate_recipient(case, new_channel, new_recipient_type, new_external_id)

        new_step_id = data.get("step_progress_id", reminder.step_progress_id)
        if new_step_id is not None and (
            await self.repo.get_progress_in_case(case.id, new_step_id) is None
        ):
            raise ValidationError("step_progress_id does not belong to this case.")

        new_scheduled_at = data.get("scheduled_at", reminder.scheduled_at)

        # Re-render source: an explicit body wins; else the (possibly
        # updated) linked template; else the stored body — for free-text
        # reminders its variables are already frozen, re-rendering is a
        # no-op (re-provide the body to refresh them).
        new_template_id = data.get("message_template_id", reminder.message_template_id)
        if "message_body" in data and data["message_body"] is not None:
            raw = data["message_body"]
        elif new_template_id is not None:
            template = await self.repo.get_message_template_in_agency(
                agent.agency_id, new_template_id
            )
            if template is None:
                raise ValidationError("Message template not found in this agency.")
            raw = template.body
        else:
            raw = reminder.message_body

        reminder.channel = ReminderChannel(new_channel).value
        reminder.scheduled_at = new_scheduled_at
        reminder.recipient_type = RecipientType(new_recipient_type).value
        reminder.recipient_external_id = new_external_id
        reminder.step_progress_id = new_step_id
        reminder.message_template_id = new_template_id
        reminder.message_body = await self._render(
            case,
            raw,
            new_step_id,
            new_scheduled_at,
            RecipientType(new_recipient_type),
            new_external_id,
        )

        if was_approved:
            # The approval covered the OLD content — re-approve.
            reminder.status = ReminderStatus.TO_APPROVE.value
            reminder.approved_by_agent_id = None
        self._log(
            case.id,
            agent,
            "reminder.edited",
            {"reminder_id": str(reminder.id), "reapproval_required": was_approved},
        )
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    async def approve_reminder(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        reminder = await self.get_reminder(agent, reminder_id)
        if reminder.status != ReminderStatus.TO_APPROVE.value:
            raise ConflictError("Only to_approve reminders can be approved.")
        # One reminder, one explicit gesture → an explicit answer (409). In
        # bulk the same rule ignores instead, and says how many (the batch of
        # 85 must not fail because 7 of its steps got validated meanwhile).
        if await self._targets_a_done_step(reminder):
            raise ConflictError(_STEP_DONE_REFUSAL)
        reminder.status = ReminderStatus.APPROVED.value
        reminder.approved_by_agent_id = agent.id
        self._log(
            reminder.case_id,
            agent,
            "reminder.approved",
            {"reminder_id": str(reminder.id), "approved_by": str(agent.id)},
        )
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    async def bulk_approve(
        self, agent: Agent, reminder_ids: list[uuid.UUID]
    ) -> tuple[int, int, int]:
        """Approve a batch in ONE gesture — the mirror of bulk_cancel for the
        agency that reads its backlog and wants it to LEAVE. Same bounds, same
        silence on what is not approvable (another agency's ids, a reminder
        already approved / sent / cancelled): `affected` says what moved.

        THE ONE ASYMMETRY: a reminder whose target step is DONE is NOT
        approved. It is counted apart in `skipped_step_done` — ignored, never
        in silence — because a batch of 85 must not be rejected wholesale for
        the 7 whose step got validated in the meantime. They stay TO_APPROVE:
        bulk-cancel is the gesture that clears them.

        Each approval is logged exactly like the unit one — the trace stays
        per-reminder, no « 85 rappels sont partis » hole in the history."""
        rows = await self.repo.list_bulk_targets_in_agency(
            agent.agency_id, reminder_ids, [ReminderStatus.TO_APPROVE.value]
        )
        done_steps = await self.repo.done_progress_ids(
            [r.step_progress_id for r in rows if r.step_progress_id is not None]
        )
        approved = 0
        skipped_step_done = 0
        for reminder in rows:
            if reminder.step_progress_id in done_steps:
                skipped_step_done += 1
                continue
            reminder.status = ReminderStatus.APPROVED.value
            reminder.approved_by_agent_id = agent.id
            self._log(
                reminder.case_id,
                agent,
                "reminder.approved",
                {"reminder_id": str(reminder.id), "approved_by": str(agent.id), "bulk": True},
            )
            approved += 1
        await self.db.commit()
        return len(reminder_ids), approved, skipped_step_done

    async def bulk_cancel(self, agent: Agent, reminder_ids: list[uuid.UUID]) -> tuple[int, int]:
        """Cancel a batch in ONE gesture — the way out of an approval backlog
        that piled up (97 rows in prod at the 13/08 constat, the oldest 17 days
        old). Only this agency's reminders, only cancellable ones; everything
        else is silently ignored, so `affected` may be lower than `examined`.

        Each cancellation is logged exactly like the unit one: the trace stays
        per-reminder, no « 85 rappels ont disparu » hole in the history."""
        rows = await self.repo.list_bulk_targets_in_agency(
            agent.agency_id,
            reminder_ids,
            [ReminderStatus.TO_APPROVE.value, ReminderStatus.APPROVED.value],
        )
        for reminder in rows:
            reminder.status = ReminderStatus.CANCELLED.value
            self._log(
                reminder.case_id,
                agent,
                "reminder.cancelled",
                {"reminder_id": str(reminder.id), "bulk": True},
            )
        await self.db.commit()
        return len(reminder_ids), len(rows)

    async def cancel_reminder(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        reminder = await self.get_reminder(agent, reminder_id)
        if reminder.status not in (
            ReminderStatus.TO_APPROVE.value,
            ReminderStatus.APPROVED.value,
        ):
            raise ConflictError("Only to_approve or approved reminders can be cancelled.")
        reminder.status = ReminderStatus.CANCELLED.value
        self._log(reminder.case_id, agent, "reminder.cancelled", {"reminder_id": str(reminder.id)})
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder

    async def mark_sent(self, agent: Agent, reminder_id: uuid.UUID) -> Reminder:
        """WhatsApp ONLY: the dispatcher never auto-sends this channel;
        the agent copies the rendered text, pastes it in WhatsApp, then
        confirms here. A GET never mutates."""
        reminder = await self.get_reminder(agent, reminder_id)
        if reminder.channel != ReminderChannel.WHATSAPP.value:
            raise ValidationError("mark-sent is only for the whatsapp channel.")
        if reminder.status != ReminderStatus.APPROVED.value:
            raise ConflictError("Only approved reminders can be marked sent.")
        reminder.status = ReminderStatus.SENT.value
        self._log(
            reminder.case_id,
            agent,
            "reminder.sent",
            {"reminder_id": str(reminder.id), "channel": reminder.channel, "manual": True},
        )
        await self.db.commit()
        await self.db.refresh(reminder)
        return reminder
