"""AI-journey import (POST /journeys/import) — DETERMINISTIC.

The agency runs a Nidria-provided prompt in ITS OWN AI and pastes the
JSON back; this module interprets that JSON into a journey template in
milliseconds. ZERO network/LLM call server-side, single transaction.

Arbitrated v1 perimeter (Alexandre, 2026-07-07, on Eric's spec):
- steps only: `informations_creation` (volet A) is ACCEPTED in the JSON
  but IGNORED with a report mention (the section packs cover intake);
- every collected field becomes a CUSTOM field (no catalog matching);
- abstract actors, never invented identities: `client` maps to the
  expat participant, `agence` to the "agency in general" participant
  (agent_id NULL) flagged "to assign", and `prestataire:<job>` becomes
  a TYPED SLOT in the report (the template participant model requires
  a real provider Agent, so nothing is created for it);
- creation only, no re-import; `pieces_jointes` ignored with mention
  (a JSON carries no file);
- personal-data detection (email / long digit run / precise date in
  labels) WARNS, never rejects;
- Postel tolerance on labels (2026-07-07): AIs naturally emit plain
  strings where the schema expects multilingual objects - a string is
  accepted anywhere a label is expected and normalized to
  {langue_par_defaut | fr: string} BEFORE validation (the fr-required
  rule applies AFTER, unchanged); any other type keeps the exact-path
  rejection. A soft warning counts the normalized labels. Select
  OPTIONS additionally accept the RICH form {valeur|value|cle|key,
  libelle|label} AIs produce spontaneously: it collapses to its label
  (the options storage is a plain list[str] - verified - so the
  technical key has nowhere to live and is dropped cleanly).

Validation is two-tiered: a violation INSIDE a step rejects that step
(partial import, prerequisite dependents cascade-rejected with
mention), while a globally invalid JSON (no fr name, zero valid step,
prerequisite cycle) raises a 422 whose import_ai.* code + {chemin,
valeur} params the front renders in the agency's language."""

import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from shared.models.agent import Agent
from shared.models.custom_field import CustomFieldDefinition
from shared.models.journey import (
    JourneyStepParticipant,
    JourneyTemplate,
    JourneyTemplateField,
    JourneyTemplateStep,
    StepPrerequisite,
)
from shared.models.step_requirement import StepRequirement
from src.core.enums import (
    ActorType,
    CustomFieldType,
    StepParticipantRole,
    StepRequirementKind,
    StepRequirementScope,
    StepValidatorType,
)
from src.core.exceptions import ValidationError
from src.core.i18n import SUPPORTED_LANGUAGES, normalize_i18n_input
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.journeys.journeys_manager import JourneysManager
from src.journeys.journeys_repository import JourneysRepository
from src.journeys.journeys_schema import (
    AssignableProvider,
    ImportExternalSlot,
    ImportParticipantsSummary,
    ImportStepCreated,
    ImportStepIgnored,
    ImportWarningItem,
    JourneyImportReport,
)
from src.usage.usage_manager import UsageManager

# The CLOSED enums of the JSON format (Eric's spec) -> internal values.
_FIELD_TYPES: dict[str, str] = {
    "text": CustomFieldType.TEXT.value,
    "number": CustomFieldType.NUMBER.value,
    "date": CustomFieldType.DATE.value,
    "boolean": CustomFieldType.BOOLEAN.value,
    "select_single": CustomFieldType.SELECT.value,
    "select_multi": CustomFieldType.MULTI_SELECT.value,
    "country": CustomFieldType.COUNTRY.value,
    "address": CustomFieldType.ADDRESS.value,
}
_ROLES: dict[str, str] = {
    "executant": StepParticipantRole.EXECUTANT.value,
    "fournit_documents": StepParticipantRole.PROVIDES_DOCUMENTS.value,
    "contributeur": StepParticipantRole.CONTRIBUTOR.value,
    "informe": StepParticipantRole.INFORMED.value,
}
_VALIDATORS: dict[str, str] = {
    "agence": StepValidatorType.AGENT.value,
    "personne": StepValidatorType.NONE.value,
}
_PROVIDER_ACTOR = re.compile(r"^prestataire:(.+)$")

# Personal-data heuristics (warnings, NEVER blocking): an email, a long
# digit run (passport/phone-like), a precise date in a label points at a
# real dossier leaking into what must stay a generic model.
_PII_PATTERNS = (
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"\d{6,}"),
    re.compile(r"\b\d{1,2}[/.]\d{1,2}[/.]\d{4}\b|\b\d{4}-\d{2}-\d{2}\b"),
)


def _slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", ascii_text.lower()).strip("_")
    return slug[:50]


@dataclass
class _Field:
    key: str
    field_type: str
    required: bool
    label: dict[str, str]
    options: list[str] | None
    chemin: str = ""
    final_key: str = ""  # resolved against the agency definitions (planning pass)


@dataclass
class _Step:
    ref: str
    index: int
    name: dict[str, str]
    estimated_days: int | None
    validator: str
    prerequisites: list[str]
    participant_roles: list[tuple[str, str]]  # (actor_kind, role) actor_kind: client|agency
    provider_jobs: list[tuple[str, str]]  # (job, role) - report slots only
    fields: list[_Field] = field(default_factory=list)
    note: dict[str, str] = field(default_factory=dict)


class _StepInvalid(Exception):
    """Internal: rejects ONE step (partial import), never the request."""

    def __init__(self, code: str, chemin: str, valeur: str | None = None) -> None:
        self.code = code
        self.chemin = chemin
        self.valeur = valeur


class JourneyImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._default_lang = "fr"
        self._normalized = 0  # Postel: plain-string labels accepted, counted

    # --- parsing helpers ------------------------------------------------------------

    def _coerce_label(self, value: Any) -> Any:
        """Postel tolerance: a plain string where a multilingual object is
        expected becomes {langue_par_defaut: string}. Anything else is
        returned untouched (the exact-path rejection stays)."""
        if isinstance(value, str) and value.strip():
            self._normalized += 1
            return {self._default_lang: value}
        return value

    def _coerce_option(self, value: Any) -> Any:
        """Single normalization point for select OPTIONS, on top of
        `_coerce_label`: the rich form {valeur|value|cle|key,
        libelle|label} collapses to its label (the technical key is
        dropped - `custom_field_definition.options` stores plain
        strings). Anything without a label keeps the exact-path
        rejection downstream."""
        if isinstance(value, dict) and ("libelle" in value or "label" in value):
            inner = value.get("libelle", value.get("label"))
            self._normalized += 1
            if isinstance(inner, str) and inner.strip():
                return {self._default_lang: inner}
            return inner
        return self._coerce_label(value)

    def _label(self, value: Any, chemin: str, *, require_fr: bool) -> dict[str, str]:
        value = self._coerce_label(value)
        if not isinstance(value, dict):
            raise _StepInvalid("import_ai.label_invalid", chemin, str(value)[:80])
        blob = normalize_i18n_input({k: v for k, v in value.items() if isinstance(v, str)})
        if require_fr and not blob.get("fr"):
            raise _StepInvalid("import_ai.label_fr_missing", f"{chemin}.fr")
        return blob

    def _parse_field(self, raw: Any, chemin: str) -> _Field:
        if not isinstance(raw, dict):
            raise _StepInvalid("import_ai.field_invalid", chemin, str(raw)[:80])
        label = self._label(raw.get("libelle"), f"{chemin}.libelle", require_fr=True)
        raw_type = raw.get("type")
        if raw_type not in _FIELD_TYPES:
            raise _StepInvalid("import_ai.invalid_field_type", f"{chemin}.type", str(raw_type))
        field_type = _FIELD_TYPES[raw_type]
        options: list[str] | None = None
        if field_type in (CustomFieldType.SELECT.value, CustomFieldType.MULTI_SELECT.value):
            raw_options = raw.get("options")
            if not isinstance(raw_options, list) or not raw_options:
                raise _StepInvalid("import_ai.select_options_missing", f"{chemin}.options")
            options = []
            for i, raw_option in enumerate(raw_options):
                option_label = self._label(
                    self._coerce_option(raw_option), f"{chemin}.options[{i}]", require_fr=True
                )
                if option_label["fr"] not in options:  # storage wants unique strings
                    options.append(option_label["fr"])
        key = _slugify(str(raw.get("cle") or "")) or _slugify(label["fr"]) or "champ"
        return _Field(
            key=key,
            field_type=field_type,
            required=bool(raw.get("requis", False)),
            label=label,
            options=options,
            chemin=chemin,
        )

    def _parse_step(self, raw: Any, index: int, refs_taken: set[str]) -> _Step:
        chemin = f"parcours.etapes[{index}]"
        if not isinstance(raw, dict):
            raise _StepInvalid("import_ai.step_invalid", chemin, str(raw)[:80])
        ref = str(raw.get("ref") or f"etape_{index + 1}")
        if ref in refs_taken:
            raise _StepInvalid("import_ai.duplicate_ref", f"{chemin}.ref", ref)
        try:
            name = self._label(raw.get("nom"), f"{chemin}.nom", require_fr=True)
        except _StepInvalid as exc:
            raise _StepInvalid("import_ai.step_name_missing", exc.chemin, exc.valeur) from exc

        estimated_days: int | None = None
        raw_delay = raw.get("delai_jours")
        if raw_delay is not None:
            if isinstance(raw_delay, bool) or not isinstance(raw_delay, int) or raw_delay < 0:
                raise _StepInvalid(
                    "import_ai.invalid_delay", f"{chemin}.delai_jours", str(raw_delay)
                )
            estimated_days = raw_delay

        raw_validator = raw.get("validee_par", "agence")
        if raw_validator not in _VALIDATORS:
            raise _StepInvalid(
                "import_ai.invalid_validator", f"{chemin}.validee_par", str(raw_validator)
            )

        participant_roles: list[tuple[str, str]] = []
        provider_jobs: list[tuple[str, str]] = []
        raw_participants = raw.get("participants") or []
        if not isinstance(raw_participants, list):
            raise _StepInvalid(
                "import_ai.invalid_actor", f"{chemin}.participants", str(raw_participants)[:80]
            )
        for i, raw_participant in enumerate(raw_participants):
            p_chemin = f"{chemin}.participants[{i}]"
            if not isinstance(raw_participant, dict):
                raise _StepInvalid("import_ai.invalid_actor", p_chemin, str(raw_participant)[:80])
            raw_role = raw_participant.get("role")
            if raw_role not in _ROLES:
                raise _StepInvalid("import_ai.invalid_role", f"{p_chemin}.role", str(raw_role))
            actor = str(raw_participant.get("acteur") or "")
            provider = _PROVIDER_ACTOR.match(actor)
            if actor in ("client", "agence"):
                kind = "client" if actor == "client" else "agency"
                if (kind, _ROLES[raw_role]) not in participant_roles:
                    participant_roles.append((kind, _ROLES[raw_role]))
            elif provider and provider.group(1).strip():
                job = _slugify(provider.group(1)) or provider.group(1).strip().lower()
                provider_jobs.append((job, _ROLES[raw_role]))
            else:
                raise _StepInvalid("import_ai.invalid_actor", f"{p_chemin}.acteur", actor)

        prerequisites = raw.get("prerequis") or []
        if not isinstance(prerequisites, list) or not all(
            isinstance(p, str) for p in prerequisites
        ):
            raise _StepInvalid(
                "import_ai.unknown_prerequisite", f"{chemin}.prerequis", str(prerequisites)[:80]
            )

        step = _Step(
            ref=ref,
            index=index,
            name=name,
            estimated_days=estimated_days,
            validator=_VALIDATORS[raw_validator],
            prerequisites=list(dict.fromkeys(prerequisites)),
            participant_roles=participant_roles,
            provider_jobs=provider_jobs,
        )
        for i, raw_field in enumerate(raw.get("informations_a_collecter") or []):
            step.fields.append(
                self._parse_field(raw_field, f"{chemin}.informations_a_collecter[{i}]")
            )
        provided = raw.get("informations_fournies")
        if isinstance(provided, dict) and provided.get("note") is not None:
            step.note = self._label(
                provided["note"], f"{chemin}.informations_fournies.note", require_fr=False
            )
        return step

    # --- the entry point -------------------------------------------------------------

    async def _resolve_providers(
        self, agent: Agent, provider_assignments: dict[str, uuid.UUID] | None
    ) -> dict[str, Agent]:
        """Validate {job: agent_id}: each target must be an EXTERNAL of
        THIS agency (422 otherwise). Returns job -> external Agent."""
        resolved: dict[str, Agent] = {}
        for job, agent_id in (provider_assignments or {}).items():
            target = await JourneysRepository(self.db).get_agent_in_agency(
                agent.agency_id, agent_id
            )
            if target is None or not target.is_external:
                raise ValidationError(
                    "The assigned provider is not an external of this agency.",
                    code="import_ai.provider_not_assignable",
                    params={"job": job, "agent_id": str(agent_id)},
                )
            resolved[job] = target
        return resolved

    async def _assignable_providers(self, agent: Agent) -> list[AssignableProvider]:
        """The agency's external providers the front can pick per slot
        (same list for every slot: no reliable job -> role match, the
        agency chooses). One query, role eager-loaded."""
        rows = (
            await self.db.execute(
                select(Agent)
                .where(Agent.agency_id == agent.agency_id, Agent.is_external.is_(True))
                .options(selectinload(Agent.role))
                .order_by(Agent.first_name, Agent.last_name)
            )
        ).scalars()
        return [
            AssignableProvider(
                agent_id=a.id,
                name=f"{a.first_name} {a.last_name}".strip(),
                role=a.role.name if a.role else "",
            )
            for a in rows
        ]

    async def run(
        self,
        agent: Agent,
        parcours: Any,
        *,
        preview: bool,
        provider_assignments: dict[str, uuid.UUID] | None = None,
    ) -> JourneyImportReport:
        resolved_providers = await self._resolve_providers(agent, provider_assignments)
        if not isinstance(parcours, dict):
            raise ValidationError(
                "The import payload must carry a 'parcours' object.",
                code="import_ai.invalid_structure",
                params={"chemin": "parcours"},
            )
        warnings: list[ImportWarningItem] = []
        pii_texts: list[tuple[str, str]] = []

        raw_default = parcours.get("langue_par_defaut")
        self._default_lang = raw_default if raw_default in SUPPORTED_LANGUAGES else "fr"
        self._normalized = 0

        raw_name = self._coerce_label(parcours.get("nom"))
        name_blob = (
            normalize_i18n_input({k: v for k, v in raw_name.items() if isinstance(v, str)})
            if isinstance(raw_name, dict)
            else {}
        )
        if not name_blob.get("fr"):
            raise ValidationError(
                "The journey needs a French name (parcours.nom.fr).",
                code="import_ai.name_missing",
                params={"chemin": "parcours.nom.fr"},
            )
        for lang, text in name_blob.items():
            pii_texts.append((f"parcours.nom.{lang}", text))

        # Volet A: ACCEPTED in the JSON, IGNORED in v1 (section packs
        # cover intake) - mention, never an error.
        if parcours.get("informations_creation"):
            warnings.append(
                ImportWarningItem(
                    code="import_ai.sections_ignored", chemin="parcours.informations_creation"
                )
            )

        raw_steps = parcours.get("etapes")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValidationError(
                "The journey needs at least one step.",
                code="import_ai.no_steps",
                params={"chemin": "parcours.etapes"},
            )

        # --- tier 1: per-step validation (partial import) --------------------------
        valid: list[_Step] = []
        ignored: list[ImportStepIgnored] = []
        refs_taken: set[str] = set()
        for index, raw_step in enumerate(raw_steps):
            try:
                step = self._parse_step(raw_step, index, refs_taken)
            except _StepInvalid as exc:
                ref = raw_step.get("ref") if isinstance(raw_step, dict) else None
                ignored.append(
                    ImportStepIgnored(
                        ref=str(ref) if ref else None,
                        code=exc.code,
                        chemin=exc.chemin,
                        valeur=exc.valeur,
                    )
                )
                continue
            refs_taken.add(step.ref)
            valid.append(step)
            provided = raw_step.get("informations_fournies") if isinstance(raw_step, dict) else None
            if isinstance(provided, dict) and provided.get("pieces_jointes"):
                warnings.append(
                    ImportWarningItem(
                        code="import_ai.attachments_ignored",
                        chemin=f"parcours.etapes[{step.index}].informations_fournies"
                        ".pieces_jointes",
                    )
                )

        # Unknown prerequisite refs (never declared) reject their step;
        # refs pointing at a REJECTED step cascade with a mention.
        declared_refs = {s.ref for s in valid} | {i.ref for i in ignored if i.ref}
        changed = True
        while changed:
            changed = False
            kept_refs = {s.ref for s in valid}
            for step in list(valid):
                for ref in step.prerequisites:
                    if ref not in declared_refs:
                        code, valeur = "import_ai.unknown_prerequisite", ref
                    elif ref not in kept_refs:
                        code, valeur = "import_ai.prerequisite_rejected", ref
                    else:
                        continue
                    valid.remove(step)
                    ignored.append(
                        ImportStepIgnored(
                            ref=step.ref,
                            code=code,
                            chemin=f"parcours.etapes[{step.index}].prerequis",
                            valeur=valeur,
                        )
                    )
                    changed = True
                    break

        # --- tier 2: global coherence (422) -----------------------------------------
        if not valid:
            first = ignored[0]
            raise ValidationError(
                "No valid step in the imported journey.",
                code=first.code if ignored else "import_ai.no_steps",
                params={"chemin": first.chemin, "valeur": first.valeur},
            )
        self._reject_cycles(valid)

        # --- warnings: personal data + participants to finalize ---------------------
        for step in valid:
            base = f"parcours.etapes[{step.index}]"
            for lang, text in step.name.items():
                pii_texts.append((f"{base}.nom.{lang}", text))
            for lang, text in step.note.items():
                pii_texts.append((f"{base}.informations_fournies.note.{lang}", text))
            for i, parsed in enumerate(step.fields):
                for lang, text in parsed.label.items():
                    pii_texts.append((f"{base}.informations_a_collecter[{i}].libelle.{lang}", text))
                for option in parsed.options or []:
                    pii_texts.append((f"{base}.informations_a_collecter[{i}].options", option))
        for chemin, text in pii_texts:
            for pattern in _PII_PATTERNS:
                match = pattern.search(text)
                if match:
                    warnings.append(
                        ImportWarningItem(
                            code="import_ai.personal_data_suspected",
                            chemin=chemin,
                            valeur=match.group(0)[:60],
                        )
                    )
                    break

        # Postel: soft mention when single-language labels were accepted.
        if self._normalized:
            warnings.append(
                ImportWarningItem(code="import_ai.labels_normalized", valeur=str(self._normalized))
            )

        # Field-key planning (shared by preview and creation): reuse an
        # existing agency definition when the type matches, otherwise a
        # SUFFIXED key (an agency definition is never mutated, no catalog
        # matching - arbitrated v1). Conflicts surface as warnings.
        defs_plan, defs_position = await self._plan_field_keys(agent, valid, warnings)

        agency_steps = [s.ref for s in valid if any(k == "agency" for k, _ in s.participant_roles)]
        if agency_steps:
            warnings.append(
                ImportWarningItem(
                    code="import_ai.agency_participants_to_assign",
                    valeur=", ".join(agency_steps),
                )
            )
        # A job with a provided assignment is RESOLVED: it becomes a real
        # participant (see _create) and drops out of the slots/warnings.
        slots: dict[str, list[str]] = {}
        for step in valid:
            for job, _role in step.provider_jobs:
                if job in resolved_providers:
                    continue
                slots.setdefault(job, [])
                if step.ref not in slots[job]:
                    slots[job].append(step.ref)
        for job, step_refs in sorted(slots.items()):
            warnings.append(
                ImportWarningItem(
                    code="import_ai.external_provider_to_name",
                    valeur=f"{job}: {', '.join(step_refs)}",
                )
            )

        assignable = await self._assignable_providers(agent) if slots else []
        participants = ImportParticipantsSummary(
            client=sum(1 for s in valid for k, _ in s.participant_roles if k == "client"),
            agency=sum(1 for s in valid for k, _ in s.participant_roles if k == "agency"),
            external_slots=[
                ImportExternalSlot(job=job, steps=refs, assignable=assignable)
                for job, refs in sorted(slots.items())
            ],
        )
        report = JourneyImportReport(
            template_id=None,
            name=name_blob["fr"],
            created=False,
            steps_created=[
                ImportStepCreated(ref=s.ref, name=s.name["fr"], position=i, fields=len(s.fields))
                for i, s in enumerate(valid)
            ],
            steps_ignored=ignored,
            participants=participants,
            warnings=warnings,
        )
        if preview:
            return report

        report.template_id = await self._create(
            agent, name_blob, valid, defs_plan, defs_position, resolved_providers, len(warnings)
        )
        report.created = True
        return report

    async def _plan_field_keys(
        self, agent: Agent, steps: list[_Step], warnings: list[ImportWarningItem]
    ) -> tuple[dict[str, _Field], int]:
        """Resolve every collected field to its FINAL agency key (pure
        read). An existing ACTIVE definition of the same type is reused;
        anything else taken (other type, archived) pushes to a suffixed
        key. Returns (definitions to create by final key, next position
        in the agency's definition ordering)."""
        existing = {
            d.key: d
            for d in await CustomFieldsRepository(self.db).list_for_agency(
                agent.agency_id, include_archived=True
            )
        }
        next_position = max((d.position for d in existing.values()), default=-1) + 1
        to_create: dict[str, _Field] = {}
        for step in steps:
            for parsed in step.fields:
                key = parsed.key
                current = existing.get(key)
                planned = to_create.get(key)
                reusable = (planned is not None and planned.field_type == parsed.field_type) or (
                    current is not None
                    and current.field_type == parsed.field_type
                    and current.archived_at is None
                )
                if not reusable and (current is not None or planned is not None):
                    base, n = key, 2
                    while key in existing or key in to_create:
                        key = f"{base[:46]}_{n}"
                        n += 1
                    warnings.append(
                        ImportWarningItem(
                            code="import_ai.field_key_conflict",
                            chemin=f"{parsed.chemin}.cle",
                            valeur=f"{base} > {key}",
                        )
                    )
                    reusable = False
                if not reusable and key not in existing:
                    to_create.setdefault(key, parsed)
                parsed.final_key = key
        return to_create, next_position

    def _reject_cycles(self, valid: list[_Step]) -> None:
        """Kahn over the kept steps: leftovers = a dependency cycle."""
        remaining = {s.ref: set(s.prerequisites) for s in valid}
        while True:
            free = [ref for ref, deps in remaining.items() if not deps]
            if not free:
                break
            for ref in free:
                del remaining[ref]
            for deps in remaining.values():
                deps.difference_update(free)
        if remaining:
            raise ValidationError(
                "The steps' prerequisites form a cycle.",
                code="import_ai.prerequisite_cycle",
                params={"chemin": "parcours.etapes", "valeur": ", ".join(sorted(remaining))},
            )

    # --- creation (single transaction) ---------------------------------------------

    async def _create(
        self,
        agent: Agent,
        name_blob: dict[str, str],
        steps: list[_Step],
        defs_plan: dict[str, _Field],
        defs_position: int,
        resolved_providers: dict[str, Agent],
        warning_count: int,
    ) -> uuid.UUID:
        agency_default = await JourneysManager(self.db).agency_default(agent.agency_id)
        template = JourneyTemplate(
            id=uuid.uuid4(),
            agency_id=agent.agency_id,
            is_sample=False,
            name=name_blob.get(agency_default) or name_blob["fr"],
            name_i18n=name_blob,
        )
        self.db.add(template)

        for key, parsed in defs_plan.items():
            self.db.add(
                CustomFieldDefinition(
                    agency_id=agent.agency_id,
                    key=key,
                    label=parsed.label["fr"],
                    label_i18n=parsed.label,
                    field_type=parsed.field_type,
                    options=parsed.options,
                    required=parsed.required,
                    position=defs_position,
                )
            )
            defs_position += 1

        # Parents FIRST (template + steps), flush, THEN the children rows
        # that FK them - the unit of work has no relationship objects here
        # to order the inserts on its own (same pattern as the clone).
        steps_by_ref: dict[str, JourneyTemplateStep] = {}
        for position, parsed_step in enumerate(steps):
            row = JourneyTemplateStep(
                id=uuid.uuid4(),
                template_id=template.id,
                name=parsed_step.name.get(agency_default) or parsed_step.name["fr"],
                name_i18n=parsed_step.name,
                position=position,
                estimated_days=parsed_step.estimated_days,
                completion_mode=(
                    "auto"
                    if parsed_step.validator == StepValidatorType.NONE.value
                    else "agency_validation"
                ),
                default_validated_by_type=parsed_step.validator,
                content_note=(
                    parsed_step.note.get(agency_default) or parsed_step.note.get("fr")
                    if parsed_step.note
                    else None
                ),
                content_note_i18n=parsed_step.note,
            )
            self.db.add(row)
            steps_by_ref[parsed_step.ref] = row
        await self.db.flush()

        declared: list[str] = []  # template collection, insertion order
        for parsed_step in steps:
            row = steps_by_ref[parsed_step.ref]
            for kind, role in parsed_step.participant_roles:
                self.db.add(
                    JourneyStepParticipant(
                        step_id=row.id,
                        type="expat" if kind == "client" else "agent",
                        agent_id=None,
                        role=role,
                    )
                )
            # Resolved provider slots become REAL template participants
            # (type='agent' + the external agent_id, the slot's own role).
            # ensure_external_assignment then propagates to every dossier
            # instantiated from this template.
            for job, role in parsed_step.provider_jobs:
                external = resolved_providers.get(job)
                if external is not None:
                    self.db.add(
                        JourneyStepParticipant(
                            step_id=row.id,
                            type="agent",
                            agent_id=external.id,
                            role=role,
                        )
                    )
            step_keys: set[str] = set()
            for i, parsed_field in enumerate(parsed_step.fields):
                key = parsed_field.final_key
                if key in step_keys:  # same field twice on one step: one ask
                    continue
                step_keys.add(key)
                if key not in declared:
                    declared.append(key)
                self.db.add(
                    StepRequirement(
                        step_id=row.id,
                        kind=StepRequirementKind.CUSTOM_FIELD.value,
                        reference=key,
                        scope=StepRequirementScope.PRINCIPAL.value,
                        position=i,
                    )
                )

        # Membership rule: every collected field is ALSO declared in the
        # template's field collection (the requirement invariant).
        for i, key in enumerate(declared):
            self.db.add(
                JourneyTemplateField(
                    template_id=template.id,
                    kind=StepRequirementKind.CUSTOM_FIELD.value,
                    reference=key,
                    position=i,
                    required_at_creation=False,
                )
            )
        for parsed_step in steps:
            for ref in parsed_step.prerequisites:
                self.db.add(
                    StepPrerequisite(
                        step_id=steps_by_ref[parsed_step.ref].id,
                        prerequisite_step_id=steps_by_ref[ref].id,
                    )
                )

        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="journey.imported_from_ai",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"steps": len(steps), "warnings": warning_count},
        )
        await self.db.commit()
        return template.id
