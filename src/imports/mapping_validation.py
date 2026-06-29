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
from src.core.enums import CustomFieldType
from src.core.exceptions import ValidationError
from src.custom_fields.custom_fields_validation import ADDRESS_SUBFIELDS
from src.imports.case_import_repository import DeclaredField

IDENTITY_TARGETS = ("email", "first_name", "last_name")
_FIELD_FAMILIES = ("base_field", "case_field", "custom_field")


@dataclass(frozen=True)
class MappingTarget:
    family: str  # "identity" | "base_field" | "case_field" | "custom_field"
    reference: str
    # Composite mapping (ADDRESS only): a sub-component of the target field fed
    # by THIS column. None for a simple 1-column→1-field mapping. When set, the
    # family is "custom_field", `reference` is the address field's key, and
    # `subpath` is one of ADDRESS_SUBFIELDS — so N columns can fill one object.
    subpath: str | None = None

    def token(self) -> str:
        """Reconstruct the source token (for error messages)."""
        if self.family == "identity":
            return self.reference
        if self.subpath is not None:
            return f"{self.family}:{self.reference}.{self.subpath}"
        return f"{self.family}:{self.reference}"


def parse_token(token: str) -> MappingTarget | None:
    if token in IDENTITY_TARGETS:
        return MappingTarget("identity", token)
    if ":" in token:
        family, reference = token.split(":", 1)
        if family not in _FIELD_FAMILIES or not reference:
            return None
        # Composite ADDRESS sub-path: custom_field:<key>.<subfield>. Keys never
        # contain a dot (^[a-z][a-z0-9_]{0,49}$), so the separator is
        # unambiguous; the sub-field must be a known address component.
        if family == "custom_field" and "." in reference:
            key, subpath = reference.rsplit(".", 1)
            if key and subpath in ADDRESS_SUBFIELDS:
                return MappingTarget(family, key, subpath=subpath)
            return None
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
        raise ValidationError("Mapping is empty.")

    declared_set = {(d.family, d.reference) for d in declared}
    targets: dict[str, MappingTarget] = {}
    bad_tokens: list[str] = []
    unknown_targets: list[str] = []
    non_address_subpaths: list[str] = []

    for column, token in mapping.items():
        target = parse_token(token)
        if target is None:
            bad_tokens.append(token)
            continue
        targets[column] = target
        if target.family == "identity":
            continue
        if target.family == "custom_field":
            definition = defs_by_key.get(target.reference)
            if definition is None:
                unknown_targets.append(token)
                continue
            # A sub-path is only meaningful on an ADDRESS field (the only
            # structured type); on any other type it is a configuration error.
            is_address = definition.field_type == CustomFieldType.ADDRESS.value
            if target.subpath is not None and not is_address:
                non_address_subpaths.append(token)
                continue
        # The membership check is on the FIELD (family, reference) — a composite
        # target's address field must be declared exactly like a whole-object one.
        if (target.family, target.reference) not in declared_set:
            unknown_targets.append(token)

    errors: list[str] = []
    if bad_tokens:
        errors.append(f"unparseable mapping targets: {sorted(bad_tokens)}")
    if unknown_targets:
        errors.append(
            f"targets not declared in the parcours Informations tab: {sorted(unknown_targets)}"
        )
    if non_address_subpaths:
        errors.append(
            f"sub-field targets only valid on an address field: {sorted(non_address_subpaths)}"
        )

    # Dedup INCLUDES the sub-path: two columns on `adresse.street` clash, but
    # `adresse.street` + `adresse.city` are distinct (that is the whole point).
    seen: set[tuple[str, str, str | None]] = set()
    dupes: set[str] = set()
    whole_object_keys: set[str] = set()  # custom fields mapped as one object
    subfield_keys: set[str] = set()  # custom fields mapped by ≥1 sub-field
    for target in targets.values():
        key = (target.family, target.reference, target.subpath)
        if key in seen:
            dupes.add(target.token())
        seen.add(key)
        if target.family == "custom_field":
            (subfield_keys if target.subpath is not None else whole_object_keys).add(
                target.reference
            )
    if dupes:
        errors.append(f"targets mapped more than once: {sorted(dupes)}")
    # A field cannot be filled BOTH as a whole object and piecemeal by sub-field.
    conflicting = sorted(whole_object_keys & subfield_keys)
    if conflicting:
        errors.append(f"address fields mapped both as a whole and by sub-field: {conflicting}")

    if errors:
        raise ValidationError("Invalid mapping — " + "; ".join(errors) + ".")
    return targets
