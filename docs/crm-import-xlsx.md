# CRM XLSX preservation

The person and company profile endpoints share parsing and value-resolution
primitives. No account, case, invitation, or email is created by profile imports.

## Workbook boundaries and numbering

The active worksheet supplies the headers and records. Other worksheets are not
merged. The frontend resolves the same active worksheet through workbook
relationships. A structured Excel table is not a data boundary: named adjacent
columns are retained. Empty headers and explicitly labelled `Légende :` / `Legend :`
columns are excluded from contact fields. Legend-only rows do not create contacts.

Rows containing only empty/whitespace values are skipped even when cells have
formatting. Nonempty records without a mapped identity are still reported as
`missing_identity`. Records retain their original `row_index` (header excluded),
including gaps left by skipped records; XLSX adds the physical `source_row` to
preview and outcomes. Corrections use `row_index`, never the visible Excel offset.
CSV multiline values still count as one data record. Existing XLSX formula handling
reads cached results (`data_only=True`); it does not calculate formulas.

The private September workbook reproduces the issue: active Leads,64 contacts on
Excel rows2..65,935 blank rows66..1000, table A1:H65 but contact columns A:J.
The source workbook is read only and is excluded from commits. CI uses a synthetic
workbook with the same shape, partial table, legends, other sheets and formatting.

## Value chain

1. `csv_reader.parse_upload` preserves textual cell values and original positions.
2. The profile manager captures `source_values` before transformations, applies
   request-scoped value rules, then per-record corrections, then typed validation.
3. Known country labels from the existing seven-language ISO catalogue resolve to
   codes. Compound/qualified values are not split and cities are not inferred.
   Residence is never suggested as tax residence or nationality.
4. Custom select values resolve only to an exact option or a unique normalized
   label (case, accents, whitespace). Options are stored strings, not a separate
   identifier/label object. Multiselect accepts a JSON string list or semicolon/
   comma-separated labels; an exact whole option takes precedence over splitting.
5. Rejected cells include their source text, target and available options. Global
   `value_problems` aggregates all records, independent of preview pagination.
6. The UI displays validated values or the source text, never a silent blank for a
   rejected value. A rule is applied once to every occurrence for its target and
   source value. The same uploaded bytes, rules and corrections go to import.
7. The normal write path persists the validated preview values. Linking still
   fills gaps only; existing nonempty values are never overwritten by this change.

Example rule:

```json
{
  "value_mappings": [
    {"target": "link_nature", "source_value": "Connecteur/ Partenaire", "value": "connector"}
  ],
  "require_valid_values": true
}
```

Rules belong to this upload/request, not the agency/client or a process-wide map.
They reset on a new import. They are not saved as cross-file vocabulary settings.
Unknown, duplicate or unmapped rule targets are rejected. Per-row corrections
have precedence. The frontend always sets `require_valid_values: true`; any
remaining invalid cell makes the server reject the entire operation with422
`import.unresolved_values` before creating fields or profiles. The optional flag
is false by default for legacy API callers; their existing partial-valid-value
behavior remains and person outcomes now include cell issues. This compatibility
mode must not be used by the new frontend.

A source such as `Suisse / Paraguay` cannot fit a single-country destination.
The UI keeps it visible and offers returning to mapping to select/create a text
field that preserves the entire value. It never chooses one country. No new
multi-country business field or tax interpretation is introduced.

## Verification and delivery boundary

Focused tests cover64 complete synthetic profiles, all ten columns persisted,
blank versus unidentified records, original positions, global resolution across
pages, row-correction precedence, agency/request separation, country labels,
multiselect, compound-country refusal before writes, and zero accounts/invitations.
The wider import regression suite and query-budget checks also run locally.
The browser witness uses the actual frontend with synthetic XLSX and intercepted
HTTP responses captured from the isolated backend tests. It is not a production
business test. No actual contact is imported by validation.

Backend contract must be delivered before the matching frontend. No migration or
provider configuration is required. Commit and release this contract before publishing the matching frontend.
No real contact import is needed to validate deployment.
