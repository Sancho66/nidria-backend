from enum import StrEnum


class Audience(StrEnum):
    """Access audience of a `protected_resource` binding.

    PUBLIC is binding-only (route open, no token). AGENT and EXPAT are
    also the two JWT audiences — a token is signed with its audience's
    secret and carries an `audience` claim, so the two flows are not
    interchangeable.
    """

    PUBLIC = "public"
    AGENT = "agent"
    EXPAT = "expat"


class ActorType(StrEnum):
    """Who performed an action (`activity_log.actor_type`,
    `document.uploaded_by_type`). EXTERNAL is used by the consent trace
    (a provider signs external_terms); the activity/document paths still
    record a provider as AGENT (their audience)."""

    AGENT = "agent"
    EXPAT = "expat"
    SYSTEM = "system"
    EXTERNAL = "external"


class CaseStatus(StrEnum):
    PROSPECT = "prospect"
    IN_PROGRESS = "in_progress"
    AWAITING_DOCUMENTS = "awaiting_documents"
    SUBMITTED = "submitted"
    VALIDATED = "validated"
    CLOSED = "closed"


class CaseUrgency(StrEnum):
    """Derived per-case urgency for the Dossiers list, priority-ordered (the
    first that applies wins): a case is OVERDUE if any of its steps is overdue,
    else TO_VALIDATE if any step awaits the agency's validation, else
    AWAITING_CLIENT if its status is AWAITING_DOCUMENTS, else NEUTRAL. Same rule
    as the dashboard worklist, lifted from per-step to per-case (see
    src/cases/case_urgency.py). The enum order == the sort priority."""

    OVERDUE = "overdue"
    TO_VALIDATE = "to_validate"
    AWAITING_CLIENT = "awaiting_client"
    NEUTRAL = "neutral"


class StepStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"


class ResponsibleType(StrEnum):
    """Polymorphic responsible of a step (also used for
    `journey_template_step.default_responsible_type`)."""

    AGENT = "agent"
    EXPAT = "expat"
    EXTERNAL = "external"


class ReminderChannel(StrEnum):
    MAIL = "mail"
    WHATSAPP = "whatsapp"
    IN_APP = "in_app"


class ReminderStatus(StrEnum):
    """Mandatory manual approval: TO_APPROVE → APPROVED → SENT,
    or CANCELLED. No send ever happens before APPROVED."""

    TO_APPROVE = "to_approve"
    APPROVED = "approved"
    SENT = "sent"
    CANCELLED = "cancelled"


class RecipientType(StrEnum):
    """Recipient of a reminder (`reminder.recipient_type`)."""

    EXPAT = "expat"
    EXTERNAL = "external"
    # The case owner (agency side). Set ONLY by the dispatch escalation when
    # an EXTERNAL recipient is unreachable — the owner is derived from
    # client_case.owner_agent_id, so there is no recipient FK for it.
    AGENT = "agent"


class DocValidationStatus(StrEnum):
    OK = "ok"
    INCOMPLETE = "incomplete"
    TO_FIX = "to_fix"


class ExternalContactType(StrEnum):
    NOTARY = "notary"
    LAWYER = "lawyer"
    BANK = "bank"
    TAX_ADVISOR = "tax_advisor"
    OTHER = "other"


class NurtureSendStatus(StrEnum):
    """Outcome of a trial-nurture calendar slot (nurture bloc 3)."""

    SENT = "sent"
    SKIPPED = "skipped"  # slot burned without a send (overtaken / stale)
    PENDING_CONFIG = "pending_config"  # J+28 held: booking URL unset (retried)


class JobRunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobTriggeredBy(StrEnum):
    SCHEDULER = "scheduler"
    MANUAL = "manual"


class InvitationStatus(StrEnum):
    """CANCELLED is a human act (admin withdraws the invitation) and is
    kept distinct from EXPIRED (time ran out, also derivable from
    expires_at) — the audit trail must not lie about the cause."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ClientSpaceState(StrEnum):
    """Can this person reach their client space RIGHT NOW? DERIVED at read
    time (never stored): ACTIVE = the account is activated; PENDING = a live
    invitation link exists; EXPIRED = no usable link left — every case where
    the person must be re-invited (link past `expires_at`, or invitation
    cancelled/absent). NULL on a person without an account at all.

    Deliberately NOT `InvitationStatus`: that one traces the invitation ROW
    (audit), this one answers the agency's question — "why has my client
    never logged in, and can I do something about it?"."""

    ACTIVE = "active"
    PENDING = "pending"
    EXPIRED = "expired"


class AgencySector(StrEnum):
    """The agency's business sector(s) — the SOCLE of the multi-sector
    groundwork. INERT for now: nothing consumes it yet (no sector default,
    no adapted vocabulary, no library filter). Extensible. Notariat lives
    IN `legal` (not separate); 'création de société' is a JOURNEY, not a
    sector."""

    LEGAL = "legal"
    ACCOUNTING = "accounting"
    REAL_ESTATE = "real_estate"
    WEALTH = "wealth"
    HR_MOBILITY = "hr_mobility"
    IMMIGRATION = "immigration"
    CONSULTING = "consulting"


class ContactChannel(StrEnum):
    """A client's PREFERRED contact channel — DISPLAY/preference only, NEVER
    a send router. Reminders keep going out by email regardless (the phone
    field is reused for phone/whatsapp; no second number). Extensible."""

    EMAIL = "email"
    PHONE = "phone"
    WHATSAPP = "whatsapp"


class CasePersonKind(StrEnum):
    """A person attached to a case. Exactly one PRINCIPAL per case
    (the file holder, linked to the shared expat_user for login);
    any number of FAMILY members (no login)."""

    PRINCIPAL = "principal"
    FAMILY = "family"


class Sex(StrEnum):
    MALE = "M"
    FEMALE = "F"
    OTHER = "X"


class MaritalStatus(StrEnum):
    SINGLE = "single"
    MARRIED = "married"
    DIVORCED = "divorced"
    WIDOWED = "widowed"
    PARTNERSHIP = "partnership"


class CustomFieldType(StrEnum):
    """Agency-defined field types — a CLOSED, minimal set. Each has a
    distinct validation rule and frontend renderer. `options` applies
    to SELECT / MULTI_SELECT only."""

    TEXT = "text"
    NUMBER = "number"
    DATE = "date"
    BOOLEAN = "boolean"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    # Refonte adresse/pays (coexistence). COUNTRY = normalized ISO-2
    # country selector (the reusable "mould"); ADDRESS (V2) = a structured
    # {street, city, postal_code, country} whose country sub-field reuses
    # the COUNTRY rule. Values live in case_person.custom_fields JSONB —
    # NEVER on client_case (the canonical origin/dest_country columns and
    # their ecosystem stay intact).
    COUNTRY = "country"
    # ADDRESS (V2) = a structured {street, city, postal_code, country}
    # sub-object; its `country` sub-field reuses the COUNTRY validation
    # (no pattern duplication). Stored in case_person.custom_fields JSONB.
    ADDRESS = "address"


class CompletionMode(StrEnum):
    """How a journey step closes. `agency_validation` (default) = the
    current flow: the agency closes the step. `auto` = capability to
    self-complete when all concrete requirements are met — the active
    trigger lands in a later wave; wave 1 only exposes the state.

    SUPERSEDED by `StepValidatorType` (the "Action validée par" refonte):
    `auto` ⇄ `none`, `agency_validation` ⇄ `agent`. KEPT during the
    transition as a rollback-safe fallback (the migration backfills the
    validator FROM it, never the reverse-loses); a later wave drops it."""

    AUTO = "auto"
    AGENCY_VALIDATION = "agency_validation"


class StepValidatorType(StrEnum):
    """ "Action validée par" — who closes a journey step (symmetric to the
    responsible: a TYPE at the template, the precise person at the dossier).
    Reuses the responsible actor strings so both mechanisms read alike, plus
    `none` = "validated by no one" = the former `auto` self-completion.

    - `none`     → self-completes when all requirements are met (ex-`auto`).
    - `expat`    → the case principal (client) clicks validate.
    - `agent`    → the agency closes (a NAMED internal member, or — agent_id
                   NULL — any member; ex-`agency_validation`, the default).
    - `external` → a DESIGNATED provider (an is_external Agent, stored in
                   `validated_by_agent_id` like the content-wave responsible —
                   a validator must be able to log in and click, so it is an
                   Agent, never a no-login external_contact)."""

    NONE = "none"
    EXPAT = "expat"
    AGENT = "agent"
    EXTERNAL = "external"


class StepParticipantRole(StrEnum):
    """ "Action à réaliser par" — the role a participant plays on a step
    (the responsible refonte: 1 responsible → N participants with roles).
    CLOSED set; the `validator` role is DELIBERATELY absent — validation
    stays on the untouched `validated_by_*` mechanism."""

    EXECUTANT = "executant"  # does the work (the former single responsible)
    PROVIDES_DOCUMENTS = "provides_documents"
    CONTRIBUTOR = "contributor"
    INFORMED = "informed"


class StepRequirementKind(StrEnum):
    BASE_FIELD = "base_field"
    CUSTOM_FIELD = "custom_field"
    DOCUMENT = "document"


class StepRequirementScope(StrEnum):
    PRINCIPAL = "principal"
    EACH_PERSON = "each_person"


class SignatureLevel(StrEnum):
    """eIDAS levels for a signable document requirement. The MODEL accepts
    the three (agnostic groundwork); only SES has an implementation — the
    journeys API refuses aes/qes with an explicit 422 until they do."""

    SES = "ses"
    AES = "aes"
    QES = "qes"


class SignatureRequestStatus(StrEnum):
    """One request = ONE document sent for signature (N signers ride it).
    draft → sent at dispatch; partially_signed when ≥1 signer done;
    completed when all signed (credit consumed); cancelled/expired release
    the credit."""

    DRAFT = "draft"
    SENT = "sent"
    PARTIALLY_SIGNED = "partially_signed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class SignatureSignerStatus(StrEnum):
    PENDING = "pending"
    SIGNED = "signed"
    DECLINED = "declined"


class SignatureCreditKind(StrEnum):
    """Écritures du ledger de crédits signature — append-only, montants
    toujours positifs, le sens est le kind."""

    PURCHASE = "purchase"
    RESERVE = "reserve"
    CONSUME = "consume"
    RELEASE = "release"
    # Crédits OFFERTS par le superadmin (lot 30/07) — un crédit comme un
    # autre au solde (aucune priorité payé/offert), distinct à l'historique.
    GRANT = "grant"


class SignatureProviderKind(StrEnum):
    """The e-signature backends the domain knows BY NAME only — every
    provider detail stays behind the SignatureProvider port."""

    DOCUSEAL = "docuseal"


class RequirementStatus(StrEnum):
    PENDING = "pending"
    PROVIDED = "provided"


class SubscriptionPlan(StrEnum):
    """Product plans (grid nidria.com/#tarifs). Included seats and provider
    caps live in the SEATS_*/PROVIDERS_* constants (agencies_manager); the
    AMOUNTS live in Paddle (PRICE_IDS env) — never in code. Seats have NO
    ceiling on an active subscription (décision Alex + Eric 05/08/2026):
    extra seats are billed per seat, never blocked."""

    CABINET = "cabinet"  # 3 seats included; 10 providers included, cap 15
    AGENCE = "agence"  # 6 seats included; 15 providers included, cap 25
    # Quote-based, manual billing (Eric's PATCH), NO provider cap: absent
    # from the MAX dicts = unlimited. The self-serve checkout refuses it.
    SUR_MESURE = "sur_mesure"


class BillingCycle(StrEnum):
    """Annual = 2 months off + concierge setup + price locked 2 years
    (or as long as the subscription stays continuous). Values follow
    Eric's passation vocabulary."""

    MONTHLY = "mensuel"
    ANNUAL = "annuel"


class ConsentDocumentType(StrEnum):
    """Legal documents subject to BLOCKING consent (point 16). The two
    agency documents bind the AGENCY and are accepted once per agency
    admin; the two client documents bind the client PER AGENCY (the
    agency is the data controller, Nidria the processor); the external
    document binds a PROVIDER for the agency whose portal they enter."""

    AGENCY_TERMS = "agency_terms"  # CGV Nidria (agency face)
    AGENCY_DPA = "agency_dpa"  # data processing agreement (agency face)
    CLIENT_TERMS = "client_terms"  # CGU of the client space
    CLIENT_PRIVACY = "client_privacy"  # privacy notice of the client space
    # Unified provider access terms (confidentiality + usage + GDPR),
    # accepted per agency the provider works for (provider face).
    EXTERNAL_TERMS = "external_terms"


# Required set per audience: what the consent gate demands (the latest
# ACTIVE version of each type) before opening the rest of the API.
AGENT_CONSENT_TYPES: frozenset[str] = frozenset(
    {ConsentDocumentType.AGENCY_TERMS.value, ConsentDocumentType.AGENCY_DPA.value}
)
EXPAT_CONSENT_TYPES: frozenset[str] = frozenset(
    {ConsentDocumentType.CLIENT_TERMS.value, ConsentDocumentType.CLIENT_PRIVACY.value}
)
EXTERNAL_CONSENT_TYPES: frozenset[str] = frozenset({ConsentDocumentType.EXTERNAL_TERMS.value})
