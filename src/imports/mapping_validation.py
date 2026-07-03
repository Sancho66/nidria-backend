"""Shared mapping-target validation (BLOC 2 import + BLOC 3 save).

The single source of truth for "is this mapping's TARGET set valid for this
parcours?": tokens parse, every non-identity target is declared in the
parcours' Informations tab (or is an active custom field), and no target is
mapped twice. It deliberately does NOT look at a CSV (no file at save time)
nor require identity to be present — those are import-time concerns layered
on top in CaseImportManager.
"""

from dataclasses import dataclass

from shared.models.custom_field import CustomFieldDefinition
from src.core.exceptions import ValidationError
from src.imports.case_import_repository import DeclaredField

IDENTITY_TARGETS = ("email", "first_name", "last_name")
_FIELD_FAMILIES = ("base_field", "case_field", "custom_field")


@dataclass(frozen=True)
class MappingTarget:
    family: str  # "identity" | "base_field" | "case_field" | "custom_field"
    reference: str


def parse_token(token: str) -> MappingTarget | None:
    if token in IDENTITY_TARGETS:
        return MappingTarget("identity", token)
    if ":" in token:
        family, reference = token.split(":", 1)
        if family in _FIELD_FAMILIES and reference:
            return MappingTarget(family, reference)
    return None


def validate_mapping_targets(
    mapping: dict[str, str],
    declared: list[DeclaredField],
    defs_by_key: dict[str, CustomFieldDefinition],
) -> dict[str, MappingTarget]:
    """Parse + validate targets against the parcours. Raises ValidationError
    (422) on an empty mapping, an unparseable token, a target not declared in
    the parcours (or an inactive custom field), or a duplicate target.
    Returns the parsed {column: target}."""
    if not mapping:
        raise ValidationError("Mapping is empty.", code="import.mapping_empty")

    declared_set = {(d.family, d.reference) for d in declared}
    targets: dict[str, MappingTarget] = {}
    bad_tokens: list[str] = []
    unknown_targets: list[str] = []

    for column, token in mapping.items():
        target = parse_token(token)
        if target is None:
            bad_tokens.append(token)
            continue
        targets[column] = target
        if target.family == "identity":
            continue
        if target.family == "custom_field" and target.reference not in defs_by_key:
            unknown_targets.append(token)
            continue
        if (target.family, target.reference) not in declared_set:
            unknown_targets.append(token)

    errors: list[str] = []
    if bad_tokens:
        errors.append(f"unparseable mapping targets: {sorted(bad_tokens)}")
    if unknown_targets:
        errors.append(
            f"targets not declared in the parcours Informations tab: {sorted(unknown_targets)}"
        )
    seen: set[tuple[str, str]] = set()
    dupes: set[str] = set()
    for target in targets.values():
        key = (target.family, target.reference)
        if key in seen:
            dupes.add(f"{target.family}:{target.reference}")
        seen.add(key)
    if dupes:
        errors.append(f"targets mapped more than once: {sorted(dupes)}")

    if errors:
        # ONE code for the whole aggregate: `detail` keeps the readable
        # english aggregation (logs / fallback), the i18n surface is the
        # token lists — always all three keys, possibly empty (the front
        # renders the non-empty ones). The import-time stage layers its
        # own keys onto the SAME code (case_import_manager._validate_mapping).
        raise ValidationError(
            "Invalid mapping — " + "; ".join(errors) + ".",
            code="import.mapping_invalid",
            params={
                "unparseable": sorted(bad_tokens),
                "undeclared": sorted(unknown_targets),
                "duplicated": sorted(dupes),
            },
        )
    return targets
