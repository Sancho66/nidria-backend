"""Sector field packs (bloc packs sectoriels) — catalog additions.

Covers: (a) the 3 new sections exist with their exact field_keys + sector tag;
(b) the sector tags on the 11 pre-existing sections; (c) preferred_language
widened to 8 options (the 5 originals intact + Russian/German/Italian);
(d) NON-REGRESSION: a pre-existing preset/section is byte-identical; (e) i18n
parity ×7 (labels AND options) on every new preset. Pure catalog unit tests —
no DB, no seed."""

from src.core.enums import AgencySector
from src.journeys.field_catalog import FIELD_PRESETS, LANGS, SECTION_TYPES

# The 29 presets introduced by this lot, by their owning new section.
_NEW_PRESETS = {
    "real_estate_deal": [
        "property_deal_type",
        "property_kind",
        "property_address",
        "property_surface",
        "property_rooms",
        "property_price",
        "mandate_type",
        "mandate_number",
        "transaction_stage",
        "energy_performance",
        "expected_signing_date",
        "agency_fee_percent",
    ],
    "wealth_review": [
        "risk_profile",
        "wealth_objective",
        "investment_horizon",
        "estimated_wealth",
        "savings_capacity",
        "funds_origin",
        "held_products",
        "esg_preference",
    ],
    "consulting_mission": [
        "mission_type",
        "billing_mode",
        "daily_rate",
        "sold_budget",
        "sold_days",
        "completion_rate",
        "consumed_days",
        "expected_deliverables",
        "steering_committee_freq",
    ],
}


# --- (a) the 3 new sections ----------------------------------------------------------------


def test_new_sections_exist_with_their_fields_and_sector() -> None:
    expected_sector = {
        "real_estate_deal": AgencySector.REAL_ESTATE,
        "wealth_review": AgencySector.WEALTH,
        "consulting_mission": AgencySector.CONSULTING,
    }
    for section_key, field_keys in _NEW_PRESETS.items():
        section = SECTION_TYPES[section_key]
        assert list(section.field_keys) == field_keys
        assert section.sectors == (expected_sector[section_key],)
        # every field_key resolves to a real preset (no dangling reference).
        for key in field_keys:
            assert key in FIELD_PRESETS, key


# --- (b) sector tags on the 11 pre-existing sections ---------------------------------------


def test_sector_tags_on_existing_sections() -> None:
    expected = {
        "immigration": ("immigration", "hr_mobility", "legal"),
        "professional": ("hr_mobility",),
        "company": ("legal", "accounting"),
        "housing": ("hr_mobility",),
        "tax": ("accounting", "wealth"),
        "family_situation": ("wealth",),
        "education": ("hr_mobility",),
        "vehicle": ("hr_mobility",),
        # universal — no tag.
        "identity": (),
        "language": (),
        "contact": (),
    }
    for key, sectors in expected.items():
        got = tuple(s.value for s in SECTION_TYPES[key].sectors)
        assert got == sectors, key


# --- (c) preferred_language widening (additive) --------------------------------------------


def test_preferred_language_has_eight_options_originals_intact() -> None:
    opts = FIELD_PRESETS["preferred_language"].options
    assert opts is not None
    # 8 options in every language, added before the "Other" catch-all.
    for lang in LANGS:
        assert len(opts[lang]) == 8, lang
    # The 5 originals are intact and in order (fr reference).
    assert opts["fr"] == [
        "Français",
        "Anglais",
        "Espagnol",
        "Portugais",
        "Russe",
        "Allemand",
        "Italien",
        "Autre",
    ]
    assert opts["en"][:4] == ["French", "English", "Spanish", "Portuguese"]
    assert opts["en"][-1] == "Other"  # catch-all stays last
    assert {"Russian", "German", "Italian"} <= set(opts["en"])


# --- (d) non-regression: a pre-existing preset/section is untouched -------------------------


def test_existing_catalog_entries_did_not_move() -> None:
    # A pre-existing SELECT keeps its exact options (fr).
    assert FIELD_PRESETS["visa_type"].options is not None
    assert FIELD_PRESETS["visa_type"].options["fr"] == [
        "Court séjour",
        "Long séjour",
        "Travail",
        "Étudiant",
        "Regroupement familial",
        "Investisseur",
        "Retraité",
        "Autre",
    ]
    # A pre-existing SECTION keeps its exact field_keys and stays universal.
    assert SECTION_TYPES["identity"].field_keys == (
        "passport_number",
        "date_of_birth",
        "nationality",
        "place_of_birth",
        "sex",
        "birth_country",
        "second_nationality",
        "residence_address",
    )
    assert SECTION_TYPES["identity"].sectors == ()


# --- (e) i18n parity ×7 on the new presets -------------------------------------------------


def test_new_presets_have_full_seven_language_parity() -> None:
    new_keys = [k for keys in _NEW_PRESETS.values() for k in keys]
    assert len(new_keys) == 29
    for key in new_keys:
        preset = FIELD_PRESETS[key]
        assert set(preset.labels) == set(LANGS), f"{key} labels"
        if preset.field_type == "select":
            assert preset.options is not None, f"{key} must carry options"
            assert set(preset.options) == set(LANGS), f"{key} options langs"
            counts = {len(v) for v in preset.options.values()}
            assert len(counts) == 1, f"{key} option count differs across languages"
        else:
            assert preset.options is None, f"{key} non-select must have options=None"
