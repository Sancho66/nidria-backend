import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import RecipientType, ReminderChannel
from src.core.i18n import Language


class MessageTemplateCreateRequest(BaseModel):
    """`language` et `channel` sont des ÉTIQUETTES pour retrouver un modèle
    dans une liste qui grandit — jamais des règles : elles ne filtrent ni ne
    contraignent la création d'un rappel. None = non étiqueté."""

    name: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)
    language: Language | None = None
    channel: ReminderChannel | None = None


class MessageTemplateUpdateRequest(BaseModel):
    """PATCH partiel. Une étiquette se RETIRE en envoyant `null`
    explicitement — d'où `exclude_unset` côté manager, qui distingue
    « absent » (inchangé) de « null » (effacé)."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    body: str | None = Field(default=None, min_length=1)
    language: Language | None = None
    channel: ReminderChannel | None = None


class MessageTemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    body: str
    language: str | None = None
    channel: str | None = None
    # L'écran de gestion trie et date les modèles — servi, jamais dérivé.
    created_at: datetime
    updated_at: datetime


class ReminderPreviewRequest(BaseModel):
    """Le BROUILLON en cours d'écriture (jamais persisté ici). `case_id`
    optionnel : avec lui l'aperçu rend les VRAIES valeurs, sans lui les
    spécimens du catalogue — écrire un modèle de message n'exige pas
    d'avoir un client sous la main."""

    content: str = Field(min_length=1, max_length=20_000)
    case_id: uuid.UUID | None = None
    step_progress_id: uuid.UUID | None = None
    # La date d'envoi PROJETÉE : {days_left} en dépend (il compte à partir
    # d'elle, pas d'aujourd'hui). Absente → maintenant.
    scheduled_at: datetime | None = None
    # Le destinataire prévu — deux jetons en dépendent : {step_due_date}
    # s'écrit dans SA langue, {client_space_link} n'existe que s'il a un
    # espace actif. Défaut `expat`, le destinataire de la modale ; sans ce
    # champ l'aperçu flatterait le figeage sur un rappel adressé ailleurs.
    recipient_type: RecipientType = RecipientType.EXPAT


class UnresolvableToken(BaseModel):
    """Un jeton CONNU que le figeage refuserait. `reason` est un slug stable
    (`step_required`, `estimated_days_required`, `step_not_started`,
    `due_date_required`, `recipient_not_client`, `client_space_inactive`,
    `agency_field_empty`) : le front le traduit, il ne l'affiche pas brut."""

    token: str
    name: str
    reason: str


class ReminderPreviewResponse(BaseModel):
    """Ce que le client lira, plus les deux signaux d'ÉDITION."""

    rendered: str
    # Jetons que personne ne résout : ils seront GELÉS TELS QUELS dans le
    # message envoyé. Nommés ici parce qu'après le figeage il est trop tard.
    unknown_tokens: list[str] = []
    # Jetons connus qui feraient lever un 422 `reminder.variable_unresolvable`
    # à l'enregistrement. Servis pour que ce refus ne surprenne jamais.
    unresolvable_tokens: list[UnresolvableToken] = []


class ReminderCreateRequest(BaseModel):
    """Body source: message_template_id OR free-text message_body (at
    least one). Variables are interpolated SERVER-side at creation —
    the approver reads the exact final text."""

    channel: ReminderChannel
    scheduled_at: datetime
    recipient_type: RecipientType
    recipient_external_id: uuid.UUID | None = None
    message_template_id: uuid.UUID | None = None
    message_body: str | None = Field(default=None, min_length=1)
    step_progress_id: uuid.UUID | None = None


class ReminderUpdateRequest(BaseModel):
    """Any edit of an APPROVED reminder sends it back to TO_APPROVE —
    the approval covers exactly what goes out."""

    channel: ReminderChannel | None = None
    scheduled_at: datetime | None = None
    recipient_type: RecipientType | None = None
    recipient_external_id: uuid.UUID | None = None
    message_template_id: uuid.UUID | None = None
    message_body: str | None = Field(default=None, min_length=1)
    step_progress_id: uuid.UUID | None = None


class ReminderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    case_id: uuid.UUID
    step_progress_id: uuid.UUID | None
    message_template_id: uuid.UUID | None
    channel: str
    scheduled_at: datetime
    status: str
    recipient_type: str
    recipient_external_id: uuid.UUID | None
    message_body: str
    approved_by_agent_id: uuid.UUID | None
    auto_threshold_days: int | None
    # The REAL recipient the dispatch will resolve ("sera envoye a Claire
    # Martin") — routing 2026-07-18: an EXPAT reminder whose step targets
    # one member with access goes to HER; the approval screen must say it.
    # Display name (member full_name, principal name, contact name, owner
    # email) — None only when nothing is resolvable (defensive).
    resolved_recipient: str | None = None
    created_at: datetime
    updated_at: datetime


class ReminderListResponse(BaseModel):
    items: list[ReminderResponse]
    total: int
    page: int
    page_size: int


# Same cap and shape as the cases bulk actions (house pattern): explicit ids,
# never a blind "cancel everything" — the screen sends what it shows.
_BULK_MAX_IDS = 500


class ReminderBulkCancelRequest(BaseModel):
    """Annulation en masse — le geste de sortie du passif d'approbation (97
    relances en attente en prod au 13/08, la plus vieille de 17 jours). Les
    ids d'une AUTRE agence, ou d'un rappel déjà envoyé, sont ignorés
    silencieusement : `affected` dit ce qui a bougé, jamais une fuite."""

    reminder_ids: list[uuid.UUID] = Field(min_length=1, max_length=_BULK_MAX_IDS)


class ReminderBulkCancelResponse(BaseModel):
    examined: int
    affected: int


class ReminderBulkApproveRequest(BaseModel):
    """Approbation en masse — le miroir exact de l'annulation : mêmes bornes
    (1..500 ids explicites), même silence sur ce qui n'est pas approuvable
    (id d'une autre agence, rappel déjà approuvé, envoyé ou annulé).

    UNE règle en plus, qui n'existe pas côté annulation : un rappel dont
    l'ÉTAPE CIBLE est terminée n'est pas approuvé. « Votre étape n'a pas
    progressé » sur une étape validée est faux, pas seulement tardif (7 des
    85 relances en attente chez domiciliation-bulgarie au constat du 13/08).
    En masse on l'ignore — mais jamais en silence : `skipped_step_done` le
    compte à part, l'écran peut le dire. À l'unité, `POST /reminders/{id}/
    approve` REFUSE (409) : un geste explicite mérite une réponse explicite."""

    reminder_ids: list[uuid.UUID] = Field(min_length=1, max_length=_BULK_MAX_IDS)


class ReminderBulkApproveResponse(BaseModel):
    examined: int
    affected: int
    # Toujours présent, 0 compris (même discipline que `skipped_no_client_space`
    # du job auto) : une clé qui n'apparaît que quand elle se déclenche se lit
    # comme une anomalie.
    skipped_step_done: int
