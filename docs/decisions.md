
## 2026-10-29: imported email memberships are provider observations

A person may already participate in several Lemlist campaigns. Importing those
participations must preserve each history on the same client profile. The server
marks them `is_provider_mirror`; the live-membership unique index now applies to
native enrollments. Readiness and moves still check every live participation,
including imported ones, before enrolling a person from Nidria. The migration
backfills the flag by campaign origin and refuses a downgrade that would require
losing a recovered history.

An import exclusion is not permanent: its normalized address and mapped provider
state are fingerprinted, and a change makes it eligible for another import.
Legacy exclusions are re-examined once; native conflicts are also retried daily.
Historical events received before a membership existed can be attached and applied
once, atomically in their original agency and campaign. Replays never lift a
bounce or unsubscribe. Regular sync applies the same export status mapping as import.

Outreach displays remaining email steps of active imported sequences separately
from dated activity. These counts obey recipient/campaign filters but have no date
window, because Lemlist does not supply the future queue dates. They exclude
non-email steps, completed/sent steps and held or ended recipients, and are not a
promise that all remaining steps will be sent. The existing dated native estimates
are unchanged. Recovery and sync use provider reads only; no launch, pause, resume
or lead import is performed at Lemlist by this repair.
