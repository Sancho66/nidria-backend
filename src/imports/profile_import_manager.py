"""CRM profile imports share one read-only analysis for preview and execution.

A row needs a mapped email, phone, or both first and last names. Deduplication
uses email first, then normalized phone, then case/accent-insensitive full name.
Agency matches are linked; repeated keys in a file link to the first accepted
row. Existing nonempty values win and later rows only fill gaps. Imports never
create accounts, cases, invitations, or reminders.
"""

import base64
import uuid
from contextlib import suppress
from typing import Annotated, Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, TypeAdapter
from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agent import Agent
from shared.models.client_profile import ClientProfile
from src.client_profiles.backfill import CIVIL_COLUMNS
from src.client_profiles.client_profiles_repository import ClientProfilesRepository
from src.core.email import NormalizedEmailStr
from src.core.enums import ActorType
from src.core.exceptions import ValidationError
from src.custom_fields.custom_fields_repository import CustomFieldsRepository
from src.imports.batching import IMPORT_WRITE_CHUNK, chunked, insert_rows
from src.imports.csv_reader import parse_upload

# L'identité vient de `import_targets` (LA source) ; les appelants
# historiques l'importent encore d'ici, le nom reste donc visible.
from src.imports.import_targets import IDENTITY_TARGETS
from src.imports.profile_identity import IdentityKey, identity_key, identity_keys
from src.usage.usage_manager import UsageManager

_EMAIL = TypeAdapter(NormalizedEmailStr)
_NAME: TypeAdapter[str] = TypeAdapter(Annotated[str, Field(min_length=1, max_length=100)])


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


class ImportValueMapping(BaseModel):
    """Resolve one source value for every occurrence, before row corrections."""

    model_config = ConfigDict(extra="forbid")
    target: str = Field(min_length=1, max_length=100)
    source_value: str = Field(min_length=1, max_length=5000)
    value: str = Field(min_length=1, max_length=5000)


class ProfileImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    csv_text: str | None = None
    file_b64: str | None = None
    filename: str | None = None
    # {csv_column: cible} — cibles : first_name/last_name/email (identité)
    # + colonnes civiles + clés custom scope='person'.
    mapping: dict[str, str] = Field(
        min_length=1,
        description="Each row needs a mapped email, phone, or both first_name and last_name. "
        "Missing identifiers are reported per row as missing_identity.",
    )
    corrections: list[ImportCorrection] = Field(default_factory=list)
    value_mappings: list[ImportValueMapping] = Field(default_factory=list)
    require_valid_values: bool = Field(
        default=False,
        strict=True,
        description="Reject the complete import before writing if any cell remains invalid.",
    )
    # Création depuis la grille (lot grille) — dédup lier-pas-dupliquer
    # sur label/clé existants ; la déf naît à l'IMPORT seulement.
    create_fields: list[FieldCreationSpec] = Field(default_factory=list)
    # LE STATUT VOULU pour les fiches CRÉÉES (lot statut). Absent = rien
    # n'est posé, la dérivation joue comme avant (prospect sans dossier
    # vivant). Il ne touche JAMAIS une fiche liée : une fiche existante
    # garde son statut, lier ne requalifie pas — et il cède devant la
    # colonne quand le fichier en porte une.
    default_status: Literal["prospect", "client"] | None = None


class ProfileImportPreviewRequest(ProfileImportRequest):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)


class RowIssue(BaseModel):
    column: str
    code: str
    target: str | None = None
    source_value: str | None = None
    options: list[str] = Field(default_factory=list)


class ImportValueProblem(BaseModel):
    target: str
    column: str
    source_value: str
    occurrences: int
    options: list[str] = Field(default_factory=list)


class RowVerdict(BaseModel):
    # A write-plan reference, never exposed as an actual profile ID in a dry run.
    _linked_row: int | None = PrivateAttr(default=None)
    row_index: int = Field(
        description="Original one-based data record number, excluding the header. "
        "Blank records are skipped without renumbering later records."
    )
    source_row: int | None = Field(
        default=None, description="Physical Excel row when reading XLSX."
    )
    source_values: dict[str, str] = Field(default_factory=dict)
    status: Literal["create", "link", "ignore"]
    reason: str | None = None
    profile_id: uuid.UUID | None = None
    # Les valeurs NORMALISÉES par cible (post-coercition du contrat).
    person: dict[str, Any] = Field(default_factory=dict)
    issues: list[RowIssue] = Field(default_factory=list)


class ProfileImportRowOutcome(BaseModel):
    row: int = Field(description="Original one-based data record number, excluding the header.")
    source_row: int | None = None
    issues: list[RowIssue] = Field(default_factory=list)
    email: str | None = None
    profile_id: uuid.UUID | None = None
    reason: str | None = None


class ProfileImportReport(BaseModel):
    total_rows: int
    created_count: int = Field(
        ge=0,
        description=(
            "Total number of newly created profiles, with or without email; links are excluded."
        ),
    )
    created: list[ProfileImportRowOutcome]
    linked: list[ProfileImportRowOutcome]
    ignored: list[ProfileImportRowOutcome]
    # VENTILATION DES CRÉÉES (lot email optionnel) : l'agence sait tout de
    # suite combien de fiches ne pourront PAS recevoir d'espace client —
    # sans email, « Nouvelle démarche » répond 422 profile.no_email tant
    # qu'un email n'est pas posé au PATCH.
    created_with_email: int = 0
    created_without_email: int = Field(
        default=0,
        ge=0,
        description="Number of newly created profiles without an email; links are excluded.",
    )
    # Kept for response compatibility: email-only rows no longer need salvaging.
    values_salvaged: int = Field(default=0, description="Legacy counter; now always zero.")
    # LE STATUT POSÉ (lot statut), ventilé sur les fiches CRÉÉES : « 1593
    # créées dont 1593 en client ». Les créées sans override n'y figurent
    # pas — elles n'ont pas de statut posé, elles ont une dérivation.
    created_by_status: dict[str, int] = Field(default_factory=dict)
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
    value_problems: list[ImportValueProblem] = Field(default_factory=list)


def _value_problems(verdicts: list[RowVerdict]) -> list[ImportValueProblem]:
    """Aggregate the entire upload, including records outside the visible page."""
    grouped: dict[tuple[str, str], ImportValueProblem] = {}
    for verdict in verdicts:
        seen: set[tuple[str, str]] = set()
        for issue in verdict.issues:
            if not issue.target or not issue.source_value:
                continue
            key = (issue.target, issue.source_value)
            if key in seen:
                continue
            seen.add(key)
            if key not in grouped:
                grouped[key] = ImportValueProblem(
                    target=issue.target,
                    column=issue.column,
                    source_value=issue.source_value,
                    occurrences=0,
                    options=issue.options,
                )
            grouped[key].occurrences += 1
    return list(grouped.values())


def _value_mapping_index(
    rules: list[ImportValueMapping], allowed: set[str]
) -> dict[tuple[str, str], str]:
    result: dict[tuple[str, str], str] = {}
    for rule in rules:
        key = (rule.target, rule.source_value.strip())
        if rule.target not in allowed or key in result or not rule.value.strip():
            raise ValidationError(
                "Invalid or duplicate value mapping.", code="import.unknown_targets"
            )
        result[key] = rule.value.strip()
    return result


def _require_valid_values(verdicts: list[RowVerdict], required: bool) -> None:
    if required and any(verdict.issues for verdict in verdicts):
        raise ValidationError(
            "Resolve invalid cells before importing.",
            code="import.unresolved_values",
            params={"count": sum(len(verdict.issues) for verdict in verdicts)},
        )


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
            resolved_key = existing.key if existing is not None else slug
            if resolved_key in {*IDENTITY_TARGETS, "phone"}:
                raise ValidationError(
                    "Identity fields must be mapped directly, not created as custom fields.",
                    code="import.create_field_conflict",
                    params={"column": spec.column, "target": resolved_key},
                )
            if existing is not None:
                creation_plan[spec.column] = (existing.key, spec.label, spec.kind, False)
                if existing.key not in defs_by_key:
                    defs_by_key[existing.key] = existing  # coercition par la vraie déf
            else:
                creation_plan[spec.column] = (slug, spec.label, spec.kind, True)
        self._creation_plan = creation_plan
        value_rules = _value_mapping_index(
            body.value_mappings,
            set(body.mapping.values()) | {item[0] for item in creation_plan.values()},
        )

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
        for index, row in parsed.numbered_rows():
            cell = ""
            for column in email_columns:
                raw_cell = (row.get(column) or "").strip()
                if raw_cell:
                    cell = raw_cell
            cell = value_rules.get(("email", cell), cell)
            for correction in corrections_by_row.get(index, ()):
                if correction.target == "email":
                    cell = correction.value.strip()
            if cell:
                # The row analysis reports invalid cells.
                with suppress(PydanticValidationError):
                    candidate_emails.add(_EMAIL.validate_python(cell))
        existing_by_email = await self.repo.profile_ids_for_emails(
            agent.agency_id, candidate_emails
        )

        existing_by_key: dict[IdentityKey, uuid.UUID] = {
            ("email", email): profile_id for email, profile_id in existing_by_email.items()
        }
        existing_identities: dict[uuid.UUID, dict[str, Any]] = {}
        mapped_targets = set(body.mapping.values())
        if "phone" in mapped_targets or {"first_name", "last_name"} <= mapped_targets:
            for profile_id, phone, first, last in await self.repo.import_fallback_identities(
                agent.agency_id
            ):
                identity = {"phone": phone, "first_name": first, "last_name": last}
                existing_identities[profile_id] = identity
                for fallback_key in identity_keys(identity):
                    existing_by_key.setdefault(fallback_key, profile_id)

        verdicts: list[RowVerdict] = []
        seen_keys: dict[IdentityKey, int] = {}
        seen_identities: dict[int, dict[str, Any]] = {}
        for index, row in parsed.numbered_rows():
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
            source_values = dict(values)
            source_values.update({key: cell for key, (cell, _kind, _col) in creation_cells.items()})
            values = {key: value_rules.get((key, cell), cell) for key, cell in values.items()}
            creation_cells = {
                key: (value_rules.get((key, cell), cell), kind, column)
                for key, (cell, kind, column) in creation_cells.items()
            }
            # CORRECTIONS : après parse, avant validation — même moulinette.
            for correction in corrections_by_row.get(index, ()):
                if (
                    correction.target not in valid_targets
                    and correction.target not in creation_cells
                ):
                    issues.append(RowIssue(column="(correction)", code="unknown_target"))
                    continue
                if correction.target in {*IDENTITY_TARGETS, "phone"} and (
                    correction.target not in body.mapping.values()
                ):
                    issues.append(RowIssue(column="(correction)", code="unmapped_identifier"))
                    continue
                corrected = correction.value.strip()
                if correction.target in creation_cells:
                    _cell, kind, column = creation_cells[correction.target]
                    if corrected:
                        creation_cells[correction.target] = (corrected, kind, column)
                    else:
                        creation_cells.pop(correction.target)
                    continue
                if corrected:
                    values[correction.target] = corrected
                else:
                    values.pop(correction.target, None)

            person: dict[str, Any] = {}
            for target in IDENTITY_TARGETS:
                if not values.get(target):
                    continue
                try:
                    adapter = _EMAIL if target == "email" else _NAME
                    person[target] = adapter.validate_python(values[target])
                except PydanticValidationError:
                    issues.append(
                        RowIssue(column=columns_by_target.get(target, target), code="invalid_value")
                    )
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
            # LE STATUT (lot statut) — par COLONNE ici ; le défaut global
            # de la requête ne s'applique qu'aux lignes muettes (plus bas,
            # à l'écriture). Illisible = trou motivé, la ligne vit.
            if values.get("status_override"):
                from src.imports.value_normalizers import normalize_status_value

                status = normalize_status_value(values["status_override"])
                if status is not None:
                    person["status_override"] = status
                else:
                    issues.append(
                        RowIssue(
                            column=columns_by_target.get("status_override", "status_override"),
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
            from src.imports.value_resolution import coerce_import_field, country_value

            for civil in CIVIL_COLUMNS:
                raw = values.get(civil)
                if raw is None:
                    continue
                raw = normalize_import_value(civil, raw)
                if civil == "nationality":
                    raw = country_value(raw)
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
                    person[key] = coerce_import_field(definition, cell)
                except ValueError:
                    issues.append(RowIssue(column=column, code="invalid_value"))
            custom_keys = (set(values) & (set(defs_by_key) | preset_person_keys)) - {
                *CIVIL_COLUMNS,
                *IDENTITY_TARGETS,
            }
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
                    defs_by_key[key] = definition
                raw = normalize_import_value(key, raw, definition.option_values or None)
                try:
                    if definition.field_type == CustomFieldType.ADDRESS.value:
                        person[key] = _coerce_one(definition, {"street": raw})
                    else:
                        person[key] = coerce_import_field(definition, raw)
                except ValueError:
                    issues.append(
                        RowIssue(column=columns_by_target.get(key, key), code="invalid_value")
                    )

            dedup_key = identity_key(person)
            if dedup_key is None:
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        source_row=parsed.source_rows.get(index),
                        source_values=source_values,
                        status="ignore",
                        reason="missing_identity",
                        person=person,
                        issues=issues,
                    )
                )
                continue
            existing_id = existing_by_key.get(dedup_key)
            linked_row = seen_keys.get(dedup_key) if existing_id is None else None
            verdict = RowVerdict(
                row_index=index,
                source_row=parsed.source_rows.get(index),
                source_values=source_values,
                status="link" if existing_id is not None or linked_row is not None else "create",
                profile_id=existing_id,
                person=person,
                issues=issues,
            )
            verdict._linked_row = linked_row
            verdicts.append(verdict)
            # Mirror fill-gap writes so later rows see the same identity as a replay.
            if existing_id is not None:
                identity = existing_identities.setdefault(existing_id, {})
            else:
                source_row = linked_row if linked_row is not None else index
                identity = seen_identities.setdefault(source_row, {})
            for target in (*IDENTITY_TARGETS, "phone"):
                if not identity.get(target) and person.get(target):
                    identity[target] = person[target]
            for available_key in identity_keys(identity):
                if existing_id is not None:
                    existing_by_key.setdefault(available_key, existing_id)
                else:
                    seen_keys.setdefault(available_key, source_row)

        for verdict in verdicts:
            for issue in verdict.issues:
                issue.target = next(
                    (key for key, column in columns_by_target.items() if column == issue.column),
                    None,
                )
                issue.source_value = verdict.source_values.get(issue.target or "")
                definition = defs_by_key.get(issue.target or "")
                if definition is not None:
                    issue.options = definition.option_values

        # LE DÉFAUT GLOBAL, posé ICI et nulle part ailleurs : dans
        # l'ANALYSE, donc l'aperçu montre exactement ce que l'import
        # écrira (la garantie du projet — une seule fonction décide).
        # Sur les seules lignes qui CRÉENT, et seulement si la ligne n'a
        # rien dit : lier ne requalifie pas, une colonne prime.
        if body.default_status is not None:
            for verdict in verdicts:
                if verdict.status == "create" and not verdict.person.get("status_override"):
                    verdict.person["status_override"] = body.default_status
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
            value_problems=_value_problems(verdicts),
            fields_created=[
                label
                for _key, label, _kind, to_create in getattr(self, "_creation_plan", {}).values()
                if to_create
            ],
        )

    # --- import réel : ÉCRIT ce que l'analyse a décidé --------------------

    async def run_import(self, agent: Agent, body: ProfileImportRequest) -> ProfileImportReport:
        verdicts = await self._analyze(agent, body)
        _require_valid_values(verdicts, body.require_valid_values)
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
        created_by_row: dict[int, uuid.UUID] = {}
        # LECTURE GROUPÉE (lot batch) : les fiches à lier arrivent EN UN
        # COUP, avant la boucle — le `get_for_agency` par ligne qui vivait
        # ici coûtait 1543 allers-retours sur le fichier réel. La boucle
        # qui suit ne parle plus à la base du tout : elle décide, elle
        # construit, elle mute des objets déjà en mémoire.
        existing_by_id = await self.repo.by_ids_for_agency(
            agent.agency_id,
            {v.profile_id for v in verdicts if v.status == "link" and v.profile_id is not None},
        )
        # Les fiches nées DANS ce batch : jamais en base au moment où une
        # ligne suivante veut s'y lier, donc tenues ici par leur id (posé
        # en Python, cf. plus bas) et non relues.
        pending_by_id: dict[uuid.UUID, ClientProfile] = {}
        to_insert: list[ClientProfile] = []
        for verdict in verdicts:
            email = verdict.person.get("email")
            if verdict.status == "ignore":
                outcome = ProfileImportRowOutcome(
                    row=verdict.row_index,
                    source_row=verdict.source_row,
                    issues=verdict.issues,
                    email=email,
                    reason=verdict.reason,
                )
                ignored.append(outcome)
                continue
            if verdict.status == "create":
                # L'ID EST POSÉ ICI, pas arraché à la base : c'est ce qui
                # libère l'insert groupé. L'ancien `flush()` par ligne
                # n'existait que pour connaître `profile.id` (le rapport
                # le rend, et les lignes suivantes s'y lient) — un
                # aller-retour par fiche pour une valeur que Python sait
                # produire seul. Le modèle a le même `default=uuid4`.
                profile = ClientProfile(
                    id=uuid.uuid4(),
                    agency_id=agent.agency_id,
                    expat_user_id=None,
                    first_name=verdict.person.get("first_name"),
                    last_name=verdict.person.get("last_name"),
                    email=email,
                    # Les NOT NULL à défaut applicatif, posés ICI : le
                    # défaut ORM n'arrive qu'au flush, et l'insert groupé
                    # n'en fait plus (cf. `insert_rows`).
                    custom_fields={},
                    tags=[],
                    preferred_channels=[],
                )
                self._apply_values(profile, verdict.person)
                to_insert.append(profile)
                pending_by_id[profile.id] = profile
                created_by_row[verdict.row_index] = profile.id
                created.append(
                    ProfileImportRowOutcome(
                        row=verdict.row_index,
                        source_row=verdict.source_row,
                        issues=verdict.issues,
                        email=email,
                        profile_id=profile.id,
                    )
                )
                continue
            # Link to an agency profile or the first accepted row with this key.
            if verdict.profile_id is not None:
                profile_id = verdict.profile_id
            else:
                assert verdict._linked_row is not None
                profile_id = created_by_row[verdict._linked_row]
            existing = existing_by_id.get(profile_id) or pending_by_id.get(profile_id)
            assert existing is not None
            self._apply_values(existing, verdict.person, fill_gaps_only=True)
            linked.append(
                ProfileImportRowOutcome(
                    row=verdict.row_index,
                    source_row=verdict.source_row,
                    issues=verdict.issues,
                    email=email,
                    profile_id=profile_id,
                )
            )

        # ÉCRITURE GROUPÉE : les fiches partent par paquets d'INSERT —
        # mais dans UNE SEULE transaction, fermée par le commit unique
        # ci-dessous. Le paquet borne la requête (nombre de paramètres
        # liés), pas l'atomicité : un import reste tout ou rien, jamais
        # 1000 fiches écrites et un rapport jamais rendu. Ce commit
        # emporte aussi les défs nées à la volée et les UPDATE de
        # fill-gap accumulés au-dessus.
        for chunk in chunked(to_insert, IMPORT_WRITE_CHUNK):
            await self.db.execute(insert(ClientProfile).values(insert_rows(chunk)))
        # L'ÉVÉNEMENT D'IMPORT (KPI temps gagné) : une fiche née d'un
        # import ne se distingue d'aucune autre en base — rien ne le note,
        # et `source` appartient au métier de l'agence, pas à notre
        # comptabilité. On date donc le GESTE, pas la ligne. Émis dans la
        # transaction de l'import : pas d'import réussi sans sa trace, pas
        # de trace sans import.
        if to_insert:
            await UsageManager(self.db).emit(
                agency_id=agent.agency_id,
                event_type="agency.profiles_imported",
                actor_type=ActorType.AGENT,
                actor_id=agent.id,
                details={"created": len(to_insert)},
            )
        await self.db.commit()
        created_by_status: dict[str, int] = {}
        for profile in to_insert:
            if profile.status_override:
                created_by_status[profile.status_override] = (
                    created_by_status.get(profile.status_override, 0) + 1
                )
        return ProfileImportReport(
            total_rows=len(verdicts),
            created_count=len(created),
            created=created,
            linked=linked,
            ignored=ignored,
            created_by_status=created_by_status,
            created_with_email=sum(1 for c in created if c.email),
            created_without_email=sum(1 for c in created if not c.email),
            values_salvaged=0,
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
                # Complete nameless imports without renaming existing identities.
                if target != "email" and not getattr(profile, target):
                    setattr(profile, target, value)
                continue
            if target == "tags":
                if not (fill_gaps_only and profile.tags):
                    profile.tags = value
                continue
            if target == "preferred_lang":
                if not (fill_gaps_only and profile.preferred_lang):
                    profile.preferred_lang = value
                continue
            if target == "status_override":
                # LIER NE REQUALIFIE PAS (lot statut) : une fiche qui existe
                # déjà garde son statut, quoi que dise le fichier. Sans ce
                # `continue`, la colonne l'aurait écrasé — et sans la branche
                # entière, `status_override` serait tombé dans le sack, où
                # personne ne l'aurait jamais lu.
                if not fill_gaps_only:
                    profile.status_override = value
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
    value_mappings: list[ImportValueMapping] = Field(default_factory=list)
    require_valid_values: bool = Field(default=False, strict=True)
    # Création depuis la grille — côté société le « champ » est une CLÉ DE
    # SACK (pas de référentiel société au MVP, écart nommé) ; coercé par
    # le kind, rangé en misc.
    create_fields: list[FieldCreationSpec] = Field(default_factory=list)


class CompanyImportPreviewRequest(CompanyImportRequest):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)


class CompanyImportRowOutcome(BaseModel):
    source_row: int | None = None
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
    value_problems: list[ImportValueProblem] = Field(default_factory=list)
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
                    labels_by_key[existing_key].field_type,
                    False,
                )
                continue
            # Dédup : le slug retombe sur une cible connue → on LIE.
            creation_plan[spec.column] = (slug, spec.label, spec.kind, slug not in valid_targets)
        self._creation_plan = creation_plan
        value_rules = _value_mapping_index(
            body.value_mappings,
            set(body.mapping.values()) | {item[0] for item in creation_plan.values()},
        )

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
        for index, row in parsed.numbered_rows():
            cell = ""
            for column in name_columns:
                raw_cell = (row.get(column) or "").strip()
                if raw_cell:
                    cell = raw_cell
            cell = value_rules.get(("name", cell), cell)
            for correction in corrections_by_row.get(index, ()):
                if correction.target == "name":
                    cell = correction.value.strip()
            if cell:
                candidate_names.add(cell.lower())
        existing_by_name = await repo.ids_for_names(agent.agency_id, candidate_names)

        verdicts: list[RowVerdict] = []
        seen_names: dict[str, int] = {}
        for index, row in parsed.numbered_rows():
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
            source_values = dict(values)
            source_values.update(
                {
                    key: (row.get(col) or "").strip()
                    for col, (key, _l, _k, _c) in creation_plan.items()
                }
            )
            values = {key: value_rules.get((key, cell), cell) for key, cell in values.items()}
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
                from src.imports.value_resolution import country_value

                try:
                    values["country"] = _coerce_country(country_value(values["country"]))
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
                cell = value_rules.get((key, cell), cell)
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
                            field_type=label_row.field_type,
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
                        source_row=parsed.source_rows.get(index),
                        source_values=source_values,
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
                        source_row=parsed.source_rows.get(index),
                        source_values=source_values,
                        status="link",
                        profile_id=existing_id,
                        person=person,
                        issues=issues,
                    )
                )
                continue
            if key in seen_names:
                verdicts.append(
                    RowVerdict(
                        row_index=index,
                        source_row=parsed.source_rows.get(index),
                        source_values=source_values,
                        status="link",
                        person=person,
                        issues=issues,
                    )
                )
                continue
            seen_names[key] = index
            verdicts.append(
                RowVerdict(
                    row_index=index,
                    source_row=parsed.source_rows.get(index),
                    source_values=source_values,
                    status="create",
                    person=person,
                    issues=issues,
                )
            )
        for verdict in verdicts:
            for issue in verdict.issues:
                issue.target = next(
                    (key for key, column in columns_by_target.items() if column == issue.column),
                    None,
                )
                issue.source_value = verdict.source_values.get(issue.target or "")
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
            value_problems=_value_problems(verdicts),
            fields_created=[
                label
                for _k, label, _kd, to_create in getattr(self, "_creation_plan", {}).values()
                if to_create
            ],
        )

    async def run_import(self, agent: Agent, body: CompanyImportRequest) -> CompanyImportReport:
        from shared.models.company_profile import CompanyFieldDefinition, CompanyProfile
        from src.company_profiles.company_catalog import materialize_company_definitions
        from src.company_profiles.company_profiles_repository import CompanyProfilesRepository

        verdicts = await self._analyze(agent, body)
        _require_valid_values(verdicts, body.require_valid_values)
        fields_created = [
            label
            for _k, label, _kd, to_create in getattr(self, "_creation_plan", {}).values()
            if to_create
        ]
        # Demande design A, devenue le lot définitions (07/08) : la clé
        # baptisée à la grille naît DÉFINITION — label + type de la
        # naissance, section 'misc', comme toute clé libre. Une vérité par
        # clé au niveau agence, jamais une copie par société ; le batch
        # reste transactionnel (commit unique).
        #
        # Les 17 presets se matérialisent au passage : un import est une
        # ouverture d'écran comme une autre, et les cibles qu'il propose
        # doivent exister en définitions pour que l'annuaire les range.
        existing_defs = {
            d.key for d in await materialize_company_definitions(self.db, agent.agency_id)
        }
        next_position = len(existing_defs)
        for key, label, kind, to_create in getattr(self, "_creation_plan", {}).values():
            if not to_create or key in existing_defs:
                continue
            existing_defs.add(key)
            self.db.add(
                CompanyFieldDefinition(
                    agency_id=agent.agency_id,
                    key=key,
                    label=label,
                    label_i18n={},
                    field_type=kind,
                    profile_section="misc",
                    position=next_position,
                )
            )
            next_position += 1
        repo = CompanyProfilesRepository(self.db)
        created: list[CompanyImportRowOutcome] = []
        linked: list[CompanyImportRowOutcome] = []
        ignored: list[CompanyImportRowOutcome] = []
        created_by_name: dict[str, uuid.UUID] = {}
        # LECTURE GROUPÉE (lot batch), symétrique de la face personne :
        # les sociétés à lier arrivent en un coup, la boucle ne parle
        # plus à la base.
        existing_by_id = await repo.by_ids_for_agency(
            agent.agency_id,
            {v.profile_id for v in verdicts if v.status == "link" and v.profile_id is not None},
        )
        pending_by_id: dict[uuid.UUID, CompanyProfile] = {}
        to_insert: list[CompanyProfile] = []
        for verdict in verdicts:
            name = verdict.person.get("name")
            key = name.strip().lower() if name else None
            if verdict.status == "ignore":
                ignored.append(
                    CompanyImportRowOutcome(
                        row=verdict.row_index,
                        source_row=verdict.source_row,
                        name=name,
                        reason=verdict.reason,
                    )
                )
                continue
            if verdict.status == "create":
                # Id posé en Python (même raison que la face personne) :
                # le `flush()` par ligne ne servait qu'à le lire.
                company = CompanyProfile(
                    id=uuid.uuid4(),
                    agency_id=agent.agency_id,
                    name=name,
                    custom_fields={},
                    tags=[],
                )
                self._fill_gaps(company, verdict.person)
                to_insert.append(company)
                pending_by_id[company.id] = company
                assert key is not None
                created_by_name[key] = company.id
                created.append(
                    CompanyImportRowOutcome(
                        row=verdict.row_index,
                        source_row=verdict.source_row,
                        name=name,
                        company_profile_id=company.id,
                    )
                )
                continue
            company_id = verdict.profile_id or (created_by_name.get(key) if key else None)
            if company_id is None:
                ignored.append(
                    CompanyImportRowOutcome(
                        row=verdict.row_index,
                        source_row=verdict.source_row,
                        name=name,
                        reason="no_name",
                    )
                )
                continue
            existing_company = existing_by_id.get(company_id) or pending_by_id.get(company_id)
            assert existing_company is not None
            self._fill_gaps(existing_company, verdict.person)
            linked.append(
                CompanyImportRowOutcome(
                    row=verdict.row_index,
                    source_row=verdict.source_row,
                    name=name,
                    company_profile_id=company_id,
                )
            )
        # ÉCRITURE GROUPÉE : paquets d'INSERT, UNE transaction (même
        # règle que la face personne — le paquet borne la requête, pas
        # l'atomicité) ; ce commit emporte aussi les labels de clés nés
        # plus haut et les UPDATE de fill-gap.
        for chunk in chunked(to_insert, IMPORT_WRITE_CHUNK):
            await self.db.execute(insert(CompanyProfile).values(insert_rows(chunk)))
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
