import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agent import Agent
from shared.models.company_profile import CompanyFieldDefinition
from shared.models.custom_field import CustomFieldDefinition
from src.agencies.profile_sections_manager import assert_section_exists
from src.company_profiles.company_catalog import (
    company_definition_by_id,
    company_definitions_by_ids,
    company_value_counts,
)
from src.core.enums import ActorType
from src.core.exceptions import ConflictError, NotFoundError, ValidationError
from src.core.i18n import DEFAULT_LANG, apply_i18n_write
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.custom_fields.custom_fields_schema import (
    CustomFieldBulkRefusal,
    CustomFieldBulkReport,
    CustomFieldBulkRequest,
    CustomFieldDefinitionCreate,
    CustomFieldDefinitionUpdate,
)
from src.usage.usage_manager import UsageManager


async def materialize_preset_definitions(
    db: "AsyncSession", agency_id: "uuid.UUID", keys: set[str], lang: str
) -> list[str]:
    """LA mécanique du picker (extraite de JourneysManager._materialize_
    catalog_definitions, réutilisée par l'import) : créer les défs
    manquantes pour des clés du CATALOGUE — label i18n complet, options
    en langue d'agence, les existantes (archivées comprises) jamais
    recréées (idempotent). Correctif au passage : scope et profile_section
    suivent la CLASSIFICATION du catalogue (les packs person naissaient
    'case'/'misc' depuis la migration f2d6 — bug latent des deux
    appelants). Retourne les clés créées."""
    from shared.models.custom_field import CustomFieldDefinition
    from src.client_profiles.profile_sections import catalog_classification
    from src.custom_fields.custom_fields_repository import CustomFieldsRepository
    from src.journeys.field_catalog import FIELD_PRESETS

    wanted = {k for k in keys if k in FIELD_PRESETS}
    if not wanted:
        return []
    existing = {
        d.key
        for d in await CustomFieldsRepository(db).list_for_agency(agency_id, include_archived=True)
    }
    created: list[str] = []
    for key in sorted(wanted - existing):
        preset = FIELD_PRESETS[key]
        options = None
        if preset.options is not None:
            options = preset.options.get(lang) or preset.options["fr"]
        scope, section = catalog_classification(key)
        db.add(
            CustomFieldDefinition(
                agency_id=agency_id,
                key=key,
                label=preset.labels.get(lang) or preset.labels["fr"],
                label_i18n=dict(preset.labels),
                field_type=preset.field_type,
                options=options,
                scope=scope,
                profile_section=section,
            )
        )
        created.append(key)
    return created


class CustomFieldsManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = CustomFieldsRepository(db)

    async def list_definitions(
        self, agent: Agent, *, include_archived: bool = False
    ) -> list[CustomFieldDefinition]:
        return await self.repo.list_for_agency(agent.agency_id, include_archived=include_archived)

    async def active_definitions(self, agency_id: uuid.UUID) -> list[CustomFieldDefinition]:
        return await self.repo.list_for_agency(agency_id, include_archived=False)

    async def agency_default(self, agency_id: uuid.UUID) -> str:
        """The agency's default content language (i18n label fallback)."""
        stmt = select(Agency.default_language).where(Agency.id == agency_id)
        return (await self.db.execute(stmt)).scalar_one_or_none() or DEFAULT_LANG

    async def build_definition(
        self,
        agent: Agent,
        payload: CustomFieldDefinitionCreate,
        *,
        scope: str,
        profile_section: str = "misc",
    ) -> CustomFieldDefinition:
        """Le cœur SANS COMMIT de `create` — réutilisé par la création
        depuis la grille d'import (le batch reste transactionnel, un seul
        commit en bout de course). Même validation, même i18n.

        `scope` N'A PLUS DE DÉFAUT (arbitrage du 07/08). Trois trous en
        trois semaines venaient tous du même motif : un appelant oubliait
        l'argument, le défaut « mission » s'appliquait en silence, et
        personne ne le voyait avant qu'une agence se plaigne de champs
        invisibles. Un paramètre sans défaut fait échouer la PASSE, pas le
        client. Une clé du catalogue se classe par
        `catalog_classification` ; un champ voulu par l'agence porte le
        choix qu'elle a fait à l'écran."""
        # `key` n'est optionnelle au contrat QUE pour la face société, où
        # elle est dérivée (`_create_company`) — sur ce chemin le
        # validateur du schéma l'a déjà exigée.
        assert payload.key is not None
        if await self.repo.get_by_key(agent.agency_id, payload.key) is not None:
            raise ConflictError(f"A custom field with key {payload.key!r} already exists.")
        agency_default = await self.agency_default(agent.agency_id)
        label_scalar, label_blob = apply_i18n_write(
            payload.label_i18n, payload.label, agency_default, None, {}
        )
        definition = self.repo.add(
            agency_id=agent.agency_id,
            key=payload.key,
            label=label_scalar or payload.label,
            field_type=payload.field_type.value,
            options=payload.options,
            required=payload.required,
            position=payload.position,
            scope=scope,
            profile_section=profile_section,
        )
        definition.label_i18n = label_blob
        return definition

    async def create(
        self, agent: Agent, payload: CustomFieldDefinitionCreate
    ) -> "CustomFieldDefinition | CompanyFieldDefinition":
        # LA FACE SOCIÉTÉ PASSE PAR LA MÊME PORTE (D9) — comme le PATCH,
        # l'archivage et la masse. Le dispatch se fait sur la portée
        # DEMANDÉE, seule information disponible avant qu'une ligne existe
        # (les trois autres gestes, eux, dispatchent sur l'id trouvé).
        if payload.scope == "company":
            return await self._create_company(agent, payload)
        section = payload.profile_section or "misc"
        if payload.profile_section is not None:
            await assert_section_exists(self.db, agent.agency_id, "person", section)
        # La portée voulue, ou le défaut historique si l'appelant se tait.
        definition = await self.build_definition(
            agent, payload, scope=payload.scope or "case", profile_section=section
        )
        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="agency.custom_fields_set",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={"key": payload.key},
        )
        await self.db.commit()
        await self.db.refresh(definition)
        return definition

    async def _create_company(
        self, agent: Agent, payload: CustomFieldDefinitionCreate
    ) -> "CompanyFieldDefinition":
        """CRÉER un champ de fiche SOCIÉTÉ (D9) — le geste qui manquait à
        cette face : ses champs ne naissaient que du catalogue (les 17
        presets matérialisés) ou d'une colonne baptisée à la grille
        d'import. Une agence qui voulait « Numéro de greffe » n'avait que
        le détour d'un fichier.

        LA CLÉ EST DÉRIVÉE, JAMAIS REÇUE, par la MÊME moulinette que la
        grille d'import (`slugify_field_label`) : les deux chemins qui
        peuplent cette table fabriquent donc la même clé pour le même
        libellé — « Numéro de greffe » saisi ici et importé demain se
        retrouvent, au lieu de coexister en double.

        DEUX COLLISIONS, DEUX REFUS DISTINCTS, parce que l'issue diffère :
        - un PRESET du catalogue (409 `company_field.key_reserved`) : la
          clé appartient au produit, elle est déjà dans l'univers de
          l'agence (ou y entrera à la première ouverture d'écran) — le
          recours est de renommer ce champ-là, pas d'en créer un second ;
        - une clé DÉJÀ PRISE par l'agence (409 `company_field.key_exists`,
          avec l'id et le libellé de la définition en place, et son état
          d'archivage) : le recours est de la renommer ou de la
          ressusciter. Un champ archivé compte comme pris — la contrainte
          `(agency_id, key)` couvre les archivés, et créer par-dessus
          adopterait ses valeurs en silence.

        On MATÉRIALISE avant de comparer (les 17 presets + les clés de
        sack de toute l'agence) : sans ça, une agence qui n'a jamais
        ouvert un écran société aurait zéro définition en base, donc zéro
        collision détectée, et le nouveau champ naîtrait sur une clé déjà
        porteuse de valeurs. C'est aussi ce qui rend la position vraie —
        voir plus bas. Le refus, lui, n'écrit rien : la transaction n'est
        jamais committée sur ce chemin.
        """
        from shared.models.company_profile import CompanyFieldDefinition
        from src.company_profiles.company_catalog import (
            company_preset_keys,
            company_preset_spec,
            company_sack_keys,
            humanize,
            materialize_company_definitions,
        )
        from src.imports.value_normalizers import slugify_field_label

        # Les attributs que cette face NE PORTE PAS — mêmes refus, même
        # code que le PATCH (`_update_company`) : `options`/`required`
        # n'existent pas sur `company_field_definition`, et `key` est
        # dérivée. Accepter pour ne rien en faire serait un mensonge.
        for unsupported in ("key", "options", "required"):
            if getattr(payload, unsupported):
                raise ValidationError(
                    f"`{unsupported}` does not apply to a company field.",
                    code="company_field.unsupported_attribute",
                    params={"attribute": unsupported},
                )
        if payload.profile_section is None:
            # SANS DÉFAUT, à la différence de la face personne : « Divers »
            # posé en silence ferait naître le champ ailleurs que là où
            # l'agence l'a déposé (le motif exact de l'arbitrage `scope`).
            raise ValidationError(
                "`profile_section` is required for a company field.",
                code="company_field.section_required",
            )
        await assert_section_exists(self.db, agent.agency_id, "company", payload.profile_section)

        key = slugify_field_label(payload.label)
        if key in company_preset_keys():
            _type, _section, labels = company_preset_spec(key)
            raise ConflictError(
                f"The key {key!r} derived from this label is a company preset.",
                code="company_field.key_reserved",
                params={"key": key, "label": labels.get("fr") or humanize(key)},
            )
        definitions = await materialize_company_definitions(
            self.db,
            agent.agency_id,
            sack_keys=await company_sack_keys(self.db, agent.agency_id),
        )
        taken = next((d for d in definitions if d.key == key), None)
        if taken is not None:
            raise ConflictError(
                f"A company field with key {key!r} already exists in this agency.",
                code="company_field.key_exists",
                params={
                    "key": key,
                    "label": taken.label,
                    "field_id": str(taken.id),
                    "archived": taken.archived_at is not None,
                },
            )
        agency_default = await self.agency_default(agent.agency_id)
        # MÊME MÉCANIQUE i18n QUE LA FACE PERSONNE, puisque c'est la même
        # porte : le libellé seul s'ancre dans la langue de l'agence, un
        # `label_i18n` explicite est honoré tel quel. C'est ce que le PATCH
        # société fait déjà — la création ne pouvait pas dire l'inverse.
        label_scalar, label_blob = apply_i18n_write(
            payload.label_i18n, payload.label, agency_default, None, {}
        )
        # EN FIN DE SECTION, obtenu par la fin de la suite GLOBALE : la
        # position est une suite d'agence, pas un rang dans la section
        # (contrat `FieldUniverseEntry.position`). `max + 1` place donc le
        # champ après tout ce qui existe, donc dernier de SA section —
        # quelle qu'elle soit, et sans départager une égalité par la clé.
        definition = CompanyFieldDefinition(
            agency_id=agent.agency_id,
            key=key,
            label=label_scalar or payload.label,
            label_i18n=label_blob,
            field_type=payload.field_type.value,
            profile_section=payload.profile_section,
            position=max((d.position for d in definitions), default=-1) + 1,
        )
        self.db.add(definition)
        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="agency.custom_fields_set",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            # `surface` distingue les deux faces dans le même événement :
            # le jalon « champs perso configurés » vaut pour les deux, le
            # détail dit laquelle.
            details={"key": key, "surface": "company"},
        )
        await self.db.commit()
        await self.db.refresh(definition)
        return definition

    async def update(
        self, agent: Agent, field_id: uuid.UUID, payload: CustomFieldDefinitionUpdate
    ) -> "CustomFieldDefinition | CompanyFieldDefinition":
        definition = await self.repo.get_in_agency(agent.agency_id, field_id)
        if definition is None:
            company = await company_definition_by_id(self.db, agent.agency_id, field_id)
            if company is not None:
                return await self._update_company(agent, company, payload)
            raise NotFoundError("Custom field not found.")
        provided = payload.model_dump(exclude_unset=True)
        # key and field_type are immutable (not in the update schema).
        if "label" in provided or "label_i18n" in provided:
            agency_default = await self.agency_default(agent.agency_id)
            scalar, blob = apply_i18n_write(
                payload.label_i18n if "label_i18n" in provided else None,
                payload.label if "label" in provided else None,
                agency_default,
                definition.label,
                definition.label_i18n,
            )
            definition.label = scalar or definition.label
            definition.label_i18n = blob
        if "scope" in provided and provided["scope"] is not None:
            definition.scope = provided["scope"]
        if "profile_section" in provided and provided["profile_section"] is not None:
            # VALIDATION PAR TENANT (remplace le `Literal` gravé) : la
            # section doit exister chez CETTE agence, sur la surface de ce
            # champ. Les champs de dossier partagent la taxonomie de la
            # face personne — deux univers de CHAMPS, une seule taxonomie
            # de sections, celle que la fiche montre.
            await assert_section_exists(
                self.db, agent.agency_id, "person", provided["profile_section"]
            )
            definition.profile_section = provided["profile_section"]
        if "required" in provided and provided["required"] is not None:
            definition.required = provided["required"]
        if "position" in provided and provided["position"] is not None:
            definition.position = provided["position"]
        if "options" in provided:
            # Only meaningful for select types; the create-time validator
            # already pinned that. Editing options is allowed (adding or
            # removing) — removed options orphan existing values, kept.
            definition.options = provided["options"]
        await self.db.commit()
        await self.db.refresh(definition)
        return definition

    async def _update_company(
        self,
        agent: Agent,
        definition: "CompanyFieldDefinition",
        payload: CustomFieldDefinitionUpdate,
    ) -> "CompanyFieldDefinition":
        """Le PATCH d'une définition SOCIÉTÉ — même route, même contrat,
        moins deux champs qui n'ont pas de sens de ce côté :

        - `scope` : une définition société EST sa face. La proposer
          reviendrait à offrir de déménager un champ de la fiche société
          vers la fiche personne, où sa clé est peut-être déjà prise par
          une autre définition. Refus explicite, pas un silence.
        - `options` / `required` : la fiche société n'a ni liste de choix
          ni champ obligatoire au contrat (l'univers les sert à `null`) —
          accepter la valeur pour ne rien en faire serait un mensonge.
        """
        provided = payload.model_dump(exclude_unset=True)
        for unsupported in ("scope", "options", "required"):
            if provided.get(unsupported) is not None:
                raise ValidationError(
                    f"`{unsupported}` does not apply to a company field.",
                    code="company_field.unsupported_attribute",
                    params={"attribute": unsupported, "key": definition.key},
                )
        if "label" in provided or "label_i18n" in provided:
            agency_default = await self.agency_default(agent.agency_id)
            scalar, blob = apply_i18n_write(
                payload.label_i18n if "label_i18n" in provided else None,
                payload.label if "label" in provided else None,
                agency_default,
                definition.label,
                definition.label_i18n,
            )
            definition.label = scalar or definition.label
            definition.label_i18n = blob
        if provided.get("profile_section") is not None:
            await assert_section_exists(
                self.db, agent.agency_id, "company", provided["profile_section"]
            )
            definition.profile_section = provided["profile_section"]
        if provided.get("position") is not None:
            definition.position = provided["position"]
        await self.db.commit()
        await self.db.refresh(definition)
        return definition

    async def archive(
        self, agent: Agent, field_id: uuid.UUID, *, force: bool = False
    ) -> "CustomFieldDefinition | CompanyFieldDefinition":
        """Soft archive — the only form of removal. Saved values are
        kept (the JSONB is independent); the field leaves the form.

        LA PROTECTION PARCOURS (posée ici, elle n'existait pas) : un champ
        qu'un parcours collecte ou exige ne part pas en silence — 409 qui
        NOMME les parcours. `force=True` la franchit, en connaissance de
        cause. Ce n'est pas un mur : le reste du système tolère un champ
        archivé attaché (il reste listé, drapeau `is_archived`), et 175 des
        254 définitions actives de la base de dev sont dans ce cas — un
        refus sans issue rendrait l'archivage impraticable."""
        definition = await self.repo.get_in_agency(agent.agency_id, field_id)
        if definition is None:
            company = await company_definition_by_id(self.db, agent.agency_id, field_id)
            if company is not None:
                # Pas de garde parcours de ce côté : voir company_value_counts.
                if company.archived_at is None:
                    company.archived_at = datetime.now(UTC)
                    await self.db.commit()
                    await self.db.refresh(company)
                return company
            raise NotFoundError("Custom field not found.")
        if definition.archived_at is None:
            if not force:
                usage = await self.repo.journey_usage(agent.agency_id, {definition.key})
                if definition.key in usage:
                    raise ConflictError(
                        f"Custom field {definition.key!r} is used by a journey template.",
                        code="custom_field.used_in_journey",
                        params={"label": definition.label, "templates": usage[definition.key]},
                    )
            definition.archived_at = datetime.now(UTC)
            await self.db.commit()
            await self.db.refresh(definition)
        return definition

    async def bulk(self, agent: Agent, payload: CustomFieldBulkRequest) -> CustomFieldBulkReport:
        """LES TROIS GESTES DE MASSE — archiver, reclasser, ranger — sur
        une SÉLECTION d'ids, en une transaction et un rapport.

        UNE SEULE évaluation sert le dry-run et le geste : le compte
        annoncé à l'écran et ce qui est appliqué ne peuvent pas diverger
        (même principe que l'aperçu d'import, où `_analyze` décide seul).
        `dry_run` s'arrête juste avant l'écriture, tout le reste est
        identique — refus compris."""
        ids = set(payload.ids)
        found = await self.repo.by_ids_in_agency(agent.agency_id, ids)
        by_id = {d.id: d for d in found}
        # LES DEUX FACES DANS UNE SÉLECTION : l'écran travaille une surface
        # à la fois, mais le contrat ne l'impose pas — un id qui n'est pas
        # une définition personne/dossier peut être une définition SOCIÉTÉ.
        # On le résout avant de crier « inconnu ».
        company_rows = await company_definitions_by_ids(
            self.db, agent.agency_id, ids - by_id.keys()
        )
        company_by_id = {row.id: row for row in company_rows}
        if payload.action == "section" and payload.profile_section is not None:
            # LA VALIDATION PAR TENANT, posée AVANT le dry-run : annoncer
            # un reclassement vers une section qui n'existe pas serait pire
            # que le refuser — l'écran afficherait un compte, puis
            # échouerait au geste réel. Vérifiée sur les surfaces
            # RÉELLEMENT visées : une sélection de champs société n'a pas à
            # exiger que la clé existe aussi côté personne.
            for surface, present in (("person", bool(found)), ("company", bool(company_rows))):
                if present:
                    await assert_section_exists(
                        self.db, agent.agency_id, surface, payload.profile_section
                    )
        # Un id inconnu (ou d'une autre agence) est un refus, pas un silence.
        refusals = [
            CustomFieldBulkRefusal(id=missing, reason="not_found")
            for missing in sorted(ids - by_id.keys() - company_by_id.keys(), key=str)
        ]
        keys = {d.key for d in found}
        usage = await self.repo.journey_usage(agent.agency_id, keys)
        values = await self.repo.value_counts(agent.agency_id, keys)

        eligible: list[CustomFieldDefinition] = []
        unchanged = 0
        for definition in found:
            if payload.action == "archive":
                if definition.archived_at is not None:
                    unchanged += 1
                    continue
                if definition.key in usage and not payload.force:
                    refusals.append(
                        CustomFieldBulkRefusal(
                            id=definition.id,
                            key=definition.key,
                            label=definition.label,
                            reason="used_in_journey",
                            templates=usage[definition.key],
                        )
                    )
                    continue
            elif (
                payload.action == "scope"
                and definition.scope == payload.scope
                or (
                    payload.action == "section"
                    and definition.profile_section == payload.profile_section
                )
            ):
                unchanged += 1
                continue
            eligible.append(definition)

        # La face SOCIÉTÉ de la sélection, évaluée sur les mêmes règles.
        company_eligible: list[CompanyFieldDefinition] = []
        for row in company_rows:
            if payload.action == "scope":
                # Une définition société n'a pas de portée à changer : sa
                # face EST sa portée (cf. _update_company).
                refusals.append(
                    CustomFieldBulkRefusal(
                        id=row.id, key=row.key, label=row.label, reason="not_found"
                    )
                )
                continue
            if payload.action == "archive" and row.archived_at is not None:
                unchanged += 1
                continue
            if payload.action == "section" and row.profile_section == payload.profile_section:
                unchanged += 1
                continue
            company_eligible.append(row)
        company_values = await company_value_counts(
            self.db, agent.agency_id, {row.key for row in company_eligible}
        )

        # Les conséquences se comptent sur les ÉLIGIBLES : annoncer des
        # valeurs concernées par un champ qu'on refuse serait un mensonge.
        report = CustomFieldBulkReport(
            dry_run=payload.dry_run,
            requested=len(ids),
            eligible=len(eligible) + len(company_eligible),
            applied=0,
            unchanged=unchanged,
            refused=len(refusals),
            with_values=sum(1 for d in eligible if values.get(d.key))
            + sum(1 for r in company_eligible if company_values.get(r.key)),
            values_count=sum(values.get(d.key, 0) for d in eligible)
            + sum(company_values.get(r.key, 0) for r in company_eligible),
            # `in_journey` reste la mesure de la face personne/dossier :
            # un parcours ne collecte pas de champ société.
            in_journey=sum(1 for d in eligible if d.key in usage),
            refusals=refusals,
        )
        if payload.dry_run or not (eligible or company_eligible):
            return report

        if company_eligible:
            company_changes: dict[str, object] = (
                {"archived_at": datetime.now(UTC)}
                if payload.action == "archive"
                else {"profile_section": payload.profile_section}
            )
            await self.db.execute(
                update(CompanyFieldDefinition)
                .where(CompanyFieldDefinition.id.in_([r.id for r in company_eligible]))
                .values(**company_changes)
                .execution_options(synchronize_session=False)
            )
        if not eligible:
            await self.db.commit()
            report.applied = len(company_eligible)
            return report

        if payload.action == "archive":
            changes: dict[str, object] = {"archived_at": datetime.now(UTC)}
        elif payload.action == "scope":
            changes = {"scope": payload.scope}
        else:
            changes = {"profile_section": payload.profile_section}
        # UN seul UPDATE pour toute la sélection (leçon du lot d'import :
        # une écriture par ligne coûte une requête par ligne).
        await self.db.execute(
            update(CustomFieldDefinition)
            .where(CustomFieldDefinition.id.in_([d.id for d in eligible]))
            .values(**changes)
            .execution_options(synchronize_session=False)
        )
        await UsageManager(self.db).emit(
            agency_id=agent.agency_id,
            event_type="agency.custom_fields_set",
            actor_type=ActorType.AGENT,
            actor_id=agent.id,
            details={
                "bulk": payload.action,
                "count": len(eligible),
                "keys": sorted(d.key for d in eligible),
                **({"scope": payload.scope} if payload.action == "scope" else {}),
                **(
                    {"profile_section": payload.profile_section}
                    if payload.action == "section"
                    else {}
                ),
                **({"forced": True} if payload.force and payload.action == "archive" else {}),
            },
        )
        await self.db.commit()
        report.applied = len(eligible) + len(company_eligible)
        return report

    async def reorder(self, agent: Agent, field_ids: list[uuid.UUID]) -> None:
        """D12 — réécrire L'ORDRE ENTIER des définitions actives (1..N),
        en une transaction. Modèle : reorder_steps, avec trois écarts
        voulus — trois 422 DISTINCTS qui nomment les ids (le modèle n'a
        qu'un mismatch muet), la validation AVANT toute écriture (un 422
        laisse l'ordre intact), et pas de renumérotation en deux phases :
        l'unicité vient de la réécriture totale, pas d'un décalage.

        Un id étranger reçoit le même mot qu'un inconnu (`order_unknown`)
        — l'existence d'une définition d'une autre agence n'est jamais
        confirmée, même en creux (patron prefill-source)."""
        seen: set[uuid.UUID] = set()
        duplicated: set[uuid.UUID] = set()
        for field_id in field_ids:
            (duplicated if field_id in seen else seen).add(field_id)
        duplicates = sorted(str(i) for i in duplicated)
        if duplicates:
            raise ValidationError(
                "field_ids contains duplicates.",
                code="custom_field.order_duplicate",
                params={"duplicates": duplicates},
            )
        active = await self.repo.list_for_agency(agent.agency_id, include_archived=False)
        expected = {d.id for d in active}
        unknown = sorted(str(i) for i in seen - expected)
        if unknown:
            raise ValidationError(
                "field_ids contains ids that are not active definitions of this agency.",
                code="custom_field.order_unknown",
                params={"unknown": unknown},
            )
        missing = sorted(str(i) for i in expected - seen)
        if missing:
            raise ValidationError(
                "field_ids must contain every active definition of the agency.",
                code="custom_field.order_missing",
                params={"missing": missing},
            )
        # UN UPDATE, positions 1..N par ORDINALITY — le garde IS DISTINCT
        # rend le REJEU inerte (rejouer la même liste ne touche aucune
        # ligne, updated_at compris).
        await self.db.execute(
            sa_text(
                "UPDATE custom_field_definition AS d SET position = v.ord"
                " FROM (SELECT t.id, t.ord FROM unnest(CAST(:ids AS uuid[]))"
                " WITH ORDINALITY AS t(id, ord)) AS v"
                " WHERE d.id = v.id AND d.agency_id = :agency_id"
                " AND d.position IS DISTINCT FROM v.ord"
            ),
            {"ids": field_ids, "agency_id": agent.agency_id},
        )
        # Les ARCHIVÉES passent APRÈS N, dans leur ordre servi : l'unicité
        # des positions tient dans TOUTE la table, et une ressuscitée
        # réapparaît en fin de liste plutôt qu'à un rang périmé.
        await self.db.execute(
            sa_text(
                "UPDATE custom_field_definition AS d SET position = :n + s.rn"
                " FROM (SELECT id, row_number() OVER (ORDER BY position, created_at, id)"
                " AS rn FROM custom_field_definition"
                " WHERE agency_id = :agency_id AND archived_at IS NOT NULL) AS s"
                " WHERE d.id = s.id AND d.position IS DISTINCT FROM :n + s.rn"
            ),
            {"n": len(field_ids), "agency_id": agent.agency_id},
        )
        await self.db.commit()

    async def unarchive(
        self, agent: Agent, field_id: uuid.UUID
    ) -> "CustomFieldDefinition | CompanyFieldDefinition":
        """Symmetric to archive: clears archived_at. The field reappears
        in forms and its previously-orphaned JSONB values become exposed
        and validable again — the (agency_id, key) UNIQUE covers archived
        rows too, so resurrection can never collide. Idempotent: a no-op
        if already active."""
        definition = await self.repo.get_in_agency(agent.agency_id, field_id)
        if definition is None:
            company = await company_definition_by_id(self.db, agent.agency_id, field_id)
            if company is None:
                raise NotFoundError("Custom field not found.")
            if company.archived_at is not None:
                company.archived_at = None
                await self.db.commit()
                await self.db.refresh(company)
            return company
        if definition.archived_at is not None:
            definition.archived_at = None
            await self.db.commit()
            await self.db.refresh(definition)
        return definition
