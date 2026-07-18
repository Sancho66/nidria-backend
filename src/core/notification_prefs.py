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
