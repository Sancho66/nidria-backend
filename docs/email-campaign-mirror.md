# Imported campaign names and statuses

`LEMLIST_IMPORT` campaigns are mirrors of campaigns managed in Lemlist.
The read-only `email_sync` job refreshes their displayed name and maps
`running` to `ACTIVE` and `paused` to `PAUSED`. The initial import and
reconciliation share `IMPORTED_CAMPAIGN_STATUSES` in `email_manager.py`.

The raw value remains available in `provider_status`. An unrecognized or
absent provider status leaves the displayed status unchanged. Reconciliation
does not reopen a local archive. Campaigns created in Nidria keep their own
name and status, and no campaign settings, steps or contents are replaced.
The existing per-campaign transaction rolls these updates back if a required
provider read fails.

This corrects the stale display observed on 2026-10-28: the sync job was
successfully reading the provider status without updating the imported
campaign's displayed status or name. Auto-import was not a remedy for already
known campaigns with no additional leads. No migration, manual reimport or
upstream pause/resume is needed; the next successful scheduled sync repairs
the mirror.

`tests/test_email_sync_engine.py` covers both state transitions, name refresh,
missing and unknown states, archives, native campaigns, transaction rollback,
API serialization and the absence of provider writes.
