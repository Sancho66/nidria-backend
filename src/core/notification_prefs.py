"""Notification preferences (lot 2026-07-18, audit §5): the AGENCY rules
what ITS clients receive (settings.notification_prefs.client), each AGENT
rules his own (agent.notification_prefs). The CRITICAL never appears here
— structurally not configurable. Absent key = the default; unknown value
in data (defensive) falls back to the default too.

`comments` drives the anti-burst window length: "grouped" = the 30-minute
case window (the demi-lot default), "on" = a short 5-minute window (every
exchange speaks, bursts still absorbed), "off" = never.
`progress_digest` is INERT until the digest job exists (next lot) — the
preference is stored and served, nothing consumes it yet."""

from datetime import timedelta

from shared.models.agency import Agency
from shared.models.agent import Agent

CLIENT_DEFAULTS = {
    "requirement_request": "on",
    "comments": "grouped",
    "reminders": "on",
    "progress_digest": "weekly",
}
CLIENT_ALLOWED = {
    "requirement_request": {"on", "off"},
    "comments": {"on", "grouped", "off"},
    "reminders": {"on", "off"},
    "progress_digest": {"weekly", "daily", "off"},
}
AGENT_DEFAULTS = {
    "comments": "grouped",
    "ready_to_validate": "on",
}
AGENT_ALLOWED = {
    "comments": {"on", "grouped", "off"},
    "ready_to_validate": {"on", "off"},
}

COMMENT_WINDOWS = {
    "on": timedelta(minutes=5),
    "grouped": timedelta(minutes=30),
}


def client_pref(agency: Agency | None, key: str) -> str:
    stored = (((agency.settings if agency else None) or {}).get("notification_prefs") or {}).get(
        "client"
    ) or {}
    value = stored.get(key)
    if value not in CLIENT_ALLOWED.get(key, set()):
        return CLIENT_DEFAULTS[key]
    return str(value)


def agent_pref(agent: Agent | None, key: str) -> str:
    stored = (agent.notification_prefs if agent else None) or {}
    value = stored.get(key)
    if value not in AGENT_ALLOWED.get(key, set()):
        return AGENT_DEFAULTS[key]
    return str(value)


def effective_client_prefs(agency: Agency | None) -> dict[str, str]:
    return {key: client_pref(agency, key) for key in CLIENT_DEFAULTS}


def effective_agent_prefs(agent: Agent | None) -> dict[str, str]:
    return {key: agent_pref(agent, key) for key in AGENT_DEFAULTS}


# --- automatic follow-ups: send, or queue for approval? ------------------------------
#
# Eloïse's promise (2026-06) was « rien ne part sans approbation », and the
# automatic follow-ups inherited it. The prod constat of the 13/08 killed that
# inheritance: 97 auto follow-ups waiting across two agencies, the oldest 17
# days old, ZERO ever sent — the feature sold as « les relances partent sans
# qu'on y pense » had never once fired. The approval queue is now a CHOICE, and
# the default is the promise: they leave on their own.
#
# The key is stored REQUIRE-shaped (absent = False = they leave), so an agency
# that never touched the setting gets the product it was sold. Manual reminders
# are untouched: they ALWAYS go through approval, whatever this says.
AUTO_REMINDERS_REQUIRE_APPROVAL_KEY = "auto_reminders_require_approval"


def auto_reminders_require_approval(agency: Agency | None) -> bool:
    """True → the automatic follow-ups land in the approval queue (the old
    behaviour, kept for whoever wants it). Absent or anything but a real
    `true` → they are created already approved and the dispatch sends them."""
    stored = ((agency.settings if agency else None) or {}).get(AUTO_REMINDERS_REQUIRE_APPROVAL_KEY)
    return stored is True
