"""Fiches client — F1 (lecture, fusion) + F2 (croisement à gestes).

Phase 0 fait foi : la fiche COPIE, ne déporte jamais (les valeurs des
dossiers restent la vérité des exigences, dérivées live) ; la divergence
est une COMPARAISON à la lecture (zéro marqueur, zéro sync) ; les gestes
sont des péages explicites tracés ; toute requête est scopée agence
(non-révélation cross-agence, patron prefill-source)."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.case_person import CasePerson
from shared.models.client_case import ClientCase
from shared.models.client_profile import ClientProfile
from shared.models.custom_field import CustomFieldDefinition
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.client_profiles.client_profiles_schema import (
    ClientProfileCreateRequest,
    ClientProfileListItemResponse,
    ClientProfileListResponse,
    ClientProfileResponse,
    ClientProfileUpdateRequest,
    NewCaseForProfileRequest,
    ProfileCaseSummaryResponse,
    ProfileCompletenessResponse,
)
from src.core.enums import CaseStatus
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.custom_fields.custom_fields_validation import visible_values
from src.progress.requirements_eval import COLLECTABLE_BASE_FIELDS, profile_field_value


def _is_empty(value: Any) -> bool:
    return value in (None, "", [], {})


def derived_client_status(cases: list[ClientCase]) -> str:
    """Phase 0 D9 — le statut client est DÉRIVÉ, jamais stocké (une seule
    vérité par construction) : 'prospect' tant qu'aucun dossier n'est allé
    au-delà de prospect, 'client' sinon."""
    beyond = any(c.status != CaseStatus.PROSPECT.value for c in cases)
    return "client" if beyond else "prospect"


async def person_scope_definitions(
    db: AsyncSession, agency_id: uuid.UUID
) -> list[CustomFieldDefinition]:
    definitions = await CustomFieldsRepository(db).list_for_agency(agency_id)
    return [d for d in definitions if d.archived_at is None and d.scope == "person"]


def completeness(
    profile: ClientProfile, person_defs: list[CustomFieldDefinition]
) -> ProfileCompletenessResponse:
    """F2.4 — la requête unique de la Phase 0 : les colonnes + le sac,
    croisés aux définitions actives scope='person'."""
    references = sorted(COLLECTABLE_BASE_FIELDS) + [d.key for d in person_defs]
    filled = [r for r in references if not _is_empty(profile_field_value(profile, r))]
    missing = [r for r in references if _is_empty(profile_field_value(profile, r))]
    return ProfileCompletenessResponse(filled=filled, missing=missing)


def profile_divergences(
    person: CasePerson, profile: ClientProfile, person_custom_keys: list[str]
) -> list[str]:
    """F2.2 — le verdict Phase 0 : la divergence est une comparaison
    directe fiche↔dossier à la lecture. Une référence diverge quand LES
    DEUX côtés portent une valeur ET qu'elles diffèrent (un côté vide =
    promouvable/reprennable, pas une divergence)."""
    out: list[str] = []
    for reference in sorted(COLLECTABLE_BASE_FIELDS) + person_custom_keys:
        case_value = (
            getattr(person, reference, None)
            if reference in COLLECTABLE_BASE_FIELDS
            else (person.custom_fields or {}).get(reference)
        )
        fiche_value = profile_field_value(profile, reference)
        if not _is_empty(case_value) and not _is_empty(fiche_value) and case_value != fiche_value:
            out.append(reference)
    return out


async def link_and_prefill_person(
    db: AsyncSession, agency_id: uuid.UUID, person: CasePerson
) -> ClientProfile | None:
    """F2.1 — le hook UNIQUE de liaison : à la création d'un dossier (le
    principal) comme à la liaison d'un membre à un compte. Get-or-create de
    la fiche (agence, expat), liaison de la personne, et PRÉ-REMPLISSAGE
    fill-gap des champs scope='person' — les valeurs déjà posées (wizard,
    prefill dossier) GAGNENT toujours (la fiche ne comble que les trous),
    snapshot posé sur le dossier (doctrine inchangée). Sans compte → None."""
    if person.expat_user_id is None:
        return None
    repo = ClientProfilesRepository(db)
    profile = await repo.get_by_expat(agency_id, person.expat_user_id)
    if profile is None:
        # ADOPTION (F4, liaison différée) : une fiche créée en direct
        # (sans compte) qui porte l'email de ce compte est LA fiche de ce
        # client — on la lie au lieu d'en créer une seconde.
        from sqlalchemy import select as sa_select

        from shared.models.expat_user import ExpatUser as ExpatUserModel

        account_email = (
            await db.execute(
                sa_select(ExpatUserModel.email).where(ExpatUserModel.id == person.expat_user_id)
            )
        ).scalar_one_or_none()
        if account_email:
            profile = await repo.get_unlinked_by_email(agency_id, account_email)
            if profile is not None:
                profile.expat_user_id = person.expat_user_id
    if profile is None:
        profile = ClientProfile(agency_id=agency_id, expat_user_id=person.expat_user_id)
        db.add(profile)
        await db.flush()
    person.client_profile_id = profile.id
    person_defs = await person_scope_definitions(db, agency_id)
    for reference in COLLECTABLE_BASE_FIELDS:
        if _is_empty(getattr(person, reference, None)):
            fiche_value = getattr(profile, reference, None)
            if not _is_empty(fiche_value):
                setattr(person, reference, fiche_value)
    if not person.preferred_channels and profile.preferred_channels:
        person.preferred_channels = list(profile.preferred_channels)
    sack = dict(person.custom_fields or {})
    changed = False
    for definition in person_defs:
        if _is_empty(sack.get(definition.key)):
            fiche_value = (profile.custom_fields or {}).get(definition.key)
            if not _is_empty(fiche_value):
                sack[definition.key] = fiche_value
                changed = True
    if changed:
        person.custom_fields = sack
    return profile


class ClientProfilesManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ClientProfilesRepository(db)

    async def _get(self, agent: Agent, profile_id: uuid.UUID) -> ClientProfile:
        profile = await self.repo.get_for_agency(agent.agency_id, profile_id)
        if profile is None:
            raise NotFoundError("Client profile not found.", code="profile.not_found")
        return profile

    async def list_profiles(
        self,
        agent: Agent,
        *,
        search: str | None,
        status: str | None = None,
        page: int,
        page_size: int,
    ) -> ClientProfileListResponse:
        profiles, total = await self.repo.list_page(
            agent.agency_id, search=search, status=status, page=page, page_size=page_size
        )
        cases = await self.repo.cases_for_profiles(
            agent.agency_id, [p.expat_user_id for p in profiles if p.expat_user_id]
        )
        by_expat: dict[uuid.UUID, list[ClientCase]] = {}
        for expat_id, case, _name in cases:
            by_expat.setdefault(expat_id, []).append(case)
        last_activity = await self.repo.last_activity_for_cases(
            list({case.id for _e, case, _n in cases})
        )
        items = []
        for profile in profiles:
            profile_cases = by_expat.get(profile.expat_user_id, []) if profile.expat_user_id else []
            case_activity = [last_activity[c.id] for c in profile_cases if c.id in last_activity]
            account = profile.expat_user
            items.append(
                ClientProfileListItemResponse(
                    id=profile.id,
                    first_name=account.first_name if account else (profile.first_name or ""),
                    last_name=account.last_name if account else (profile.last_name or ""),
                    email=account.email if account else (profile.email or ""),
                    cases_count=len(profile_cases),
                    active_cases_count=sum(
                        1 for c in profile_cases if c.status != CaseStatus.CLOSED.value
                    ),
                    derived_status=derived_client_status(profile_cases),
                    tags=list(profile.tags or []),
                    last_activity_at=max(case_activity) if case_activity else profile.updated_at,
                    client_space_activated=(account.activated_at is not None if account else False),
                    created_at=profile.created_at,
                )
            )
        return ClientProfileListResponse(items=items, total=total, page=page, page_size=page_size)

    async def get_profile(self, agent: Agent, profile_id: uuid.UUID) -> ClientProfileResponse:
        profile = await self._get(agent, profile_id)
        cases = await self.repo.cases_for_profiles(
            agent.agency_id, [profile.expat_user_id] if profile.expat_user_id else []
        )
        person_defs = await person_scope_definitions(self.db, agent.agency_id)
        all_defs = await CustomFieldsRepository(self.db).list_for_agency(agent.agency_id)
        active_defs = [d for d in all_defs if d.archived_at is None]
        # Annuaire F3.3 : l'étape en cours par dossier, batchée (UNE requête
        # progress), résolue dans la LANGUE DE L'AGENCE — même règle de bande
        # de progression que la liste dossiers.
        from src.core.i18n import resolve_i18n
        from src.progress.progress_repository import ProgressRepository

        with_journey = [c.id for _e, c, _n in cases if c.journey_template_id is not None]
        step_rows = (
            await ProgressRepository(self.db).current_steps_for_cases(with_journey)
            if with_journey
            else {}
        )
        agency_lang = await self.repo.agency_default_language(agent.agency_id)
        current_steps: dict[uuid.UUID, str | None] = {}
        for case_id, (step, _index, _total) in step_rows.items():
            current_steps[case_id] = (
                resolve_i18n(step.name_i18n, agency_lang, agency_lang, step.name)
                if step is not None
                else None
            )
        account = profile.expat_user
        return ClientProfileResponse(
            id=profile.id,
            expat_user_id=profile.expat_user_id,
            first_name=account.first_name if account else (profile.first_name or ""),
            last_name=account.last_name if account else (profile.last_name or ""),
            email=account.email if account else (profile.email or ""),
            preferred_lang=account.preferred_lang if account else None,
            activated_at=account.activated_at if account else None,
            passport_number=profile.passport_number,
            date_of_birth=profile.date_of_birth,
            nationality=profile.nationality,
            place_of_birth=profile.place_of_birth,
            sex=profile.sex,
            marital_status=profile.marital_status,
            phone=profile.phone,
            birth_name=profile.birth_name,
            profession=profile.profession,
            employer=profile.employer,
            preferred_channels=list(profile.preferred_channels or []),
            custom_fields=visible_values(active_defs, profile.custom_fields or {}),
            source=profile.source,
            tags=list(profile.tags or []),
            cases=[
                ProfileCaseSummaryResponse(
                    id=case.id,
                    status=case.status,
                    journey_name=journey_name,
                    current_step_name=current_steps.get(case.id),
                    reference=case.reference,
                    created_at=case.created_at,
                )
                for _expat, case, journey_name in cases
            ],
            derived_status=derived_client_status([c for _e, c, _n in cases]),
            completeness=completeness(profile, person_defs),
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )

    async def create_profile(
        self, agent: Agent, payload: ClientProfileCreateRequest
    ) -> ClientProfileResponse:
        """Création DIRECTE de fiche (complément 2, F4) — le prospect à
        froid, avant tout dossier. Fiche NON LIÉE (expat_user_id NULL) :
        AUCUN compte n'est créé ici — la liaison est différée au premier
        dossier (adoption par email dans link_and_prefill_person). Dédup :
        l'email déjà présent dans l'annuaire de l'agence (fiche liée ou
        non) → 409 nommé."""
        from src.client_profiles.backfill import CIVIL_COLUMNS

        if await self.repo.email_taken(agent.agency_id, payload.email):
            raise ConflictError(
                "A client profile with this email already exists in this agency.",
                code="profile.email_taken",
                params={"email": payload.email},
            )
        profile = ClientProfile(
            agency_id=agent.agency_id,
            expat_user_id=None,
            first_name=payload.first_name,
            last_name=payload.last_name,
            email=payload.email,
        )
        provided = payload.model_dump(exclude_unset=True)
        for field in (*CIVIL_COLUMNS, "preferred_channels"):
            if field not in provided:
                continue
            value = provided[field]
            if field == "preferred_channels":
                seen: list[str] = []
                for c in value or []:
                    v = c.value if hasattr(c, "value") else c
                    if v not in seen:
                        seen.append(v)
                profile.preferred_channels = seen
                continue
            setattr(profile, field, value.value if hasattr(value, "value") else value)
        if payload.custom_fields:
            all_defs = await CustomFieldsRepository(self.db).list_for_agency(agent.agency_id)
            active = [d for d in all_defs if d.archived_at is None]
            case_scope_keys = {d.key for d in active if d.scope != "person"}
            offending = sorted(set(payload.custom_fields) & case_scope_keys)
            if offending:
                raise ValidationError(
                    f"{offending[0]!r} is not a person-scoped field.",
                    code="profile.reference_not_person_scope",
                    params={"reference": offending[0]},
                )
            from src.custom_fields.custom_fields_validation import validate_and_merge

            profile.custom_fields = validate_and_merge(
                [d for d in active if d.scope == "person"], {}, payload.custom_fields
            )
        self.db.add(profile)
        await self.db.commit()
        return await self.get_profile(agent, profile.id)

    async def update_profile(
        self, agent: Agent, profile_id: uuid.UUID, payload: ClientProfileUpdateRequest
    ) -> ClientProfileResponse:
        """PATCH de la fiche — le miroir d'édition de PersonUpdateRequest,
        appliqué au plan PROFILE : mêmes sémantiques exclude_unset et la
        même transformation des valeurs que `_apply_civil_fields` côté
        dossier. Les dossiers ne bougent pas d'un octet (leur divergence
        éventuelle devient visible à la lecture — F2.2). Tracé sur chaque
        dossier vivant de la fiche (activity_log est case-scopé — une fiche
        sans dossier ne laisse que son updated_at)."""
        from src.client_profiles.backfill import CIVIL_COLUMNS

        profile = await self._get(agent, profile_id)
        provided = payload.model_dump(exclude_unset=True)
        touched: list[str] = []
        for field in (*CIVIL_COLUMNS, "preferred_channels"):
            if field not in provided:
                continue
            value = provided[field]
            touched.append(field)
            if field == "preferred_channels":
                seen: list[str] = []
                for c in value or []:
                    v = c.value if hasattr(c, "value") else c
                    if v not in seen:
                        seen.append(v)
                profile.preferred_channels = seen
                continue
            setattr(profile, field, value.value if hasattr(value, "value") else value)
        if "custom_fields" in provided and payload.custom_fields is not None:
            all_defs = await CustomFieldsRepository(self.db).list_for_agency(agent.agency_id)
            active = [d for d in all_defs if d.archived_at is None]
            case_scope_keys = {d.key for d in active if d.scope != "person"}
            offending = sorted(set(payload.custom_fields) & case_scope_keys)
            if offending:
                raise ValidationError(
                    f"{offending[0]!r} is not a person-scoped field.",
                    code="profile.reference_not_person_scope",
                    params={"reference": offending[0]},
                )
            from src.custom_fields.custom_fields_validation import validate_and_merge

            profile.custom_fields = validate_and_merge(
                [d for d in active if d.scope == "person"],
                dict(profile.custom_fields or {}),
                payload.custom_fields,
            )
            touched.extend(sorted(payload.custom_fields))
        if touched:
            from src.activity.activity_manager import ActivityManager
            from src.core.enums import ActorType

            cases = await self.repo.cases_for_profiles(
                agent.agency_id, [profile.expat_user_id] if profile.expat_user_id else []
            )
            activity = ActivityManager(self.db)
            for case_id in {case.id for _e, case, _n in cases}:
                activity.log_action(
                    case_id=case_id,
                    actor_type=ActorType.AGENT,
                    actor_id=agent.id,
                    action_type="profile.updated",
                    details={"profile_id": str(profile.id), "fields": touched},
                )
        await self.db.commit()
        return await self.get_profile(agent, profile_id)

    async def merge_profiles(
        self, agent: Agent, target_id: uuid.UUID, source_id: uuid.UUID
    ) -> ClientProfileResponse:
        """F1.6 — FUSIONNER : la cible GAGNE, la source comble les trous,
        les case_person re-liés, la source supprimée. Garde cross-agence :
        les deux fiches doivent être de MON agence (404 non-révélateur).
        Les comptes de login restent distincts. Tracé sur CHAQUE dossier
        re-lié (activity_log est case-scopé — le seul journal existant)."""
        if target_id == source_id:
            raise ValidationError("A profile cannot merge into itself.", code="profile.merge_self")
        target = await self._get(agent, target_id)
        source = await self._get(agent, source_id)
        for reference in COLLECTABLE_BASE_FIELDS:
            if _is_empty(getattr(target, reference, None)):
                value = getattr(source, reference, None)
                if not _is_empty(value):
                    setattr(target, reference, value)
        if not target.preferred_channels and source.preferred_channels:
            target.preferred_channels = list(source.preferred_channels)
        sack = dict(target.custom_fields or {})
        for key, value in (source.custom_fields or {}).items():
            if _is_empty(sack.get(key)) and not _is_empty(value):
                sack[key] = value
        target.custom_fields = sack
        if target.source is None and source.source:
            target.source = source.source
        target.tags = list(dict.fromkeys(list(target.tags or []) + list(source.tags or [])))

        from src.activity.activity_manager import ActivityManager
        from src.core.enums import ActorType

        activity = ActivityManager(self.db)
        persons = await self.repo.persons_linked_to_profile(source.id)
        for person in persons:
            person.client_profile_id = target.id
            activity.log_action(
                case_id=person.case_id,
                actor_type=ActorType.AGENT,
                actor_id=agent.id,
                action_type="profile.merged",
                details={
                    "from_profile_id": str(source.id),
                    "into_profile_id": str(target.id),
                    "person_id": str(person.id),
                },
            )
        await self.db.delete(source)
        await self.db.commit()
        return await self.get_profile(agent, target_id)

    # --- F2 : les gestes du croisement -----------------------------------

    async def _person_scope_reference_or_422(self, agency_id: uuid.UUID, reference: str) -> None:
        if reference in COLLECTABLE_BASE_FIELDS:
            return
        person_defs = await person_scope_definitions(self.db, agency_id)
        if reference not in {d.key for d in person_defs}:
            raise ValidationError(
                f"{reference!r} is not a person-scoped field.",
                code="profile.reference_not_person_scope",
                params={"reference": reference},
            )

    async def _gesture_person(
        self, agent: Agent, case_id: uuid.UUID, person_id: uuid.UUID
    ) -> tuple[CasePerson, ClientProfile]:
        from sqlalchemy import select

        row = (
            await self.db.execute(
                select(CasePerson, ClientCase)
                .join(ClientCase, ClientCase.id == CasePerson.case_id)
                .where(
                    CasePerson.id == person_id,
                    CasePerson.case_id == case_id,
                    ClientCase.agency_id == agent.agency_id,
                    ClientCase.deleted_at.is_(None),
                )
            )
        ).first()
        if row is None:
            raise NotFoundError("Person not found.", code="case.person_not_found")
        person = row[0]
        if person.client_profile_id is None:
            raise ValidationError(
                "This person has no client profile (no account).",
                code="profile.person_not_linked",
            )
        profile = await self.repo.get_for_agency(agent.agency_id, person.client_profile_id)
        if profile is None:
            raise NotFoundError("Client profile not found.", code="profile.not_found")
        return person, profile

    @staticmethod
    def _read(entity: Any, reference: str) -> Any:
        if reference in COLLECTABLE_BASE_FIELDS:
            return getattr(entity, reference, None)
        return (entity.custom_fields or {}).get(reference)

    @staticmethod
    def _write(entity: Any, reference: str, value: Any) -> None:
        if reference in COLLECTABLE_BASE_FIELDS:
            setattr(entity, reference, value)
        else:
            sack = dict(entity.custom_fields or {})
            if value is None:
                sack.pop(reference, None)
            else:
                sack[reference] = value
            entity.custom_fields = sack

    async def promote_field(
        self, agent: Agent, case_id: uuid.UUID, person_id: uuid.UUID, reference: str
    ) -> dict[str, Any]:
        """Dossier → fiche. Les AUTRES dossiers ne bougent pas (structurel :
        on n'écrit que la fiche). Idempotent (re-poser la même valeur)."""
        await self._person_scope_reference_or_422(agent.agency_id, reference)
        person, profile = await self._gesture_person(agent, case_id, person_id)
        value = self._read(person, reference)
        if _is_empty(value):
            raise ValidationError(
                "Nothing to promote: the case value is empty.",
                code="profile.promote_empty",
                params={"reference": reference},
            )
        self._write(profile, reference, value)
        from src.activity.activity_manager import ActivityManager
        from src.core.enums import ActorType

        ActivityManager(self.db).log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            action_type="profile.field_promoted",
            details={"reference": reference, "person_id": str(person_id)},
        )
        await self.db.commit()
        return {"reference": reference, "value": value}

    async def pull_field(
        self, agent: Agent, case_id: uuid.UUID, person_id: uuid.UUID, reference: str
    ) -> dict[str, Any]:
        """Fiche → dossier. La valeur reste dérivée live par les exigences
        (poser la valeur suffit, step_all_met suit — aucune mécanique
        d'étape touchée)."""
        await self._person_scope_reference_or_422(agent.agency_id, reference)
        person, profile = await self._gesture_person(agent, case_id, person_id)
        value = self._read(profile, reference)
        if _is_empty(value):
            raise ValidationError(
                "Nothing to pull: the profile value is empty.",
                code="profile.pull_empty",
                params={"reference": reference},
            )
        self._write(person, reference, value)
        from src.activity.activity_manager import ActivityManager
        from src.core.enums import ActorType

        ActivityManager(self.db).log_action(
            case_id=case_id,
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            action_type="profile.field_pulled",
            details={"reference": reference, "person_id": str(person_id)},
        )
        await self.db.commit()
        return {"reference": reference, "value": value}

    async def create_case_for_profile(
        self, agent: Agent, profile_id: uuid.UUID, payload: NewCaseForProfileRequest
    ) -> Any:
        """F2.5 — « Nouvelle démarche pour ce client » : nommer le germe
        constaté en Phase 0. L'identité vient de la FICHE (le link-or-create
        par email retombe sur le compte existant), le pré-remplissage passe
        par le hook F2.1 — même mécanique que toute création."""
        profile = await self._get(agent, profile_id)
        from src.cases.cases_manager import CasesManager
        from src.cases.cases_schema import CaseCreateRequest

        account = profile.expat_user
        if account is None and not profile.email:
            raise ValidationError(
                "This profile has no email to create a case with.",
                code="profile.no_email",
            )
        request = CaseCreateRequest(
            first_name=account.first_name if account else (profile.first_name or ""),
            last_name=account.last_name if account else (profile.last_name or ""),
            email=account.email if account else profile.email,
            preferred_lang=account.preferred_lang if account else "fr",
            journey_template_id=payload.journey_template_id,
            origin_country=payload.origin_country,
            dest_country=payload.dest_country,
            reference=payload.reference,
            source=payload.source or profile.source,
            tags=payload.tags,
        )
        from src.cases.cases_schema import CaseResponse

        case = await CasesManager(self.db).create_case(agent, request)
        return CaseResponse.model_validate(case)
