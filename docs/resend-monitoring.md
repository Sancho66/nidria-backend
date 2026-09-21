# Shared Resend account monitoring

This is a separate batch from the invitation choice. It never sends a monitoring
email, changes a subscription, replays historical invitations, or polls Resend.

## Verified provider contract

Sources: https://resend.com/docs/api-reference/rate-limit and
https://resend.com/docs/api-reference/errors (checked 2026-09-21).
`x-resend-daily-quota` and `x-resend-monthly-quota` are **used** email counts.
The daily header applies only to free accounts. Rate-limit remaining/reset headers
are unrelated to email usage. Both sent and received emails can count at Resend.

The pinned and installed Python SDK 2.30.1 returns `http_headers` on successful
`Emails.send` responses and `.headers` plus `.error_type` on `ResendError`.
Tests run that SDK against an in-memory HTTP transport, including all three 429
categories. One invocation makes one transport call, with no automatic SDK retry.
No real provider credentials or paid sends are needed for this proof.

## Observation and operational surface

`send_email` observes successful sends and provider refusals. The global
`email_account_state` row stores nullable observations, last observation time,
cooldown and deduplication state. It is platform scoped: every agency shares the
same account, state and limits. A row lock serializes updates across processes.
The additive table has deny-all RLS, like the existing backend-owned tables.

Crossing a configured threshold creates one task in the **existing platform
administration task list** (`/admin/tasks`), assigned to an active platform
operator. There is no email dispatch or watcher notification from this path.
Deduplication is per daily/monthly period and threshold, persisted independently
of task completion/deletion. A first observation at 100% creates alerts for every
crossed threshold (80/90/100). A quota-exceeded error creates a 100% alert even if
consumption/limit is unknown, without inventing a numeric used count.

`GET /admin/email-usage`, protected by `platform.task.manage`, returns both quota
observations, optional limits/percentages and the active cooldown. Missing,
malformed, stale or previous-period usage is `null`/`unknown`, never zero. This
endpoint reads our database only; it makes no provider call.

## Configuration

- `RESEND_MONITOR_ENABLED`: false by default; true in this batch's Fly config.
- `RESEND_DAILY_LIMIT`, `RESEND_MONTHLY_LIMIT`: positive integers, default unknown.
  Confirm actual account limits; do not infer the account plan from this incident.
- `RESEND_MONTHLY_RESET_DAY`: 1-31, default unknown. Confirm the account's actual
  cycle; the configured day is interpreted at midnight UTC and clipped to month
  end. Daily periods always reset at midnight UTC.
- `RESEND_QUOTA_THRESHOLDS`: JSON array, default `[80,90,100]`, values 1-100.
- `RESEND_USAGE_MAX_AGE_SECONDS`: default 3600; stale observations become unknown.
- `RESEND_MONTHLY_COOLDOWN_SECONDS`: default 3600, minimum 60.

Unknown limits mean percentages and percentage-based alerts are unavailable.
Quota errors still alert. With an unknown monthly reset day, monthly alerts use
one stable unknown period and cannot be reliably rearmed each billing month.
Set the verified cycle before relying on recurring monthly alerts.

## Refusals and retry policy

- `daily_quota_exceeded`: shared cooldown until the next UTC midnight.
- `monthly_quota_exceeded`: shared cooldown of at least 60 seconds (default one
  hour). This is a backoff, **not a claim that the billing quota resets then**.
- `rate_limit_exceeded`: honor numeric `retry-after`, with at least one second.

During cooldown, subsequent application sends fail with the same error category
without contacting Resend. Existing job retry semantics remain intact; no email
is silently marked delivered and no immediate retry loop is introduced. Already
in-flight concurrent requests cannot be withdrawn. A monitoring database outage
logs an error and falls back to the existing send behavior; SDK sends still have
no immediate retry loop. Inspect application logs if telemetry persistence fails.

## Limits and rollout

Usage includes other senders on the shared Resend account **only when the next
observed Resend response reports it**. There is no continuous account polling;
activity outside Nidria between observations is not known. A missing daily header
is not proof of a zero count or proof of the current plan. Local observations are
not a billing ledger, delivery proof or list of Resend-scheduled messages.

No active platform operator means tasks cannot be assigned; the application logs
an error and leaves the threshold eligible for a later observation. Operational
logs are a fallback, not a replacement for the administration task list.

Deploy independently after the invitation option. The migration runs before app
startup; configure verified account limits without changing the Resend plan.
Verify `/admin/email-usage` and the next normal provider response; do not send a
real invitation just to exercise monitoring. Production header availability may
remain unknown until a normal application send occurs.

## Account configuration probe, 2026-09-21

A read-only request executed inside the running application confirms SDK 2.30.1,
configured Resend key, `MOCK_EMAIL=null` and `MOCK_SERVICES=false`. No key was
copied/read from a local dotenv file. `GET /usage` returned 404 `not_found`, with
no quota headers. This endpoint is documented as private beta and is not a usable
usage source for this account today. No beta enrollment or subscription change
was attempted. Actual plan limits remain unverified, so no default count or
percentage is invented.

A standard `Emails.list(limit=1)` SDK read also succeeded, but returned neither
quota header. Only response metadata was retained; no recipient, subject or body
was displayed or saved. This confirms read permission and SDK operation, not the
availability of quota headers on a future POST /emails. The monitor therefore
observes normal sends and preserves unknown until the provider supplies a value.
