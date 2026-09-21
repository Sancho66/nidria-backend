# Per-operation client invitation choice

`POST /cases` accepts `send_invitation: boolean`, default `true`. Validation is
strict: strings, numbers, null, arrays and objects are rejected with 422.

## Creation chain

- `cases_router.create_case` validates `CaseCreateRequest` and calls
  `CasesManager.create_case`.
- The manager links or creates `ExpatUser`, creates the case and principal,
  links the agency client profile, copies optional person data and materializes
  journey steps inside the existing transaction.
- With `false`, none of the invitation-specific side effects occur: invitation
  token/row, notification window, `case.client_invited` usage event,
  `case.invitation_sent` audit event, email rendering or transport, deferred sink.
  `case.created` records `send_invitation: false` in its details.
- Existing activation/password/permissions/preferences remain untouched. An active
  account keeps its access and the existing account-activation usage signal.
- `activation_jobs.send_activation_reminders` selects pending `CaseInvitation`
  rows; no row exists for an opted-out creation. Repeated sweeps cannot resurrect
  an invitation. The import's `PendingEmail` sink also receives no entry.
- No SQLAlchemy hook or event consumer sends invitations from case/account
  creation. Usage events are persisted analytics, not an email event bus.
- Later explicit invite/resend actions remain available. This choice does not
  cancel another case's pending invitations for the same shared client.
- The form also calls `POST /cases/{id}/persons` for optional family members.
  Its strict `send_invitation` option defaults to true and preserves account and
  membership creation while suppressing invitation preparation. The create form
  passes its choice to these calls; standalone member creation sends true.

## UI and deployment

The business application form defaults to an unchecked suppression checkbox,
submits an explicit boolean, resets on success and closing, and retains the
neutral case-created success message. Labels/help are translated in all seven
languages. An OpenAPI capability check hides the option on older servers; a
second check before an opted-out submission refuses creation if support vanished.
Backend release must be healthy before frontend release. Rollbacks must follow
frontend-first order. A server rollback between capability check and POST is not
an atomic protocol; coordinated releases remain necessary.

## Existing invitations

This change does not alter the 94 historical cases or purge a queue. Their pending
invitation rows may still be eligible for the J+3/J+7 activation sweep. The initial
invitation transport has no persistent retry queue; CRM imports use process-local
FastAPI background tasks. Inspect historical pending rows read-only, scoped to the
agency and exact references, before reporting their current state. A Resend-side
scheduled message cannot be inferred from a local pending invitation row.

## Local evidence

Synthetic example.com identities only, PostgreSQL testcontainers, mock email,
mandatory pre-collection network guard. `tests/test_case_send_invitation.py`
covers default/true/false across new/inactive/active clients, strict rejection,
complete journey/profile data, deferred sink, repeated sweeps, a 103-case batch,
concurrent agencies and family creation. No real influencer data is used.
