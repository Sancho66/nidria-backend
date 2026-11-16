# Support privacy and one-off signatures

Prepared on 2026-11-16 for the two product tasks created that day. This document
describes the implementation, not a production deployment.

## Support boundary

`enforce()` evaluates `support_access.policy` before ordinary permissions,
impersonation, consent and billing checks. A platform operator never inherits
access to customer content by entering an agency. Current and legacy masks resolve
their original actor; deactivation or demotion invalidates the support session.
Ordinary agency administrators can still preview their own members and clients.

The policy is an explicit method/path allowlist, closed for new routes by default.
It allows reviewed agency metadata (configuration, members, reusable journeys and
templates, billing), anonymous case counts and step statuses. Case/profile lists,
exports, messages, notes, documents, signing capabilities, client previews and
free-text search journals are inaccessible through their ordinary routes.

This also restricts a platform account in its own home agency, including the Email
module. Ordinary agency accounts keep their existing permissions. No internal-agency
exception is implicit. Platform administration of tasks, demos, subscriptions,
email-module rights and scheduled jobs remains available in the original session.
Job responses retain reviewed aggregate counters and statuses; historical log
text, exception messages, nested provider payloads and unknown statistic keys are
redacted on serialization. Stored audit rows are not destroyed. Demo adoption uses
counts, not client identities; demo content lives in the isolated fictional database.
Platform tasks contain team-authored task text, not an automatic customer-data join.

## Exceptional access

An agency's actual administrator opens the Support tab of a case and chooses a
platform operator and a duration of 15, 30, 60, 120 or 240 minutes. The screen requires
explicit confirmation. The server checks the real actor, agency, case and operator;
neither platform operators nor impersonated administrators can grant access.

- `GET /support/operators`: available operators, agency administrator only.
- `GET /support/cases`: anonymous paginated overview for support, 25 rows by default.
- `GET /support/cases/{id}/content`: an authorized read-only projection of this case.
- `GET /support/cases/{id}/documents/{document_id}/download`: scoped bytes, never a
  storage URL or provider capability.
- `GET/POST /cases/{id}/support-access`: journal and new grant, agency administrator.
- `DELETE /cases/{id}/support-access/{grant_id}`: immediate revocation for new reads,
  available even under a billing hold or a pending legal consent.

Each content/download request checks an active grant for the exact case and operator
and commits a content-free event before returning data. Failure to write that event
fails the read. Responses are `Cache-Control: no-store, private`. The agency sees
who, when and what in a paginated journal, including grants and revocations.
Consultations show at most 500 entries per category, with truncation indicated.
The UI clears content on expiry, refused requests or the next 15-second grant refresh.
Revocation cannot retract bytes already delivered or a download already authorized.

`n8f4c0e6a2b9` adds grants and audit events with foreign keys and deny-all RLS. The
backend retains its normal database-owner access; the public Supabase API gains none.

## One-off signature flow

Both the case's Documents tab and the Signatures page offer a one-off document.
The agency chooses a client/case, a non-completed step, one person with a client
space, a document name and a fresh PDF. Preparing fields uses the existing DocuSeal
builder. A separate confirmation creates the signature request. Closing the builder
keeps a private draft that can be resumed or deleted from the case.

API prefix: `/cases/{case_id}/signature-drafts`. GET requires `case.view`; create,
builder-token, builder-sync, submit and delete require `case.edit`. These operations
do not require permission to configure reusable journey templates. Existing feature
flags, PDF validation, credit checks, provider adapter, reminders, signing page,
completion webhook and signed-PDF filing remain authoritative.

`p9a5d1f7b3c0` adds nullable case, step and person references plus `submitted_at` to
`document_template`. A check constraint requires either a complete private scope or
a reusable template. The private provider template is an implementation detail: it
never appears in the reusable library, cannot be fetched through library routes and
cannot be attached to a different journey or case. The product has no conversion to
a reusable template.

Confirmation locks the draft and step, creates a concrete case requirement and uses
the existing signature ledger. Repeated confirmations are idempotent. An active step
sends immediately; a future step queues until normal activation. A completed step
must first be reopened. Duplicate document labels on the step are rejected, and the
signing engine rejects any group that would mix different PDFs under one label.

The sandbox mounts only the new GET list through its existing case-scoped read
router. No mutation or provider route is added. The agency consent controls are
hidden in the public sandbox.

## Verification and delivery

Tests use disposable PostgreSQL databases and mocked external services. Coverage
includes legacy masks, isolation, administrator consent, expiry/revocation, billing
and legal holds, audit failures, job-log redaction, private-template isolation,
one-off completion and credit use, duplicate submission, future-step activation,
reminders, label collisions, and the migration upgrade/downgrade cycle.

The frontend contract is generated from `openapi.json`. All new text is provided in
FR, EN, ES, HU, IT, PT and RU. The browser harness uses fictional data, blocks external
network requests and stubs the existing DocuSeal builder. It does not send a real PDF
to DocuSeal or deliver a real email.

Publish only after the repository's explicit commit/deployment GO and green CI.
Apply migrations before serving the new code. Prepare the sandbox schema and reader
grants before publishing its API, then publish the production API and frontend.
Run read-only deployment checks and update the task status only after delivery.

A code rollback must retain the new schema: downgrading the one-off migration deletes
private template rows to prevent them becoming reusable templates. Do not use that
downgrade as a routine production rollback. PostgreSQL development was backed up and
upgraded locally to `p9a5d1f7b3c0`; no production migration was run in preparation.
