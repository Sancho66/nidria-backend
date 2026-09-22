# CRM person import identity

`POST /imports/client-profiles` and `/preview` share the same analysis.
This contract concerns CRM profiles, before any case or client account exists.
The separate journey-based `/imports/cases` contract is unchanged.

A record is accepted when the mapped, usable values contain at least one of:

- `email`, without requiring either name;
- `phone`, without requiring either name;
- both `first_name` and `last_name`.

Unmapped columns do not identify a record. Corrections can complete identifiers
only when those targets are mapped. Each person target has `required: false` in
`GET /imports/targets`: none is individually mandatory. A mapping without any
usable identity alternative produces per-record rejections, not a global failure.
Existing email validation and database name/phone length limits apply. An invalid
cell is reported as `invalid_value` in the preview; remaining identifiers can still
identify the record. Creating a custom field that resolves to `email`, `phone`,
`first_name` or `last_name` returns 422 `import.create_field_conflict`: these
identifiers must be mapped directly. A legacy custom definition with the same key
cannot override native identity validation.

## Deduplication

The incoming record selects exactly one key, in this order:

1. Normalized lowercase email, when present.
2. Normalized telephone, otherwise.
3. Exact first-name/last-name pair, ignoring case and accents, otherwise.

A selected email that does not match does not fall back to telephone or names.
A selected telephone that does not match does not fall back to names. Different
email keys remain different profiles even when phone or names coincide.

Telephone comparison removes whitespace, parentheses, periods and hyphens and
treats the international `00` prefix as `+`. Only optional `+` and digits remain.
No country is inferred: `0612345678` and `+33612345678` remain different keys.
The original readable telephone is stored. Name comparison trims outer whitespace
but preserves internal whitespace, punctuation and first/last-name boundaries;
there is no fuzzy matching.

Database lookup is agency-scoped, using the effective account identity for linked
profiles. A phone/name-only record can match an existing profile that has email.
Historical duplicate fallback keys select the oldest profile, then its UUID.
Other agencies never influence the result.

Within one file, records with the same selected key produce one creation, then
`linked` outcomes referring to that profile. Earlier accepted profiles expose all
their available identity keys, just like existing database profiles: an email-less
record can therefore link by phone to an earlier email-bearing record. If the
selected key already exists in the agency, the database match takes priority.
The first nonempty value wins; later records fill empty fields, including missing
first/last names, without overwriting existing email, names or other data. Linked records do not count as newly created profiles.

## Report and numbering

An unidentified record appears in `ignored` with `reason: "missing_identity"` and
`row`. Preview uses `status: "ignore"`, the same reason, and `row_index`.
Both numbers are original one-based **data record numbers excluding the header**.
Blank records are skipped without renumbering subsequent records. A populated
record without an identifier still produces `missing_identity`. A quoted multiline
CSV value belongs to one record. XLSX reads the active sheet only and also returns
`source_row`, the physical Excel row including the header offset. The interface
uses `source_row` when available; correction requests continue using `row_index`.
See [XLSX preservation](crm-import-xlsx.md) for value resolution and empty rows.

The required report field `created_count` is the total number of newly created
profiles, with or without email. The frontend can display this total directly
without adding other counters. `created_without_email` is the subset created
without email; `created_with_email` is the subset created with email. Linked and
rejected records are excluded from all three creation counters. These fields
accompany `total_rows` (nonempty input records), `created`, `linked` and `ignored`.
Preview exposes `summary.create_with_email` and `summary.create_without_email`.
The compatibility field `values_salvaged` remains present and is now zero: a valid
email-only record is accepted immediately instead of being rejected then salvaged.

## Profiles without email

These are normal editable CRM profiles with nullable stored email, no fake address,
no account, no case and no invitation created during import. Existing profile
responses render an absent email or name as an empty string.

The client-access path `POST /client-profiles/{id}/cases` refuses a profile without
email with HTTP 422 `profile.no_email`, before creating anything or sending mail.
After email is supplied, missing names produce HTTP 422 `profile.missing_identity`.
Reminders belong to cases; a profile ID passed to `/cases/{id}/reminders` returns
HTTP 404 `not_found`. Without a case/account, these profiles are absent from the
automatic reminder and invitation flows. No email is queued or silently sent to
an empty address.
