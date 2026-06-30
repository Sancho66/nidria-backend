"""CRM referential catalogue (BLOC 1) — loading + the allégé projection.

Pure unit tests on the in-memory catalogue: no DB, no HTTP.
"""

from src.imports import crm_catalog


def test_list_serves_only_usable_crms() -> None:
    crms = crm_catalog.list_crms()
    # 30 in the source; only those with >= MIN_USABLE_FIELDS headers served.
    assert len(crms) == 19
    assert all(len(c.headers) >= crm_catalog.MIN_USABLE_FIELDS for c in crms)
    # sorted by display name, stable order
    names = [c.name for c in crms]
    assert names == sorted(names, key=str.lower)


def test_sub_threshold_crms_are_hidden() -> None:
    # Actionstep (0 headers) and amoCRM (1) fall below the threshold.
    served = {c.slug for c in crm_catalog.list_crms()}
    assert "actionstep" not in served
    assert "amocrm" not in served
    # and a direct lookup of a hidden CRM behaves like unknown
    assert crm_catalog.get_crm("actionstep") is None
    assert crm_catalog.get_crm("amocrm") is None


def test_slugs_are_unique() -> None:
    slugs = [c.slug for c in crm_catalog.list_crms()]
    assert len(slugs) == len(set(slugs))


def test_empty_csv_fields_are_dropped() -> None:
    """A field with no CSV header can never be a mapping source — every
    exposed header has a non-empty csv."""
    for crm in crm_catalog.list_crms():
        assert all(field.csv != "" for field in crm.headers)


def test_hubspot_projection() -> None:
    crm = crm_catalog.get_crm("hubspot-crm")
    assert crm is not None
    assert crm.name == "HubSpot CRM"
    # 18 contact fields, 3 with empty csv (owner_id, createdate, lastmodified)
    assert len(crm.headers) == 15
    by_csv = {field.csv: field for field in crm.headers}
    assert "Email" in by_csv
    assert by_csv["Email"].format == "email"
    assert by_csv["Email"].dedup is True
    assert by_csv["Record ID"].dedup is True
    assert by_csv["First name"].dedup is False
    # the API-only fields (empty csv in the source) are absent
    assert "createdate" not in by_csv


def test_pipedrive_projection() -> None:
    crm = crm_catalog.get_crm("pipedrive")
    assert crm is not None
    assert crm.name == "Pipedrive"
    # 13 contact fields, 3 with empty csv (id, add_time, update_time)
    assert len(crm.headers) == 10
    by_csv = {field.csv: field for field in crm.headers}
    assert by_csv["Email"].format == "email"
    assert by_csv["Email"].dedup is True
    assert by_csv["Email"].type == "array"


def test_unknown_slug_returns_none() -> None:
    assert crm_catalog.get_crm("does-not-exist") is None
