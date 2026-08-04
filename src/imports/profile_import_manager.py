"""Import FICHES (V4a + lot aperçu) — créer/lier des fiches depuis un
mapping colonnes→champs person, SANS parcours.

LA GARANTIE STRUCTURELLE (lot aperçu) : une SEULE fonction d'analyse
(`_analyze`) décide de chaque ligne — parse, corrections, validation
cellule par cellule (le contrat person), dédup base ET intra-batch.
Le preview la sert en dry-run (ZÉRO écriture) ; l'import réel ÉCRIT ce
qu'elle a décidé. Même fichier → verdicts IDENTIQUES, prouvé par test.

LA RÈGLE ABSOLUE (debug Teamleader 03/08) : une cellule mauvaise = trou
laissé (issue rapportée), une ligne mauvaise = ignorée avec raison, le
batch ne meurt JAMAIS sur une donnée utilisateur.

L'EMAIL N'EST PAS OBLIGATOIRE (parité avec la création manuelle, où il
l'est déjà) : une ligne SANS email mais AVEC identité est CRÉÉE — le
motif `no_email` est mort. Sans nom NI email il ne reste rien à créer :
`missing_identity`. La DÉDUP suit la même frontière — email présent, la
clé d'email (base + intra-batch) ; email absent, l'identité normalisée
DANS LE BATCH SEULEMENT, jamais contre la base (deux homonymes d'une
agence peuvent être deux personnes réelles). Le filet est en aval : une
fiche sans email refuse la création de démarche (422 profile.no_email)
tant qu'un email n'est pas posé au PATCH."""

import base64
import unicodedata
import uuid
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from src.client_profiles.backfill import CIVIL_COLUMNS
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.core.exceptions import ValidationError
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.imports.csv_reader import parse_upload

# L'identité vient de `import_targets` (LA source) ; les appelants
# historiques l'importent encore d'ici, le nom reste donc visible.
from src.imports.import_targets import IDENTITY_TARGETS


class StreetPair(NamedTuple):
    """Le couple assemblé : les colonnes DANS l'ordre « {numéro} {rue} »,
    et celles reconnues comme portant le NUMÉRO (par leur en-tête)."""

    columns: list[str]
    number_columns: frozenset[str]


def _resolve_street_pairs(
    mapping: dict[str, str], dotted_targets: set[str]
) -> dict[str, StreetPair]:
    """LE COUPLE rue + numéro — l'exception déclarée à l'anti-concaténation.
    DEUX colonnes max vers un <base>.street, rien d'autre : hors street un
    sous-champ = UNE colonne, au-delà de deux = 422 (l'exception est un
    couple, pas une invitation au collage libre). L'assemblage suit l'ordre
    fixe « {numéro} {rue} » (dominante FR/BE/BG des fichiers réels — pas de
    logique par pays en V1) ; le numéro se reconnaît à son en-tête, sinon
    l'ordre du mapping fait foi."""
    from src.imports.header_aliases import STREET_NUMBER_HEADERS, normalize_header

    by_target: dict[str, list[str]] = {}
    for column, target in mapping.items():
        if target in dotted_targets:
            by_target.setdefault(target, []).append(column)
    pairs: dict[str, StreetPair] = {}
    for target, columns in by_target.items():
        if len(columns) == 1:
            continue
        if not target.endswith(".street") or len(columns) > 2:
            raise ValidationError(
                f"{len(columns)} columns mapped to {target!r} — only the street "
                "number + street pair may share a sub-field.",
                code="import.address_subfield_pair_exceeded",
                params={"target": target, "columns": columns},
            )
        first, second = columns
        if normalize_header(second) in STREET_NUMBER_HEADERS and (
            normalize_header(first) not in STREET_NUMBER_HEADERS
        ):
            columns = [second, first]
        pairs[target] = StreetPair(
            columns=columns,
            number_columns=frozenset(
                c for c in columns if normalize_header(c) in STREET_NUMBER_HEADERS
            ),
        )
    return pairs


def _assemble_street_pair(pair: StreetPair, row: dict[str, str]) -> tuple[str | None, bool]:
    """Assemble « {numéro} {rue} » pour UNE ligne → (valeur, numéro orphelin).

    LA GARDE LIGNE À LIGNE (correctif b) : la garde « jamais un numéro
    seul » vivait au niveau COLONNE (le numéro n'est jamais suggéré sans
    une colonne rue en face) — pas au niveau LIGNE. Une ligne où seul le
    numéro était rempli produisait `street="11"`. Un numéro sans rue SUR LA
    LIGNE est désormais un trou motivé (issue), jamais un street."""
    cells = {c: (row.get(c) or "").strip() for c in pair.columns}
    named = [c for c in pair.columns if c not in pair.number_columns]
    if named and not any(cells[c] for c in named):
        # Aucune colonne « nom de rue » remplie : le numéro seul ne fait
        # pas une adresse — orphelin dès qu'il porte quelque chose.
        return None, any(cells[c] for c in pair.number_columns)
    joined = " ".join(cells[c] for c in pair.columns if cells[c])
    return (joined or None), False


def _name_dedup_key(first: str | None, last: str | None) -> tuple[str, str] | None:
    """LA CLÉ DE DÉDUP SANS EMAIL : prénom + nom normalisés (accents ôtés,
    casse et espaces indifférents). INTRA-BATCH SEULEMENT — jamais contre
    la base : deux « Jean Martin » d'une agence peuvent être deux personnes
    réelles, on ne fusionne pas des homonymes en silence. Dans un MÊME
    fichier, deux lignes d'identité identique sans email sont la même
    personne exportée deux fois."""

    def norm(value: str | None) -> str:
        s = unicodedata.normalize("NFKD", value or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        return " ".join(s.lower().split())

    first_n, last_n = norm(first), norm(last)
    return (first_n, last_n) if first_n and last_n else None


def _is_empty(value: Any) -> bool:
    return value in (None, "", [], {})


def _merge_gap(existing: Any, incoming: Any) -> Any | None:
    """LE FILL-GAP DESCEND AU SOUS-CHAMP → la valeur à poser, ou None si
    rien ne change.

    Une adresse est un OBJET `{street, city, postal_code, country}`.
    Comparer l'objet ENTIER (« non vide, on ne touche pas ») laissait un
    `{country: "FR"}` bloquer l'arrivée de la rue et de la ville : l'ordre
    des passes d'import devenait signifiant, et personne ne peut deviner
    que « le sous-champ le plus pauvre doit passer en dernier ».

    Au sous-champ, la règle s'efface : chaque sous-champ VIDE se remplit,
    chaque sous-champ REMPLI est protégé. Un scalaire déjà posé garde le
    comportement d'avant — l'existant gagne."""
    if _is_empty(existing):
        return incoming
    if not (isinstance(existing, dict) and isinstance(incoming, dict)):
        return None
    merged = dict(existing)
    for sub, sub_value in incoming.items():
        if _is_empty(merged.get(sub)) and not _is_empty(sub_value):
            merged[sub] = sub_value
    return merged if merged != existing else None


class ImportCorrection(BaseModel):
    """Une correction front : appliquée APRÈS parse, AVANT validation —
    la valeur corrigée passe la MÊME moulinette que la cellule d'origine.
    Une correction invalide = issue rapportée / ligne ignorée motivée,
    JAMAIS un 500 (la règle absolue tient)."""

    model_config = ConfigDict(extra="forbid")

    row_index: int = Field(ge=1)
    target: str = Field(min_length=1, max_length=100)
    value: str = Field(max_length=1000)


class FieldCreationSpec(BaseModel):
    """« Champ à créer » depuis la grille : la colonne, son label
    (pré-rempli du nom de colonne au front), le kind SIMPLE. Le scope est
    porté par l'ENDPOINT (import personnes → person, sociétés → sack) —
    plus simple au contrat, nommé au rapport."""

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=200)
    label: str = Field(min_length=1, max_length=200)
    kind: Literal["text", "number", "date", "boolean"]


class ProfileImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str | None = None
    file_b64: str | None = None
    filename: str | None = None
    # {csv_column: cible} — cibles : first_name/last_name/email (identité)
    # + colonnes civiles + clés custom scope='person'.
    mapping: dict[str, str] = Field(min_length=1)
    corrections: list[ImportCorrection] = Field(default_factory=list)
    # Création depuis la grille (lot grille) — dédup lier-pas-dupliquer
    # sur label/clé existants ; la déf naît à l'IMPORT seulement.
    create_fields: list[FieldCreationSpec] = Field(default_factory=list)


class ProfileImportPreviewRequest(ProfileImportRequest):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)


class RowIssue(BaseModel):
    column: str
    code: str


class RowVerdict(BaseModel):
    row_index: int
    status: Literal["create", "link", "ignore"]
    reason: str | None = None
    profile_id: uuid.UUID | None = None
    # Les valeurs NORMALISÉES par cible (post-coercition du contrat).
    person: dict[str, Any] = Field(default_factory=dict)
    issues: list[RowIssue] = Field(default_factory=list)


class ProfileImportRowOutcome(BaseModel):
    row: int
    email: str | None = None
    profile_id: uuid.UUID | None = None
    reason: str | None = None


class ProfileImportReport(BaseModel):
    total_rows: int
    created: list[ProfileImportRowOutcome]
    linked: list[ProfileImportRowOutcome]
    ignored: list[ProfileImportRowOutcome]
    # VENTILATION DES CRÉÉES (lot email optionnel) : l'agence sait tout de
    # suite combien de fiches ne pourront PAS recevoir d'espace client —
    # sans email, « Nouvelle démarche » répond 422 profile.no_email tant
    # qu'un email n'est pas posé au PATCH.
    created_with_email: int = 0
    created_without_email: int = 0
    # Correctif c : lignes ignorées dont les VALEURS ont tout de même été
    # reportées (fill-only) sur la fiche sœur créée par une autre ligne.
    values_salvaged: int = 0
    # Lot plafond : les lignes dont les tags ont été posés sur la fiche.
    tags_applied: int = 0
    # Lot grille : les champs NÉS de cet import (labels).
    fields_created: list[str] = Field(default_factory=list)


class ImportPreviewSummary(BaseModel):
    create: int
    link: int
    ignore: int
    ignore_reasons: dict[str, int]
    # Le dry-run annonce la même ventilation que le rapport réel
    # (preview == import, l'invariant structurel).
    create_with_email: int = 0
    create_without_email: int = 0


class ProfileImportPreviewResponse(BaseModel):
    total_rows: int
    summary: ImportPreviewSummary
    rows: list[RowVerdict]
    page: int
    page_size: int
    # Dry-run de la création : les labels qui NAÎTRAIENT à l'import (ceux
    # dédupliqués vers un champ existant n'y figurent pas).
    fields_created: list[str] = Field(default_factory=list)


def _summarize(verdicts: list[RowVerdict]) -> ImportPreviewSummary:
    reasons: dict[str, int] = {}
    for v in verdicts:
        if v.status == "ignore" and v.reason:
            reasons[v.reason] = reasons.get(v.reason, 0) + 1
    creates = [v for v in verdicts if v.status == "create"]
    return ImportPreviewSummary(
        create=len(creates),
        link=sum(1 for v in verdicts if v.status == "link"),
        ignore=sum(1 for v in verdicts if v.status == "ignore"),
        ignore_reasons=reasons,
        create_with_email=sum(1 for v in creates if v.person.get("email")),
        create_without_email=sum(1 for v in creates if not v.person.get("email")),
    )


class ProfileImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = ClientProfilesRepository(db)

    # --- LA fonction partagée (preview == import, structurel) -------------

    async def _analyze(self, agent: Agent, body: ProfileImportRequest) -> list[RowVerdict]:
        """Parse + corrections + validation + dédup — AUCUNE écriture."""
        if body.csv_text is None and body.file_b64 is None:
            raise ValidationError("Provide csv_text or file_b64.", code="import.source_missing")
        content: bytes | str = (
            base64.b64decode(body.file_b64) if body.file_b64 else (body.csv_text or "")
        )
        parsed = parse_upload(body.filename, content)

        # L'UNIVERS DES CIBLES vient de `import_targets` — LA source, celle
        # que la config d'agence consomme aussi (un test prouve l'égalité).
        # Composition d'adresse : les bases typées address acceptent le
        # mapping PAR SOUS-CHAMP (base.street|city|postal_code|country) EN
        # PLUS du texte intégral — les deux modes exclusifs par base.
        from src.imports.import_targets import person_targets

        targets = await person_targets(self.db, agent.agency_id)
        defs_by_key = targets.defs_by_key
        preset_person_keys = targets.preset_keys
        address_bases = targets.address_bases
        dotted_targets = targets.dotted
        valid_targets = targets.valid
        bad_targets = sorted(set(body.mapping.values()) - valid_targets)
        if bad_targets:
            raise ValidationError(
                f"Unknown import targets: {', '.join(bad_targets)}.",
                code="import.unknown_targets",
                params={"targets": bad_targets},
            )
        if "email" not in body.mapping.values():
            raise ValidationError(
                "The mapping must bind one column to 'email' (the dedup key).",
                code="import.email_target_required",
            )
        unknown_columns = sorted(set(body.mapping) - set(parsed.headers))
        if unknown_columns:
            raise ValidationError(
                f"Mapped columns absent from the file: {', '.join(unknown_columns)}.",
                code="import.unknown_columns",
                params={"columns": unknown_columns},
            )

        # EXCLUSIVITÉ des deux modes par base : texte intégral OU composé.
        mapped = set(body.mapping.values()) | {c.target for c in body.corrections}
        for base in address_bases:
            if base in mapped and any(t.startswith(base + ".") for t in mapped):
                raise ValidationError(
                    f"{base!r} is mapped both as full text and by sub-fields.",
                    code="import.address_mode_conflict",
                    params={"target": base},
                )
        street_pairs = _resolve_street_pairs(body.mapping, dotted_targets)

        # CRÉATION DEPUIS LA GRILLE : résolution AVANT la boucle — dédup
        # lier-pas-dupliquer (label OU clé déjà à l'agence → on LIE), sinon
        # pseudo-déf virtuelle (kind choisi) ; la naissance n'arrive qu'à
        # l'import (jamais ici).
        from src.imports.value_normalizers import slugify_field_label

        all_defs = await CustomFieldsRepository(self.db).list_for_agency(
            agent.agency_id, include_archived=True
        )
        labels_index = {(d.label or "").strip().lower(): d for d in all_defs}
        keys_index = {d.key: d for d in all_defs}
        creation_plan: dict[str, tuple[str, str, str, bool]] = {}
        # colonne → (clé cible, label, kind, à_créer)
        for spec in body.create_fields:
            if spec.column not in parsed.headers:
                raise ValidationError(
                    f"Column {spec.column!r} is absent from the file.",
                    code="import.unknown_columns",
                    params={"columns": [spec.column]},
                )
            if spec.column in body.mapping:
                raise ValidationError(
                    f"Column {spec.column!r} is both mapped and marked for creation.",
                    code="import.create_field_conflict",
                    params={"column": spec.column},
                )
            existing = labels_index.get(spec.label.strip().lower())
            slug = slugify_field_label(spec.label)
            if existing is None:
                existing = keys_index.get(slug)
            if existing is not None:
                creation_plan[spec.column] = (existing.key, spec.label, spec.kind, False)
                if existing.key not in defs_by_key:
                    defs_by_key[existing.key] = existing  # coercition par la vraie déf
            else:
                creation_plan[spec.column] = (slug, spec.label, spec.kind, True)
        self._creation_plan = creation_plan

        corrections_by_row: dict[int, list[ImportCorrection]] = {}
        for correction in body.corrections:
            corrections_by_row.setdefault(correction.row_index, []).append(correction)
        columns_by_target = {t: c for c, t in body.mapping.items()}
        columns_by_target.update({key: col for col, (key, _l, _k, _c) in creation_plan.items()})
        columns_by_target.update({t: " + ".join(p.columns) for t, p in street_pairs.items()})

        from src.cases.cases_schema import PersonUpdateRequest
        from src.core.enums import CustomFieldType
        from src.custom_fields.custom_fields_validation import _coerce_one

        # LE SELECT GROUPÉ (anti N+1) : les emails du fichier se relèvent en
        # une pré-passe légère (colonne mappée, correction éventuelle), la
        # base répond UNE fois pour tout le fichier — la boucle de verdicts
        # ne fait plus que des lectures de dictionnaire. La pré-passe est un
        # SURSET volontaire (une correction hors cibles y entre quand même) :
        # un email de trop dans le IN est inoffensif, un email manquant
        # fausserait le verdict.
        email_columns = [c for c, t in body.mapping.items() if t == "email"]
        candidate_emails: set[str] = set()
        for index, row in enumerate(parsed.rows, start=1):
            cell = ""
            for column in email_columns:
                raw_cell = (row.get(column) or "").strip()
                if raw_cell:
                    cell = raw_cell
            for correction in corrections_by_row.get(index, ()):
                if correction.target == "email":
                    cell = correction.value.strip()
            if cell:
                candidate_emails.add(cell.lower())
        existing_by_email = await self.repo.profile_ids_for_emails(
            agent.agency_id, candidate_emails
        )

        verdicts: list[RowVerdict] = []
        seen_emails: dict[str, int] = {}
        # Dédup des lignes SANS email — identité normalisée, batch seulement.
        seen_names: dict[tuple[str, str], int] = {}
        for index, row in enumerate(parsed.rows, start=1):
            issues: list[RowIssue] = []
            values: dict[str, str] = {}
            for column, target in body.mapping.items():
                if target in street_pairs:
                    continue  # le couple s'assemble ci-dessous, ordonné
                cell = (row.get(column) or "").strip()
                if cell:
                    values[target] = cell
            for target, pair in street_pairs.items():
                joined, orphan_number = _assemble_street_pair(pair, row)
                if joined:
                    values[target] = joined
                elif orphan_number:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get(target, target),
                            code="street_number_orphan",
                        )
                    )
            creation_cells: dict[str, tuple[str, str, str]] = {}
            for column, (key, _label, kind, _to_create) in creation_plan.items():
                cell = (row.get(column) or "").strip()
                if cell:
                    creation_cells[key] = (cell, kind, column)
            # CORRECTIONS : après parse, avant validation — même moulinette.
            for correction in corrections_by_row.get(index, ()):
                if correction.target not in valid_targets:
                    issues.append(RowIssue(column="(correction)", code="unknown_target"))
                    continue
                corrected = correction.value.strip()
                if corrected:
                    values[correction.target] = corrected
                else:
                    values.pop(correction.target, None)

            email = (values.get("email") or "").lower() or None
            person: dict[str, Any] = {}
            for target in ("first_name", "last_name"):
                if values.get(target):
                    person[target] = values[target]
            if email:
                person["email"] = email
            if values.get("preferred_lang"):
                from src.imports.value_normalizers import normalize_language_code

                code = normalize_language_code(values["preferred_lang"])
                if code is not None:
                    person["preferred_lang"] = code
                else:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get("preferred_lang", "preferred_lang"),
                            code="invalid_value",
                        )
                    )
            if values.get("tags"):
                person["tags"] = list(
                    dict.fromkeys(
                        t.strip() for t in values["tags"].replace(";", ",").split(",") if t.strip()
                    )
                )
            from src.imports.value_normalizers import normalize_import_value

            for civil in CIVIL_COLUMNS:
                raw = values.get(civil)
                if raw is None:
                    continue
                raw = normalize_import_value(civil, raw)
                try:
                    validated = PersonUpdateRequest.model_validate({civil: raw})
                except PydanticValidationError:
                    issues.append(
                        RowIssue(column=columns_by_target.get(civil, civil), code="invalid_value")
                    )
                    continue
                coerced = validated.model_dump(exclude_unset=True).get(civil)
                person[civil] = getattr(coerced, "value", coerced)
            # COMPOSITION : les sous-champs mappés s'assemblent en objet
            # adresse propre (validation PAR sous-champ, le reste vit).
            from src.imports.value_normalizers import assemble_address

            for base in address_bases:
                parts = {
                    t.split(".", 1)[1]: values.pop(t)
                    for t in list(values)
                    if t.startswith(base + ".")
                }
                if not parts:
                    continue
                assembled, failed = assemble_address(parts)
                for sub in failed:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get(f"{base}.{sub}", f"{base}.{sub}"),
                            code="invalid_value",
                        )
                    )
                if assembled:
                    person[base] = assembled
            # Champs à créer : la valeur coerce par le KIND choisi dès la
            # naissance (suggérable = coerçable) — illisible → trou motivé.
            for key, (cell, kind, column) in creation_cells.items():
                definition = defs_by_key.get(key)
                if definition is None:
                    from shared.models.custom_field import CustomFieldDefinition

                    definition = CustomFieldDefinition(
                        agency_id=agent.agency_id, key=key, label=key, field_type=kind
                    )
                try:
                    person[key] = _coerce_one(definition, cell)
                except ValueError:
                    issues.append(RowIssue(column=column, code="invalid_value"))
            custom_keys = (set(values) & (set(defs_by_key) | preset_person_keys)) - set(
                CIVIL_COLUMNS
            )
            for key in sorted(custom_keys):
                raw = values.get(key)
                if raw is None:
                    continue
                definition = defs_by_key.get(key)
                if definition is None:
                    # Preset non déclaré : la pseudo-déf du CATALOGUE porte
                    # le type et les options (langue d'agence en repli fr).
                    from src.journeys.field_catalog import FIELD_PRESETS

                    preset = FIELD_PRESETS[key]
                    from shared.models.custom_field import CustomFieldDefinition

                    definition = CustomFieldDefinition(
                        agency_id=agent.agency_id,
                        key=key,
                        label=preset.labels["fr"],
                        field_type=preset.field_type,
                        options=(preset.options or {}).get("fr") if preset.options else None,
                    )
                raw = normalize_import_value(key, raw, definition.option_values or None)
                try:
                    if definition.field_type == CustomFieldType.ADDRESS.value:
                        person[key] = _coerce_one(definition, {"street": raw})
                    else:
                        person[key] = _coerce_one(definition, raw)
                except ValueError:
                    issues.append(
                        RowIssue(column=columns_by_target.get(key, key), code="invalid_value")
                    )

            missing_identity = RowVerdict(
                row_index=index,
                status="ignore",
                reason="missing_identity",
                person=person,
                issues=issues,
            )
            if email:
                existing_id = existing_by_email.get(email)
                if existing_id is not None:
                    verdicts.append(
                        RowVerdict(
                            row_index=index,
                            status="link",
                            profile_id=existing_id,
                            person=person,
                            issues=issues,
                        )
                    )
                    continue
                if email in seen_emails:
                    # Dédup INTRA-BATCH : la 1re occurrence crée, celle-ci LIE.
                    verdicts.append(
                        RowVerdict(row_index=index, status="link", person=person, issues=issues)
                    )
                    continue
                if not values.get("first_name") or not values.get("last_name"):
                    verdicts.append(missing_identity)
                    continue
                seen_emails[email] = index
                verdicts.append(
                    RowVerdict(row_index=index, status="create", person=person, issues=issues)
                )
                continue

            # SANS EMAIL (parité avec la création manuelle, où l'email est
            # déjà optionnel) : l'identité suffit à créer. Le motif
            # `no_email` est MORT — sans nom NI email il ne reste rien,
            # c'est `missing_identity` qui parle.
            name_key = _name_dedup_key(values.get("first_name"), values.get("last_name"))
            if name_key is None:
                verdicts.append(missing_identity)
                continue
            if name_key in seen_names:
                # Dédup par identité — DANS LE BATCH SEULEMENT. Jamais
                # contre la base : on ne fusionne pas des homonymes.
                verdicts.append(
                    RowVerdict(row_index=index, status="link", person=person, issues=issues)
                )
                continue
            seen_names[name_key] = index
            verdicts.append(
                RowVerdict(row_index=index, status="create", person=person, issues=issues)
            )
        return verdicts

    # --- dry-run (ZÉRO écriture) ------------------------------------------

    async def preview(
        self, agent: Agent, body: ProfileImportPreviewRequest
    ) -> ProfileImportPreviewResponse:
        verdicts = await self._analyze(agent, body)
        start = (body.page - 1) * body.page_size
        return ProfileImportPreviewResponse(
            total_rows=len(verdicts),
            summary=_summarize(verdicts),
            rows=verdicts[start : start + body.page_size],
            page=body.page,
            page_size=body.page_size,
            fields_created=[
                label
                for _key, label, _kind, to_create in getattr(self, "_creation_plan", {}).values()
                if to_create
            ],
        )

    # --- import réel : ÉCRIT ce que l'analyse a décidé --------------------

    async def run_import(self, agent: Agent, body: ProfileImportRequest) -> ProfileImportReport:
        verdicts = await self._analyze(agent, body)
        # DÉCLARATION À LA VOLÉE (la mécanique du picker, helper partagé) :
        # les presets du catalogue mappés mais non déclarés deviennent des
        # défs de l'agence — idempotent, jamais au preview.
        from src.client_profiles.client_profiles_repository import (
            ClientProfilesRepository as _CPRepo,
        )
        from src.custom_fields.custom_fields_manager import materialize_preset_definitions

        used_targets = set(body.mapping.values()) | {c.target for c in body.corrections}
        # Les cibles POINTÉES déclarent leur BASE (residence_address.street
        # → la déf residence_address doit exister pour porter l'objet).
        used_targets |= {t.split(".", 1)[0] for t in used_targets if "." in t}
        lang = await _CPRepo(self.db).agency_default_language(agent.agency_id)
        await materialize_preset_definitions(self.db, agent.agency_id, used_targets, lang)
        # CRÉATION DEPUIS LA GRILLE : la déf naît par le cœur SANS COMMIT
        # du picker (build_definition) — scope person, section misc
        # (reclassable au toggle) ; le batch reste transactionnel.
        fields_created: list[str] = []
        plan = getattr(self, "_creation_plan", {})
        if any(to_create for _k, _l, _kd, to_create in plan.values()):
            from src.custom_fields.custom_fields_manager import CustomFieldsManager
            from src.custom_fields.custom_fields_schema import CustomFieldDefinitionCreate

            cf_manager = CustomFieldsManager(self.db)
            for _column, (key, label, kind, to_create) in plan.items():
                if not to_create:
                    continue
                await cf_manager.build_definition(
                    agent,
                    CustomFieldDefinitionCreate(key=key, label=label, field_type=kind),
                    scope="person",
                    profile_section="misc",
                )
                fields_created.append(label)
        created: list[ProfileImportRowOutcome] = []
        linked: list[ProfileImportRowOutcome] = []
        ignored: list[ProfileImportRowOutcome] = []
        ignored_by_row: dict[int, ProfileImportRowOutcome] = {}
        created_by_email: dict[str, uuid.UUID] = {}
        created_by_name: dict[tuple[str, str], uuid.UUID] = {}
        for verdict in verdicts:
            email = verdict.person.get("email")
            name_key = _name_dedup_key(
                verdict.person.get("first_name"), verdict.person.get("last_name")
            )
            if verdict.status == "ignore":
                outcome = ProfileImportRowOutcome(
                    row=verdict.row_index, email=email, reason=verdict.reason
                )
                ignored.append(outcome)
                ignored_by_row[verdict.row_index] = outcome
                continue
            if verdict.status == "create":
                profile = ClientProfile(
                    agency_id=agent.agency_id,
                    expat_user_id=None,
                    first_name=verdict.person["first_name"],
                    last_name=verdict.person["last_name"],
                    email=email,
                )
                self._apply_values(profile, verdict.person)
                self.db.add(profile)
                await self.db.flush()
                if email:
                    created_by_email[email] = profile.id
                elif name_key is not None:
                    created_by_name[name_key] = profile.id
                created.append(
                    ProfileImportRowOutcome(
                        row=verdict.row_index, email=email, profile_id=profile.id
                    )
                )
                continue
            # link — en base, ou vers la fiche créée plus haut dans le batch
            # (par email, ou par identité quand la ligne n'a pas d'email).
            profile_id = verdict.profile_id or (
                created_by_email.get(email)
                if email
                else (created_by_name.get(name_key) if name_key else None)
            )
            if profile_id is None:
                # La 1re occurrence de cet email a été ignorée (sans
                # identité) : celle-ci n'a rien à lier — même raison.
                ignored.append(
                    ProfileImportRowOutcome(
                        row=verdict.row_index, email=email, reason="missing_identity"
                    )
                )
                continue
            existing = await self.repo.get_for_agency(agent.agency_id, profile_id)
            assert existing is not None
            self._apply_values(existing, verdict.person, fill_gaps_only=True)
            linked.append(
                ProfileImportRowOutcome(row=verdict.row_index, email=email, profile_id=profile_id)
            )

        # LA DONNÉE DE LA LIGNE JETÉE (correctif c) : une ligne ignorée pour
        # `missing_identity` dont l'email a produit une fiche PAR UNE AUTRE
        # ligne emportait ses valeurs dans la tombe (2 téléphones perdus sur
        # le fichier Teamleader réel). Elles se reportent désormais en
        # FILL-ONLY — la donnée existe, l'identité vient d'ailleurs. La
        # ligne reste `ignored` (elle n'a créé aucune fiche) mais son
        # `profile_id` dit où sa donnée est allée.
        values_salvaged = 0
        for verdict in verdicts:
            if verdict.status != "ignore" or verdict.reason != "missing_identity":
                continue
            email = verdict.person.get("email")
            profile_id = created_by_email.get(email) if email else None
            if profile_id is None:
                continue
            payload = {k: v for k, v in verdict.person.items() if k not in IDENTITY_TARGETS}
            if not payload:
                continue
            sibling = await self.repo.get_for_agency(agent.agency_id, profile_id)
            assert sibling is not None
            self._apply_values(sibling, payload, fill_gaps_only=True)
            ignored_by_row[verdict.row_index].profile_id = profile_id
            values_salvaged += 1
        await self.db.commit()
        return ProfileImportReport(
            total_rows=len(verdicts),
            created=created,
            linked=linked,
            ignored=ignored,
            created_with_email=sum(1 for c in created if c.email),
            created_without_email=sum(1 for c in created if not c.email),
            values_salvaged=values_salvaged,
            tags_applied=sum(1 for v in verdicts if v.status != "ignore" and v.person.get("tags")),
            fields_created=fields_created,
        )

    @staticmethod
    def _apply_values(
        profile: ClientProfile, person: dict[str, Any], *, fill_gaps_only: bool = False
    ) -> None:
        """Pose les valeurs NORMALISÉES par l'analyse. `fill_gaps_only`
        (liaison) : l'existant gagne toujours — l'import ne comble que
        les trous, et sur une adresse ce sont les trous SOUS-CHAMP par
        SOUS-CHAMP (cf. `_merge_gap`)."""
        sack = dict(profile.custom_fields or {})
        changed = False
        for target, value in person.items():
            if target in IDENTITY_TARGETS:
                continue
            if target == "tags":
                if not (fill_gaps_only and profile.tags):
                    profile.tags = value
                continue
            if target == "preferred_lang":
                if not (fill_gaps_only and profile.preferred_lang):
                    profile.preferred_lang = value
                continue
            if target in CIVIL_COLUMNS:
                if fill_gaps_only and not _is_empty(getattr(profile, target, None)):
                    continue
                setattr(profile, target, value)
            elif fill_gaps_only:
                merged = _merge_gap(sack.get(target), value)
                if merged is None:
                    continue
                sack[target] = merged
                changed = True
            else:
                sack[target] = value
                changed = True
        if changed:
            profile.custom_fields = sack


# --- IMPORT SOCIÉTÉS (complément B + lot aperçu) --------------------------------------


class CompanyImportRequest(BaseModel):
    """Import de fiches SOCIÉTÉ — endpoint séparé (même verdict que
    l'annuaire : cibles disjointes de l'import personnes). Cibles :
    `name` (dénomination, la clé de dédup, OBLIGATOIRE au mapping) + les
    presets company de la taxonomie posée. Les clés libres restent au
    PATCH (pas de référentiel société au MVP — écart nommé)."""

    model_config = ConfigDict(extra="forbid")

    csv_text: str | None = None
    file_b64: str | None = None
    filename: str | None = None
    mapping: dict[str, str] = Field(min_length=1)
    corrections: list[ImportCorrection] = Field(default_factory=list)
    # Création depuis la grille — côté société le « champ » est une CLÉ DE
    # SACK (pas de référentiel société au MVP, écart nommé) ; coercé par
    # le kind, rangé en misc.
    create_fields: list[FieldCreationSpec] = Field(default_factory=list)


class CompanyImportPreviewRequest(CompanyImportRequest):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)


class CompanyImportRowOutcome(BaseModel):
    row: int
    name: str | None = None
    company_profile_id: uuid.UUID | None = None
    reason: str | None = None


class CompanyImportReport(BaseModel):
    total_rows: int
    created: list[CompanyImportRowOutcome]
    linked: list[CompanyImportRowOutcome]
    ignored: list[CompanyImportRowOutcome]
    tags_applied: int = 0
    fields_created: list[str] = Field(default_factory=list)


class CompanyImportPreviewResponse(BaseModel):
    total_rows: int
    summary: ImportPreviewSummary
    rows: list[RowVerdict]
    page: int
    page_size: int
    fields_created: list[str] = Field(default_factory=list)


class CompanyImportManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _analyze(self, agent: Agent, body: CompanyImportRequest) -> list[RowVerdict]:
        """La même garantie que les personnes : UNE analyse, zéro écriture."""
        from src.company_profiles.company_profiles_repository import CompanyProfilesRepository

        if body.csv_text is None and body.file_b64 is None:
            raise ValidationError("Provide csv_text or file_b64.", code="import.source_missing")
        content: bytes | str = (
            base64.b64decode(body.file_b64) if body.file_b64 else (body.csv_text or "")
        )
        parsed = parse_upload(body.filename, content)

        from src.client_profiles.profile_sections import COMPANY_TARGET_ALIASES

        # L'univers société vient de `import_targets` — LA source (le plan
        # de valeurs, ses alias, les deux bases adresse en sous-champs, et
        # les clés BAPTISÉES de l'agence avec leur kind de naissance).
        from src.imports.import_targets import company_targets

        targets = await company_targets(self.db, agent.agency_id)
        company_address_bases = targets.address_bases
        dotted_targets = targets.dotted
        labels_by_key = targets.labels_by_key
        keys_by_label = targets.keys_by_label
        valid_targets = targets.valid
        bad_targets = sorted(set(body.mapping.values()) - valid_targets)
        if bad_targets:
            raise ValidationError(
                f"Unknown company import targets: {', '.join(bad_targets)}.",
                code="import.unknown_targets",
                params={"targets": bad_targets},
            )
        if "name" not in body.mapping.values():
            raise ValidationError(
                "The mapping must bind one column to 'name' (the dedup key).",
                code="import.name_target_required",
            )
        mapped = set(body.mapping.values()) | {c.target for c in body.corrections}
        for base in company_address_bases:
            if base in mapped and any(t.startswith(base + ".") for t in mapped):
                raise ValidationError(
                    f"{base!r} is mapped both as full text and by sub-fields.",
                    code="import.address_mode_conflict",
                    params={"target": base},
                )
        street_pairs = _resolve_street_pairs(body.mapping, dotted_targets)
        unknown_columns = sorted(set(body.mapping) - set(parsed.headers))
        if unknown_columns:
            raise ValidationError(
                f"Mapped columns absent from the file: {', '.join(unknown_columns)}.",
                code="import.unknown_columns",
                params={"columns": unknown_columns},
            )

        from src.imports.value_normalizers import slugify_field_label

        creation_plan: dict[str, tuple[str, str, str, bool]] = {}
        for spec in body.create_fields:
            if spec.column not in parsed.headers:
                raise ValidationError(
                    f"Column {spec.column!r} is absent from the file.",
                    code="import.unknown_columns",
                    params={"columns": [spec.column]},
                )
            if spec.column in body.mapping:
                raise ValidationError(
                    f"Column {spec.column!r} is both mapped and marked for creation.",
                    code="import.create_field_conflict",
                    params={"column": spec.column},
                )
            slug = slugify_field_label(spec.label)
            # Dédup lier-pas-dupliquer : le LABEL déjà connu de l'agence
            # (casse ignorée) OU sa clé → on LIE, le kind de NAISSANCE
            # coerce (celui de la table de labels, pas celui du payload).
            existing_key = keys_by_label.get(spec.label.strip().lower())
            if existing_key is None and slug in labels_by_key:
                existing_key = slug
            if existing_key is not None:
                creation_plan[spec.column] = (
                    existing_key,
                    spec.label,
                    labels_by_key[existing_key].kind,
                    False,
                )
                continue
            # Dédup : le slug retombe sur une cible connue → on LIE.
            creation_plan[spec.column] = (slug, spec.label, spec.kind, slug not in valid_targets)
        self._creation_plan = creation_plan

        corrections_by_row: dict[int, list[ImportCorrection]] = {}
        for correction in body.corrections:
            corrections_by_row.setdefault(correction.row_index, []).append(correction)
        columns_by_target = {t: c for c, t in body.mapping.items()}
        columns_by_target.update({key: col for col, (key, _l, _k, _c) in creation_plan.items()})
        columns_by_target.update({t: " + ".join(p.columns) for t, p in street_pairs.items()})

        repo = CompanyProfilesRepository(self.db)
        # LE SYMÉTRIQUE du SELECT groupé fiches (anti N+1) : une pré-passe
        # légère relève les noms du fichier (colonnes mappées, corrections)
        # avant la boucle — surset volontaire : un nom de trop dans le IN
        # est inoffensif, un nom manquant fausserait le verdict.
        name_columns = [c for c, t in body.mapping.items() if t == "name"]
        candidate_names: set[str] = set()
        for index, row in enumerate(parsed.rows, start=1):
            cell = ""
            for column in name_columns:
                raw_cell = (row.get(column) or "").strip()
                if raw_cell:
                    cell = raw_cell
            for correction in corrections_by_row.get(index, ()):
                if correction.target == "name":
                    cell = correction.value.strip()
            if cell:
                candidate_names.add(cell.lower())
        existing_by_name = await repo.ids_for_names(agent.agency_id, candidate_names)

        verdicts: list[RowVerdict] = []
        seen_names: dict[str, int] = {}
        for index, row in enumerate(parsed.rows, start=1):
            issues: list[RowIssue] = []
            values: dict[str, str] = {}
            for column, target in body.mapping.items():
                if target in street_pairs:
                    continue  # le couple s'assemble ci-dessous, ordonné
                cell = (row.get(column) or "").strip()
                if cell:
                    values[target] = cell
            for target, pair in street_pairs.items():
                joined, orphan_number = _assemble_street_pair(pair, row)
                if joined:
                    values[target] = joined
                elif orphan_number:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get(target, target),
                            code="street_number_orphan",
                        )
                    )
            for correction in corrections_by_row.get(index, ()):
                if correction.target not in valid_targets:
                    issues.append(RowIssue(column="(correction)", code="unknown_target"))
                    continue
                corrected = correction.value.strip()
                if corrected:
                    values[correction.target] = corrected
                else:
                    values.pop(correction.target, None)
            # Alias → clé canonique, puis coercitions TYPÉES (la règle
            # absolue : échec de cellule = issue + trou, jamais un 500).
            for alias, canonical in COMPANY_TARGET_ALIASES.items():
                if alias in values:
                    values.setdefault(canonical, values.pop(alias))
            if "country" in values:
                from src.custom_fields.custom_fields_validation import _coerce_country

                try:
                    values["country"] = _coerce_country(values["country"])
                except ValueError:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get("country", "country"),
                            code="invalid_value",
                        )
                    )
                    values.pop("country")
            if "email" in values:
                values["email"] = values["email"].lower()
            # Cibles numériques (audit catalogue) : effectif/capital coercés
            # en number — « 51-200 » = issue + trou, jamais un 500.
            from src.client_profiles.profile_sections import COMPANY_NUMBER_TARGETS
            from src.custom_fields.custom_fields_validation import _coerce_number
            from src.imports.value_normalizers import normalize_number_value

            number_values: dict[str, int | float] = {}
            for number_target in COMPANY_NUMBER_TARGETS:
                raw_number = values.pop(number_target, None)
                if raw_number is None:
                    continue
                try:
                    number_values[number_target] = _coerce_number(
                        normalize_number_value(raw_number)
                    )
                except ValueError:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get(number_target, number_target),
                            code="invalid_value",
                        )
                    )
            from shared.models.custom_field import CustomFieldDefinition as _Def
            from src.custom_fields.custom_fields_validation import _coerce_one
            from src.imports.value_normalizers import assemble_address

            for column, (key, _label, kind, _to_create) in creation_plan.items():
                cell = (row.get(column) or "").strip()
                if not cell:
                    continue
                try:
                    values[key] = _coerce_one(
                        _Def(agency_id=agent.agency_id, key=key, label=key, field_type=kind),
                        cell,
                    )
                except ValueError:
                    issues.append(RowIssue(column=column, code="invalid_value"))
            # Clés à label mappées DIRECTEMENT : coercées par leur kind de
            # naissance — échec = issue + trou, jamais 500 (règle absolue).
            plan_keys = {key for key, _l, _kd, _c in creation_plan.values()}
            for label_key, label_row in labels_by_key.items():
                if label_key not in values or label_key in plan_keys:
                    continue
                try:
                    values[label_key] = _coerce_one(
                        _Def(
                            agency_id=agent.agency_id,
                            key=label_key,
                            label=label_key,
                            field_type=label_row.kind,
                        ),
                        values[label_key],
                    )
                except ValueError:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get(label_key, label_key),
                            code="invalid_value",
                        )
                    )
                    values.pop(label_key)

            address_values: dict[str, dict[str, str]] = {}
            for base in company_address_bases:
                parts = {
                    t.split(".", 1)[1]: values.pop(t)
                    for t in list(values)
                    if t.startswith(base + ".")
                }
                if not parts:
                    continue
                assembled, failed = assemble_address(parts)
                for sub in failed:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get(f"{base}.{sub}", f"{base}.{sub}"),
                            code="invalid_value",
                        )
                    )
                if assembled:
                    address_values[base] = assembled
            name = values.get("name")
            person: dict[str, Any] = dict(values)
            person.update(number_values)
            person.update(address_values)
            if values.get("tags"):
                person["tags"] = list(
                    dict.fromkeys(
                        t.strip() for t in values["tags"].replace(";", ",").split(",") if t.strip()
                    )
                )
            if not name:
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        status="ignore",
                        reason="no_name",
                        person=person,
                        issues=issues,
                    )
                )
                continue
            key = name.strip().lower()
            existing_id = existing_by_name.get(key)
            if existing_id is not None:
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        status="link",
                        profile_id=existing_id,
                        person=person,
                        issues=issues,
                    )
                )
                continue
            if key in seen_names:
                verdicts.append(
                    RowVerdict(row_index=index, status="link", person=person, issues=issues)
                )
                continue
            seen_names[key] = index
            verdicts.append(
                RowVerdict(row_index=index, status="create", person=person, issues=issues)
            )
        return verdicts

    async def preview(
        self, agent: Agent, body: CompanyImportPreviewRequest
    ) -> CompanyImportPreviewResponse:
        verdicts = await self._analyze(agent, body)
        start = (body.page - 1) * body.page_size
        return CompanyImportPreviewResponse(
            total_rows=len(verdicts),
            summary=_summarize(verdicts),
            rows=verdicts[start : start + body.page_size],
            page=body.page,
            page_size=body.page_size,
            fields_created=[
                label
                for _k, label, _kd, to_create in getattr(self, "_creation_plan", {}).values()
                if to_create
            ],
        )

    async def run_import(self, agent: Agent, body: CompanyImportRequest) -> CompanyImportReport:
        from shared.models.company_profile import CompanyFieldLabel, CompanyProfile
        from src.company_profiles.company_profiles_repository import CompanyProfilesRepository

        verdicts = await self._analyze(agent, body)
        fields_created = [
            label
            for _k, label, _kd, to_create in getattr(self, "_creation_plan", {}).values()
            if to_create
        ]
        # Demande design A : le label (et le kind) de la naissance se
        # GRAVE au niveau agence — une vérité par clé, jamais une copie
        # par société ; le batch reste transactionnel (commit unique).
        label_keys_born: set[str] = set()
        for key, label, kind, to_create in getattr(self, "_creation_plan", {}).values():
            if not to_create or key in label_keys_born:
                continue
            label_keys_born.add(key)
            self.db.add(
                CompanyFieldLabel(agency_id=agent.agency_id, key=key, label=label, kind=kind)
            )
        repo = CompanyProfilesRepository(self.db)
        created: list[CompanyImportRowOutcome] = []
        linked: list[CompanyImportRowOutcome] = []
        ignored: list[CompanyImportRowOutcome] = []
        created_by_name: dict[str, uuid.UUID] = {}
        for verdict in verdicts:
            name = verdict.person.get("name")
            key = name.strip().lower() if name else None
            if verdict.status == "ignore":
                ignored.append(
                    CompanyImportRowOutcome(row=verdict.row_index, name=name, reason=verdict.reason)
                )
                continue
            if verdict.status == "create":
                company = CompanyProfile(agency_id=agent.agency_id, name=name)
                self._fill_gaps(company, verdict.person)
                self.db.add(company)
                await self.db.flush()
                assert key is not None
                created_by_name[key] = company.id
                created.append(
                    CompanyImportRowOutcome(
                        row=verdict.row_index, name=name, company_profile_id=company.id
                    )
                )
                continue
            company_id = verdict.profile_id or (created_by_name.get(key) if key else None)
            if company_id is None:
                ignored.append(
                    CompanyImportRowOutcome(row=verdict.row_index, name=name, reason="no_name")
                )
                continue
            existing_company = await repo.get_for_agency(agent.agency_id, company_id)
            assert existing_company is not None
            self._fill_gaps(existing_company, verdict.person)
            linked.append(
                CompanyImportRowOutcome(
                    row=verdict.row_index, name=name, company_profile_id=company_id
                )
            )
        await self.db.commit()
        return CompanyImportReport(
            total_rows=len(verdicts),
            created=created,
            linked=linked,
            ignored=ignored,
            tags_applied=sum(1 for v in verdicts if v.status != "ignore" and v.person.get("tags")),
            fields_created=fields_created,
        )

    def _fill_gaps(self, company: Any, values: dict[str, Any]) -> None:
        from src.client_profiles.profile_sections import COMPANY_PRESET_PROFILE_SECTION

        if values.get("tags") and not company.tags:
            company.tags = values["tags"]
        plan_keys = {k for k, _l, _kd, _c in getattr(self, "_creation_plan", {}).values()}
        sack = dict(company.custom_fields or {})
        changed = False
        for key in set(COMPANY_PRESET_PROFILE_SECTION) | plan_keys:
            raw = values.get(key)
            if raw is None:
                continue
            # Même règle que la face personne : au SOUS-CHAMP sur les
            # adresses (`address`, `headquarters_address`), à l'objet
            # ailleurs — l'ordre des passes cesse de compter.
            merged = _merge_gap(sack.get(key), raw)
            if merged is None:
                continue
            sack[key] = merged
            changed = True
        if changed:
            company.custom_fields = sack
