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
    `document.uploaded_by_type`)."""

    AGENT = "agent"
    EXPAT = "expat"
    SYSTEM = "system"


class CaseStatus(StrEnum):
    PROSPECT = "prospect"
    IN_PROGRESS = "in_progress"
    AWAITING_DOCUMENTS = "awaiting_documents"
    SUBMITTED = "submitted"
    VALIDATED = "validated"
    CLOSED = "closed"


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


class CompletionMode(StrEnum):
    """How a journey step closes. `agency_validation` (default) = the
    current flow: the agency closes the step. `auto` = capability to
    self-complete when all concrete requirements are met — the active
    trigger lands in a later wave; wave 1 only exposes the state."""

    AUTO = "auto"
    AGENCY_VALIDATION = "agency_validation"


class StepRequirementKind(StrEnum):
    BASE_FIELD = "base_field"
    CUSTOM_FIELD = "custom_field"
    DOCUMENT = "document"


class StepRequirementScope(StrEnum):
    PRINCIPAL = "principal"
    EACH_PERSON = "each_person"


class RequirementStatus(StrEnum):
    PENDING = "pending"
    PROVIDED = "provided"
