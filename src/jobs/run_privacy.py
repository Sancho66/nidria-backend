"""Only reviewed, aggregate counters may leave the operational job journal.

Free-form logs, exception messages, nested provider payloads and unknown
keys can contain customer identities. Redaction applies to historical rows
too; it is never a regex-based attempt to recognize personal information.
"""

from typing import Any

COUNTER_KEYS = frozenset(
    {
        "agencies",
        "agencies_skipped",
        "bounces",
        "calls",
        "campaigns",
        "campaigns_failed",
        "campaigns_imported",
        "campaigns_listed",
        "campaigns_new",
        "campaigns_stale",
        "cases",
        "clicked",
        "conflicts",
        "contacts",
        "created",
        "deleted",
        "disabled",
        "dry_run",
        "dry_run_campaigns",
        "due",
        "duplicate_events",
        "emails",
        "errors",
        "excluded",
        "expired",
        "failed",
        "in_scope",
        "mails",
        "members_created",
        "members_updated",
        "missing_leads",
        "new_events",
        "no_recipient",
        "opened",
        "pause_changes",
        "pending_config",
        "profiles_created",
        "replies",
        "reply_contents",
        "sent",
        "sent_contents",
        "skipped",
        "skipped_disabled",
        "skipped_no_client_space",
        "skipped_step_done",
        "tasks",
        "unknown_leads",
        "unsubscribed",
        "variable_unsubscribed",
    }
)
REASONS = frozenset({"dry_run", "module_disabled", "external_effects_disabled"})


def safe_statistics(stats: dict[str, Any]) -> dict[str, Any]:
    clean = {
        key: value
        for key, value in stats.items()
        if key in COUNTER_KEYS and type(value) in (int, bool)
    }
    if isinstance(stats.get("reason"), str) and stats["reason"] in REASONS:
        clean["reason"] = stats["reason"]
    return clean
