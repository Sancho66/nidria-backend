"""LemlistEmailProvider: the EmailProvider contract on Lemlist's public API.

Ported from Prism's `providers/lemlist.py` (reference commit 629a2b0, in
production since 2026-10-06). Every route below comes from Lemlist's API
reference (developer.lemlist.com, read by Prism on 2026-10-04) or was
measured by Prism in production; none is guessed:

    POST   /campaigns                                   create (autoReview off)
    PATCH  /campaigns/{id}                              name, stopOnEmailReplied,
                                                        tracking, sendUserIds
    PATCH  /schedules/{scheduleId}                      the campaign's own schedule
    POST   /campaigns/{id}/start | /pause
    GET    /campaigns/{id}
    GET    /campaigns/{id}/sequences                    the steps, in order
    POST   /sequences/{sequenceId}/steps                add an email step (at the end)
    PATCH  /sequences/{sequenceId}/steps/{stepId}       delay, placeholder
    DELETE /sequences/{sequenceId}/steps/{stepId}       only before any lead entered
    POST   /campaigns/{id}/leads/?deduplicate=true      add a lead (fillEmptyOnly)
    PUT    /campaigns/{id}/leads/{leadId}/steps/{stepId}/personalization
    POST   /leads/review/{leadId}                       launch (emailPro plan)
    POST   /leads/pause/{leadId}?campaignId= , /leads/start/{leadId}?campaignId=
    DELETE /campaigns/{id}/leads/{leadId}?action=remove
    GET    /leads?id= | ?email= &version=v2
    GET    /campaigns/{id}/leads?limit=&offset=          the leads and their state
    GET    /activities?version=v2&campaignId=&leadId=&minDate=
    GET    /inbox/{contactId}?limit=&page=              a contact's thread, newest
                                                        first, 10 a page (measured
                                                        09/10/2026: `skip` ignored),
                                                        sent emails' and replies'
                                                        content included (never
                                                        markAsRead: reading changes
                                                        nothing in Lemlist)
    GET    /team ; GET /team/senders ; GET /user/channels

Outside the EmailProvider protocol (Prism had no such calls; the platform
routes of spec section 10.5 need them, and only Lemlist has them):

    GET    /team                                        `read_team`: id and name
    GET    /user/channels                               `read_plan`: the "plan"
    GET    /hooks ; POST /hooks                         `list_hooks`, `create_hook`,
                                                        `ensure_hook` (no `type`:
                                                        every event)
    DELETE /hooks/{hookId}                              `delete_hook`, `recreate_hook`
                                                        (a secret's rotation)

The import of the campaigns already running in the team (lot C, decision
of 08/10/2026; `ImportReader`, GET only, each route sourced in
notes/lemlist-import-research.txt):

    GET    /campaigns?version=v2&limit=&offset=         `list_campaigns` (§6)
    GET    /campaigns/{id}                              `read_campaign`, its
                                                        `sequenceId` (§4)
    GET    /campaigns/{id}/sequences                    `read_sequences` (§5)
    GET    /campaigns/{id}/leads?limit=&offset=          `list_contacts` (§1.1)
    GET    /v2/campaigns/{id}/export/leads?state=all&format=json
                                                        `export_leads` (§1.2 a;
                                                        its real rows, §17)
    GET    /contacts?idsOrEmails=                       `lookup_contacts` (§1.2 d)
    GET    /activities?version=v2&campaignId=            `read_activities` (§3)

and the one write an imported campaign takes besides a pause:
`resume_campaign`, `POST /campaigns/{id}/start` alone (§9).

What Lemlist does not have, and what this adapter does instead:
- no archive route: archiving pauses the campaign;
- no step reorder route: before any lead entered, Nidria pushes each lead's
  content per position, so a reorder only moves delays (each positional step
  gets its new delay);
- once a lead entered the campaign, its sequence only grows at the end,
  as Prism measured on 2026-10-05: an insert at an index is refused (409
  `SEQUENCE_STEP_INSERT_ON_SEQUENCE_IN_USE`, "new steps can only be appended
  after the last one") and so is removing any step, even one no lead
  reached, whether the campaign runs or is paused (409
  `remove-step-on-sequence-in-use`). A step Nidria drops then is parked
  (`park_step`): a delay no lead outlives, reused by the next step added;
- skipping a step exists in Lemlist's app only, not in its API;
- delays are whole days only: WEEKS are converted, HOURS do not exist here;
- no per-campaign daily limit (sending limits belong to each mailbox): Nidria
  caps how many new clients it hands over per day instead.

Each step is created as a shell. Its content in Lemlist (`_SHELL_SUBJECT`,
`_SHELL_MESSAGE`) is only a placeholder that says so: what a lead receives is
the email Nidria rendered for that lead and that step, pushed as the lead's
personalization of the step. A lead is launched only once every step it can
still reach carries its own content, and a step added later reaches running
leads only after theirs was pushed, or they are paused (see `email_sync`), so
the placeholder is never what a lead gets.

What Lemlist does not report: an email being delivered (there is no
`emailsDelivered` for email), and a reply attributed to a third party
(`isThirdPartyReply` carries no campaign or lead, so it matches nothing).

Auth is HTTP Basic with an empty login and the API key, which the caller
passes to the constructor (the platform key, `lemlist_api_key`): this module
reads no setting. Rate limit: 20 requests per 2 seconds per key, throttled
here per process; a 429 is retried after its Retry-After, capped at 10 s. A
read (GET) is also retried after a timeout or a 5xx; a write never is on an
ambiguous failure, so nothing is created twice. The key is never logged nor
put in an error.
"""

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from src.email_campaigns.providers.base import (
    CampaignRef,
    CampaignSpec,
    LeadRef,
    LeadSpec,
    ProviderActivityRead,
    ProviderCampaign,
    ProviderCampaignListing,
    ProviderCampaignRow,
    ProviderContact,
    ProviderError,
    ProviderEvent,
    ProviderLead,
    ProviderLeadIdentity,
    ProviderLeadListing,
    ProviderMessage,
    ProviderSender,
    ProviderSequences,
    ProviderSequenceStep,
    ProviderStep,
    StepContent,
    StepSpec,
)

logger = logging.getLogger(__name__)


class _AddressFreeRequestLog(logging.Filter):
    """httpx logs every request's full URL at INFO (`HTTP Request: GET
    <url> ...`), which would put in the logs a query naming addresses
    (`idsOrEmails`, `email`): such a URL is logged without its query. Never
    an address in a log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) > 1 and isinstance(args[1], httpx.URL):
            url = args[1]
            if any("@" in key or "@" in value for key, value in url.params.multi_items()):
                record.args = (args[0], url.copy_with(query=None), *args[2:])
        return True


_HTTPX_LOG = logging.getLogger("httpx")
if not any(isinstance(known, _AddressFreeRequestLog) for known in _HTTPX_LOG.filters):
    _HTTPX_LOG.addFilter(_AddressFreeRequestLog())

#: Lemlist's public API, the one base URL of every Lemlist call Nidria makes
#: (`lemlist_team.team_of_key` reads it from here).
LEMLIST_API_BASE_URL = "https://api.lemlist.com/api"

_RATE_LIMIT = 20
_RATE_WINDOW = 2.0
_MAX_429_RETRIES = 3
#: The longest a 429's Retry-After is honoured, in seconds.
_MAX_RETRY_AFTER = 10.0
#: A read is tried again this many times after a timeout or a 5xx.
_READ_RETRIES = 2
_ACTIVITY_PAGE = 100
_MAX_ACTIVITY_PAGES = 100
#: `/campaigns/{id}/leads` paginates by offset (confirmed live by Prism's
#: legacy `LemlistService.list_campaign_leads`); past the cap the listing is
#: incomplete.
_LEAD_PAGE = 100
_MAX_LEAD_PAGES = 200
#: `/inbox/{contactId}` pages by `page` (from 1), newest first, 10 items at
#: most: measured live on 09/10/2026 (`limit` 20 or 100 is served as
#: `perPage` 10; `skip` is ignored, `page=2` serves the next items).
_INBOX_PAGE = 10
_MAX_INBOX_PAGES = 20
#: `GET /campaigns?version=v2`: `limit` 100 at most, paginated by `offset`.
_CAMPAIGN_PAGE = 100
_MAX_CAMPAIGN_PAGES = 50
#: An import reads a campaign's whole history, beyond a sync's window.
_MAX_IMPORT_ACTIVITY_PAGES = 500
#: `GET /contacts?idsOrEmails=` takes 100 values at most.
_CONTACTS_PER_CALL = 100
#: The lead fields kept verbatim on a membership.
_LEAD_KEYS = ("state", "isPaused", "isUnsubscribed", "contactId", "status")
#: The campaign fields kept verbatim on a campaign.
_CAMPAIGN_KEYS = ("status", "hasError", "errors", "timezone", "createdAt", "archived", "name")
#: Lemlist activity types, mapped to Nidria event types. Others are not email events.
#: Unsubscribes (developer.lemlist.com, webhooks/add-webhook, read 08/10/2026):
#: "Since March 30, 2026, a contact unsubscribing emits `entityUnsubscribed`
#: (whole contact) or `variableUnsubscribed` (single variable) instead of
#: `emailsUnsubscribed`", and `emailsUnsubscribed` "also matches
#: `apiUnsubscribed` and `manualUnsubscribed` activities". A
#: `variableUnsubscribed` is "a specific contact variable (email address,
#: phone number, LinkedIn profile)" whose payload names no documented field
#: saying which: it is VARIABLE_UNSUBSCRIBED, stored and counted, never
#: applied (nothing is guessed).
ACTIVITY_TYPES: dict[str, str] = {
    "emailsSent": "SENT",
    "emailsOpened": "OPENED",
    "emailsClicked": "CLICKED",
    "emailsReplied": "REPLIED",
    "emailsBounced": "BOUNCED",
    "emailsFailed": "FAILED",
    "emailsUnsubscribed": "UNSUBSCRIBED",
    "apiUnsubscribed": "UNSUBSCRIBED",
    "manualUnsubscribed": "UNSUBSCRIBED",
    "entityUnsubscribed": "UNSUBSCRIBED",
    "variableUnsubscribed": "VARIABLE_UNSUBSCRIBED",
    "paused": "PAUSED",
    "resumed": "RESUMED",
}
#: Activity types about a whole CONTACT rather than one lead of a campaign:
#: they may come without `campaignId` or `leadId` (Lemlist documents
#: `campaignId` "absent for non-campaign events" and `leadId` for "campaign
#: events only"), and an unsubscribe of the contact reaches every one of its
#: leads.
CONTACT_ACTIVITY_TYPES = frozenset({"entityUnsubscribed", "variableUnsubscribed"})
#: A lead state that says it unsubscribed (`state` is the lead's last
#: activity type; seen live on 08/10/2026: `entityUnsubscribed`).
_UNSUBSCRIBED_STATES = frozenset(
    {
        "unsubscribed",
        "emailsunsubscribed",
        "entityunsubscribed",
        "apiunsubscribed",
        "manualunsubscribed",
    }
)
#: A parked step waits this long (Lemlist's maximum): no lead ever reaches it.
PARKED_DELAY_DAYS = 1500


class TeamUnknownError(ProviderError):
    """Lemlist did not say which team the key opens (decision A4).

    The one way "unknown" is told, wherever the team is asked (the
    adapter's `team_id`, `lemlist_team.team_of_key`): never None, which
    means "no team" (nothing to ask: the dry run), and never another
    team's id. A bound agency is never switched to dry run because Lemlist
    did not answer. `reason` is a short stable word for logs and tests
    (`network`, `http_<status>`, `payload`, `not_configured`)."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            f"Lemlist team unknown ({reason})", code="TEAM_UNKNOWN", params={"reason": reason}
        )
        self.reason = reason


#: The step's own content in Lemlist: a placeholder, never what a lead gets.
#: Each lead's subject and message are its personalization of the step,
#: pushed by Nidria before the lead can reach it. English, no em dash
#: (spec 9.3): only Lemlist's app shows it.
_SHELL_MESSAGE = (
    "<p>Content managed by Nidria.</p>"
    "<p>Personalized subject and message are provided individually for each lead.</p>"
)
#: Lemlist refuses to launch leads while a campaign's first email has no
#: subject. No lead receives this one either: each gets its own.
_SHELL_SUBJECT = "Managed by Nidria"


class _Throttle:
    def __init__(self) -> None:
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            while self._calls and now - self._calls[0] > _RATE_WINDOW:
                self._calls.popleft()
            if len(self._calls) >= _RATE_LIMIT:
                time.sleep(_RATE_WINDOW - (now - self._calls[0]) + 0.05)
            self._calls.append(time.monotonic())


_THROTTLE = _Throttle()


def _parse_time(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


def _retry_after(response: httpx.Response) -> float:
    """A 429's wait: its Retry-After in seconds, 2 s when absent or not a
    number (an HTTP date), never more than `_MAX_RETRY_AFTER`."""
    try:
        wait = float(response.headers.get("Retry-After") or 2)
    except ValueError:
        wait = 2.0
    return min(max(wait, 0.0), _MAX_RETRY_AFTER)


def normalize_activity(payload: dict[str, Any]) -> ProviderEvent | None:
    """One Lemlist activity (from GET /activities or a webhook body) as a
    ProviderEvent; None when it is not an email event Nidria tracks. The
    webhook's `secret` is never kept."""
    provider_type = str(payload.get("type") or "")
    normalized = ACTIVITY_TYPES.get(provider_type)
    event_id = payload.get("_id")
    if normalized is None or not event_id:
        return None
    step = payload.get("sequenceStep")
    reason = payload.get("errorMessage") or payload.get("bounceReason") or payload.get("reason")
    to = payload.get("to")
    recipient = (
        to[0].get("address") if isinstance(to, list) and to and isinstance(to[0], dict) else None
    )
    clean = {k: v for k, v in payload.items() if k != "secret"}
    return ProviderEvent(
        event_id=str(event_id),
        type=normalized,
        provider_type=provider_type,
        occurred_at=_parse_time(payload.get("createdAt")),
        campaign_id=payload.get("campaignId"),
        lead_id=payload.get("leadId"),
        email=payload.get("leadEmail") or recipient or payload.get("email"),
        step_position=(int(step) + 1) if isinstance(step, int) else None,
        reason=str(reason) if reason else None,
        payload=clean,
    )


#: The campaign's tracking, as Lemlist's `tracking` object (PATCH /campaigns).
_TRACKING = {"trackOpens": True, "trackClicks": True, "trackReplies": True}


@dataclass(frozen=True)
class LemlistTeam:
    """The team a key opens (`GET /team`)."""

    team_id: str
    name: str | None
    user_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class LemlistHook:
    """A webhook of the team (`GET /hooks`). Lemlist keeps one per target URL;
    its secret is never returned."""

    hook_id: str
    target_url: str | None
    #: The one event type the hook is limited to; None: every event.
    type: str | None


class LemlistEmailProvider:
    name = "LEMLIST"
    external = True

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = LEMLIST_API_BASE_URL,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
        team_lookup: Callable[[], str] | None = None,
    ) -> None:
        """`api_key`: the platform's Lemlist key (setting `lemlist_api_key`),
        passed by whoever chooses the provider. `transport`: tests pass an
        httpx.MockTransport; production uses the default one. `team_lookup`:
        how `team_id` knows the key's team: it returns the team's id or
        raises `TeamUnknownError`, never None (the registry wires
        `lemlist_team.key_team_id`, the 10-minute cache of `GET /team`).
        Without it the adapter reads `GET /team` itself, once per instance."""
        self._auth = httpx.BasicAuth("", api_key)
        self._base = base_url.rstrip("/")
        self._timeout = httpx.Timeout(timeout, connect=10.0)
        self._transport = transport
        self._team_lookup = team_lookup
        self._team: str | None = None

    # ── transport ────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        ok_codes: tuple[int, ...] = (),
    ) -> httpx.Response:
        """One call. 429 is retried after Retry-After (the request was not
        processed); a read is retried after a timeout or a 5xx; anything else
        is surfaced as a ProviderError."""
        url = f"{self._base}{path}"
        reads_left = _READ_RETRIES if method == "GET" else 0
        rate_limits_left = _MAX_429_RETRIES
        while True:
            _THROTTLE.wait()
            try:
                with httpx.Client(
                    timeout=self._timeout, auth=self._auth, transport=self._transport
                ) as client:
                    response = client.request(method, url, params=params, json=json)
            except httpx.TimeoutException as exc:
                if reads_left:
                    reads_left -= 1
                    time.sleep(1.0)
                    continue
                raise ProviderError("Lemlist did not answer in time.", code="TIMEOUT") from exc
            except httpx.HTTPError as exc:
                if reads_left:
                    reads_left -= 1
                    time.sleep(1.0)
                    continue
                raise ProviderError("Lemlist could not be reached.", code="UNREACHABLE") from exc
            if response.status_code == 429:
                if not rate_limits_left:
                    raise ProviderError(
                        "Lemlist rate limit persisted.", code="RATE_LIMITED", status=429
                    )
                rate_limits_left -= 1
                wait = _retry_after(response)
                logger.info("Lemlist rate limit, retrying in %.1fs", wait)
                time.sleep(wait)
                continue
            if response.status_code >= 500 and reads_left:
                reads_left -= 1
                time.sleep(1.0)
                continue
            if response.is_success or response.status_code in ok_codes:
                return response
            raise _error(response)

    # ── campaigns ────────────────────────────────────────────────────────

    def create_campaign(self, spec: CampaignSpec) -> CampaignRef:
        body: dict[str, Any] = {"name": spec.name, "autoReview": False}
        if spec.schedule is not None:
            body["timezone"] = spec.schedule.timezone
        data = _json(self._request("POST", "/campaigns", json=body))
        if not data.get("_id"):
            raise ProviderError("Lemlist created no campaign id.", code="BAD_RESPONSE")
        schedules = data.get("scheduleIds")
        ref = CampaignRef(
            campaign_id=str(data["_id"]),
            sequence_id=data.get("sequenceId"),
            schedule_id=str(schedules[0]) if isinstance(schedules, list) and schedules else None,
        )
        self.update_campaign(ref, spec)
        return ref

    def update_campaign(self, ref: CampaignRef, spec: CampaignSpec) -> None:
        # Opens, clicks and replies are tracked whatever the account's default:
        # every web address of a body is a link (`body_to_html`) that Lemlist
        # must rewrite to count it. Sent both as the documented `tracking`
        # object and as the legacy flat flag.
        body: dict[str, Any] = {
            "name": spec.name,
            "stopOnEmailReplied": spec.stop_on_reply,
            "disableTrackClick": False,
            "tracking": dict(_TRACKING),
        }
        if spec.sender_ids:
            sending = {
                str(s.get("id"))
                for s in _json(self._request("GET", f"/campaigns/{ref.campaign_id}")).get("senders")
                or []
                if isinstance(s, dict)
            }
            if sending != set(spec.sender_ids):
                # The sender chosen in Nidria; otherwise starting assigns one.
                # Sent only when it changes, so a mailbox picked in Lemlist for
                # that user stays (decision A5: Prism's final behaviour).
                body["sendUserIds"] = list(spec.sender_ids)
        self._request("PATCH", f"/campaigns/{ref.campaign_id}", json=body)
        # Only the schedule Lemlist created with this campaign is ever changed,
        # never a team schedule other campaigns could share.
        if ref.schedule_id and spec.schedule is not None:
            self._request(
                "PATCH",
                f"/schedules/{ref.schedule_id}",
                json={
                    "timezone": spec.schedule.timezone,
                    "start": spec.schedule.start,
                    "end": spec.schedule.end,
                    "weekdays": list(spec.schedule.weekdays),
                },
            )

    def start_campaign(self, ref: CampaignRef, *, mailbox: str | None = None) -> None:
        """Start sending. The campaign gets a sender first (Lemlist creates it
        with none, and then refuses to launch its leads); starting one that
        already runs does nothing. With a chosen mailbox, nothing starts
        until Lemlist sends from it (`_ensure_sender`)."""
        self._ensure_sender(ref, mailbox)
        response = self._request("POST", f"/campaigns/{ref.campaign_id}/start", ok_codes=(400,))
        if response.status_code == 400 and not _says(response, "already running"):
            raise _error(response)

    def resume_campaign(self, ref: CampaignRef) -> None:
        """Start again a campaign as it stands at Lemlist (`POST
        /campaigns/{id}/start`): an imported one, whose senders are Lemlist's
        own. Unlike `start_campaign`, no sender is assigned and no mailbox
        checked: the start is the only write. One that already runs answers
        400 "already running" (measured by Prism): nothing to do."""
        response = self._request("POST", f"/campaigns/{ref.campaign_id}/start", ok_codes=(400,))
        if response.status_code == 400 and not _says(response, "already running"):
            raise _error(response)

    def pause_campaign(self, ref: CampaignRef) -> None:
        """Stop sending; a campaign that does not run (Lemlist: "You can't
        pause campaigns that are not running.", seen by Prism on 2026-10-05 on
        a synced draft) is already where pausing would put it."""
        response = self._request("POST", f"/campaigns/{ref.campaign_id}/pause", ok_codes=(400,))
        if response.status_code == 400 and not _says(response, "already paused", "not running"):
            raise _error(response)

    def team_id(self) -> str:
        """The Lemlist team this key opens: stamped on every campaign Nidria
        creates (`provider_metadata.team_id`, rule N4), so a campaign of
        another team is told apart. Raises `TeamUnknownError` when Lemlist
        does not say (no answer, an error, no team named): unknown is never
        "no team" nor "another team" (decision A4). A failure is not kept:
        the next call asks again."""
        if self._team_lookup is not None:
            return self._team_lookup()
        if self._team is None:
            try:
                team = self.read_team()
            except ProviderError as exc:
                logger.warning("Lemlist team unknown (%s)", exc.code)
                raise TeamUnknownError(f"http_{exc.status}" if exc.status else "network") from None
            if team is None:
                raise TeamUnknownError("payload")
            self._team = team.team_id
        return self._team

    def list_senders(self) -> list[ProviderSender]:
        """The mailboxes a campaign can be sent from, each with its user.
        Lemlist's senders route lists only users who already send a campaign
        (nobody, in a new team). A team of one user sends from that user's
        mailboxes: the API key's own (`GET /user/channels`). With several
        users, each one's address is read from a campaign it sends."""
        rows = _json_list(self._request("GET", "/team/senders"))
        campaigns_of = {
            str(row["userId"]): [
                str(c["_id"])
                for c in row.get("campaigns") or []
                if isinstance(c, dict) and c.get("_id")
            ]
            for row in rows
            if row.get("userId")
        }
        users = self._team_users()
        if len(users) == 1:
            mailboxes = self._own_mailboxes()
            if mailboxes:
                owner = users[0]
                count = len(campaigns_of.get(owner, []))
                return sorted(
                    (ProviderSender(owner, email, count) for email in mailboxes),
                    key=lambda s: (s.email or "", s.user_id),
                )
        senders: list[ProviderSender] = []
        for user_id, campaigns in campaigns_of.items():
            email = None
            for campaign_id in campaigns[:3]:
                detail = _json(self._request("GET", f"/campaigns/{campaign_id}", ok_codes=(404,)))
                email = next(
                    (
                        str(s["email"])
                        for s in detail.get("senders") or []
                        if isinstance(s, dict) and s.get("id") == user_id and s.get("email")
                    ),
                    None,
                )
                if email:
                    break
            senders.append(ProviderSender(user_id, email, len(campaigns)))
        return sorted(senders, key=lambda s: (s.email is None, s.email or "", s.user_id))

    def _team_users(self) -> list[str]:
        """The team's users (`GET /team`)."""
        team = _json(self._request("GET", "/team"))
        return sorted({str(u) for u in team.get("userIds") or [] if u})

    def _own_mailboxes(self) -> list[str]:
        """The mailboxes connected to the API key's user (`GET /user/channels`)."""
        channels = _json(self._request("GET", "/user/channels")).get("email")
        accounts = channels.get("accounts") if isinstance(channels, dict) else None
        return [str(a["email"]) for a in accounts or [] if isinstance(a, dict) and a.get("email")]

    def _ensure_sender(self, ref: CampaignRef, mailbox: str | None = None) -> None:
        """The team's only sender, when the campaign has none. With several,
        which one sends is a person's choice: the campaign's "Sent from" in
        Nidria, sent with the campaign before it starts.

        Lemlist's API assigns a user, which then sends from its default
        mailbox; only Lemlist's app picks another one of its mailboxes. So a
        mailbox chosen in Nidria is checked here, and the campaign does not
        start until Lemlist sends from it (`SENDER_MAILBOX`, decision A5)."""
        campaign = _json(self._request("GET", f"/campaigns/{ref.campaign_id}"))
        if not (campaign.get("senders") or campaign.get("sendUsers")):
            senders = _json_list(self._request("GET", "/team/senders"))
            ids = sorted({str(row["userId"]) for row in senders if row.get("userId")})
            if not ids:
                # A new team has no campaign sender yet: its only user is one.
                users = self._team_users()
                ids = users if len(users) == 1 else []
            if len(ids) != 1:
                raise ProviderError(
                    "This Lemlist campaign has no sender: "
                    + (
                        "connect a mailbox in Lemlist"
                        if not ids
                        else f"the team has {len(ids)}, choose one in the campaign's Sent from"
                    )
                    + ", then sync again.",
                    code="SENDER_REQUIRED",
                    params={"senders": len(ids)},
                )
            self._request("PATCH", f"/campaigns/{ref.campaign_id}", json={"sendUserIds": ids})
            if mailbox:
                campaign = _json(self._request("GET", f"/campaigns/{ref.campaign_id}"))
        if mailbox:
            used = _sending_mailboxes(campaign)
            if mailbox.strip().lower() not in used:
                raise ProviderError(
                    f"Lemlist sends this campaign from {', '.join(sorted(used)) or 'no mailbox'}, "
                    f"not {mailbox}: its API assigns the sender's account, not the mailbox. "
                    f"In Lemlist, open the campaign › Settings › Senders, choose {mailbox}, "
                    "then sync again.",
                    code="SENDER_MAILBOX",
                    params={"mailbox": mailbox, "sending_from": sorted(used)},
                )

    def archive_campaign(self, ref: CampaignRef) -> None:
        # No archive route: a paused campaign sends nothing more.
        self.pause_campaign(ref)

    def read_campaign(self, ref: CampaignRef) -> ProviderCampaign:
        """The campaign as Lemlist sees it. Lemlist documents a 404 for an
        unknown campaign and was seen answering `200 {}` (Prism's legacy
        finding LM-1): both mean it no longer exists there."""
        response = self._request("GET", f"/campaigns/{ref.campaign_id}", ok_codes=(404,))
        data = _json(response) if response.status_code != 404 else {}
        if data.get("_id") != ref.campaign_id:
            return ProviderCampaign(ref.campaign_id, exists=False)
        return ProviderCampaign(
            ref.campaign_id,
            exists=True,
            status=str(data["status"]) if data.get("status") else None,
            metadata={k: data[k] for k in _CAMPAIGN_KEYS if k in data},
            sequence_id=_text(data.get("sequenceId")),
        )

    def list_campaigns(self) -> ProviderCampaignListing:
        """The team's campaigns (`GET /campaigns?version=v2`, documented as an
        array of campaigns, paginated by `offset` with a `limit` of 100 at
        most). Its v2 shape was never observed: an object carrying them under
        `campaigns` is read too. A campaign listed twice (a page shifting
        while it is read) is kept once."""
        found: dict[str, ProviderCampaignRow] = {}
        for page in range(_MAX_CAMPAIGN_PAGES):
            response = self._request(
                "GET",
                "/campaigns",
                params={"version": "v2", "limit": _CAMPAIGN_PAGE, "offset": page * _CAMPAIGN_PAGE},
            )
            rows = _rows(response, "campaigns", what="campaigns listing")
            for row in rows:
                campaign_id = _text(row.get("_id"))
                if campaign_id and campaign_id not in found:
                    found[campaign_id] = ProviderCampaignRow(
                        campaign_id=campaign_id,
                        name=_text(row.get("name")),
                        status=_text(row.get("status")),
                        created_at=_text(row.get("createdAt")),
                    )
            if len(rows) < _CAMPAIGN_PAGE:
                return ProviderCampaignListing(list(found.values()), complete=True, pages=page + 1)
        logger.warning("Lemlist campaigns past the page cap: listing incomplete")
        return ProviderCampaignListing(
            list(found.values()), complete=False, pages=_MAX_CAMPAIGN_PAGES
        )

    # ── steps ────────────────────────────────────────────────────────────

    def _sequence(self, ref: CampaignRef) -> str:
        if not ref.sequence_id:
            raise ProviderError("The campaign has no Lemlist sequence id.", code="NO_SEQUENCE")
        return ref.sequence_id

    def create_step(self, ref: CampaignRef, step: StepSpec) -> str:
        # Appended at the end: Lemlist moves leads parked on the last step on
        # to it, which is why the sync pauses the campaign around it.
        data = self._request(
            "POST",
            f"/sequences/{self._sequence(ref)}/steps",
            json={
                "type": "email",
                "delay": step.delay_days,
                "message": _SHELL_MESSAGE,
                **({"subject": _SHELL_SUBJECT} if step.position == 1 else {}),
            },
        )
        step_id = _json(data).get("_id")
        if not step_id:
            raise ProviderError("Lemlist created no step id.", code="BAD_RESPONSE")
        return str(step_id)

    def update_step(self, ref: CampaignRef, step_id: str, step: StepSpec) -> None:
        self._request(
            "PATCH",
            f"/sequences/{self._sequence(ref)}/steps/{step_id}",
            # Only the first email has a subject; the others answer in its thread.
            # The placeholder is sent again, so older shells read as it does now.
            json={
                "type": "email",
                "delay": step.delay_days,
                "subject": _SHELL_SUBJECT if step.position == 1 else "",
                "message": _SHELL_MESSAGE,
            },
        )

    def delete_step(self, ref: CampaignRef, step_id: str) -> None:
        self._request(
            "DELETE", f"/sequences/{self._sequence(ref)}/steps/{step_id}", ok_codes=(404,)
        )

    def read_steps(self, ref: CampaignRef) -> list[ProviderStep]:
        """The campaign's main sequence as Lemlist has it, in order."""
        sequence = self._sequence(ref)
        data = _json(self._request("GET", f"/campaigns/{ref.campaign_id}/sequences"))
        main = data.get(sequence)
        rows = main.get("steps") if isinstance(main, dict) else None
        steps = [row for row in rows or [] if isinstance(row, dict) and row.get("_id")]
        steps.sort(key=lambda row: int(row.get("sequenceStep") or 0))
        return [
            ProviderStep(str(row["_id"]), index + 1, int(row.get("delay") or 0))
            for index, row in enumerate(steps)
        ]

    def read_sequences(self, ref: CampaignRef) -> ProviderSequences:
        """The campaign's sequences as Lemlist has them (`GET
        /campaigns/{id}/sequences`: an object keyed by sequence id). The main
        one is the campaign's `sequenceId`, else the one at `level` 0; its
        steps come in their order (`sequenceStep`, then `index`), each kept as
        Lemlist returned it. The conditional sequences (`level` 1 and more)
        are counted, never read further. Under an A/B test (`abTest` true,
        or "A" / "B" once a winner is chosen), `subject` and `message` are
        always variant A's."""
        data = _json(self._request("GET", f"/campaigns/{ref.campaign_id}/sequences"))
        sequences = {str(key): value for key, value in data.items() if isinstance(value, dict)}
        main_id = (
            ref.sequence_id
            if ref.sequence_id in sequences
            else next((key for key, value in sequences.items() if value.get("level") == 0), None)
        )
        main = sequences.get(main_id) if main_id is not None else None
        rows = [
            row
            for row in (main or {}).get("steps") or []
            if isinstance(row, dict) and _text(row.get("_id"))
        ]
        rows.sort(key=lambda row: (_int(row.get("sequenceStep")), _int(row.get("index"))))
        branches = [value for key, value in sequences.items() if key != main_id]
        return ProviderSequences(
            main_sequence_id=main_id,
            steps=[
                ProviderSequenceStep(
                    step_id=str(row["_id"]),
                    position=index + 1,
                    type=_text(row.get("type")),
                    delay_days=_int(row.get("delay")),
                    ab_test=row.get("abTest") is True or row.get("abTest") in ("A", "B"),
                    raw=dict(row),
                )
                for index, row in enumerate(rows)
            ],
            branches=len(branches),
            branch_steps=sum(
                sum(1 for step in branch.get("steps") or [] if isinstance(step, dict))
                for branch in branches
            ),
        )

    def park_step(self, ref: CampaignRef, step_id: str) -> None:
        """Lemlist keeps every step once a lead entered: this one waits longer
        than any lead will, says it is a placeholder, and stays in place for
        the next step Nidria adds."""
        self._request(
            "PATCH",
            f"/sequences/{self._sequence(ref)}/steps/{step_id}",
            json={
                "type": "email",
                "delay": PARKED_DELAY_DAYS,
                "subject": "",
                "message": _SHELL_MESSAGE,
            },
        )

    def reorder_steps(self, ref: CampaignRef, steps: list[tuple[str, StepSpec]]) -> None:
        # Content is per lead and per position: only the delays move.
        for step_id, spec in steps:
            self.update_step(ref, step_id, spec)

    # ── leads ────────────────────────────────────────────────────────────

    def add_contact(self, ref: CampaignRef, lead: LeadSpec) -> LeadRef:
        """Create the lead. `deduplicate` refuses a person already in another
        campaign; `fillEmptyOnly` never overwrites what the workspace already
        knows about the contact or the company. A lead this campaign already
        has (a previous push whose answer was lost) is adopted, not duplicated.

        Every field the spec carries is sent, empty ones left out. Nidria's
        leads carry email, first and last name only (decision A8, see
        `LeadSpec`)."""
        body = {
            "email": lead.email,
            "firstName": lead.first_name,
            "lastName": lead.last_name,
            "companyName": lead.company_name,
            "companyDomain": lead.company_domain,
            "jobTitle": lead.job_title,
            "linkedinUrl": lead.linkedin_url,
        }
        try:
            response = self._request(
                "POST",
                f"/campaigns/{ref.campaign_id}/leads/",
                params={"deduplicate": "true", "updateStrategy": "fillEmptyOnly"},
                json={k: v for k, v in body.items() if v},
            )
        except ProviderError as exc:
            if exc.code != "ALREADY_IN_CAMPAIGN":
                raise
            existing = self._lead_by_email(lead.email)
            if existing is None or existing.get("campaignId") != ref.campaign_id:
                raise
            return LeadRef(lead_id=str(existing["_id"]), contact_id=existing.get("contactId"))
        data = _json(response)
        if not data.get("_id"):
            raise ProviderError("Lemlist created no lead id.", code="BAD_RESPONSE")
        return LeadRef(lead_id=str(data["_id"]), contact_id=data.get("contactId"))

    def _lead_by_email(self, email: str) -> dict[str, Any] | None:
        response = self._request(
            "GET", "/leads", params={"email": email, "version": "v2"}, ok_codes=(404,)
        )
        data = _json(response) if response.status_code != 404 else {}
        return data if data.get("_id") else None

    def set_step_content(
        self, ref: CampaignRef, lead: LeadRef, step_id: str, content: StepContent
    ) -> bool:
        """Push this lead's exact email for one step. False when Lemlist already
        reached the step: what was sent stays as sent."""
        # The subject is always sent, empty included: on a follow-up an empty
        # subject keeps the email in the thread, and sending it explicitly
        # clears any subject an earlier push gave this lead.
        response = self._request(
            "PUT",
            f"/campaigns/{ref.campaign_id}/leads/{lead.lead_id}/steps/{step_id}/personalization",
            json={"subject": content.subject, "message": content.html},
            ok_codes=(409,),
        )
        if response.status_code == 409:
            if _code(_json(response)) == "REVIEW_DRAFT_STEP_ALREADY_REACHED":
                return False
            raise _error(response)
        return True

    def launch_contact(self, lead: LeadRef) -> None:
        response = self._request("POST", f"/leads/review/{lead.lead_id}", ok_codes=(400,))
        if (
            response.status_code == 400
            and _code(_json(response)) != "CAMPAIGN_LEAD_REVIEW_LEAD_ALREADY_LAUNCHED"
        ):
            raise _error(response)

    def pause_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._request(
            "POST", f"/leads/pause/{lead.lead_id}", params={"campaignId": ref.campaign_id}
        )

    def resume_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._request(
            "POST", f"/leads/start/{lead.lead_id}", params={"campaignId": ref.campaign_id}
        )

    def remove_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._request(
            "DELETE",
            f"/campaigns/{ref.campaign_id}/leads/{lead.lead_id}",
            params={"action": "remove"},
            ok_codes=(404,),
        )

    def get_contact(self, lead: LeadRef) -> ProviderLead | None:
        response = self._request(
            "GET", "/leads", params={"id": lead.lead_id, "version": "v2"}, ok_codes=(404,)
        )
        if response.status_code == 404:
            return None
        return _lead(_json(response), fallback_id=lead.lead_id)

    def list_contacts(self, ref: CampaignRef) -> ProviderLeadListing:
        leads: list[ProviderLead] = []
        for page in range(_MAX_LEAD_PAGES):
            response = self._request(
                "GET",
                f"/campaigns/{ref.campaign_id}/leads",
                params={"limit": _LEAD_PAGE, "offset": page * _LEAD_PAGE},
            )
            try:
                data = response.json() if (response.text or "").strip() else []
            except ValueError as exc:
                raise ProviderError(
                    "Lemlist answered the leads listing with no JSON.", code="BAD_RESPONSE"
                ) from exc
            if isinstance(data, dict):
                data = data.get("leads") or data.get("data") or []
            rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
            leads.extend(lead for lead in (_lead(row) for row in rows) if lead is not None)
            if len(rows) < _LEAD_PAGE:
                return ProviderLeadListing(leads=leads, complete=True)
        logger.warning("Lemlist leads of %s past the page cap: listing incomplete", ref.campaign_id)
        return ProviderLeadListing(leads=leads, complete=False)

    def export_leads(self, ref: CampaignRef) -> list[ProviderLeadIdentity]:
        """Who the campaign's leads are: its export, `GET
        /v2/campaigns/{id}/export/leads?state=all&format=json` under the API
        base (https://api.lemlist.com/api/v2/...), one answer: neither
        pagination nor a size limit is documented, so the caller checks that
        every lead it listed came back. Per lead id: its email, names,
        company, `status` and `lastState` (both declared, neither required).
        Observed on 08/10/2026 on the real team, undocumented (notes §17): an
        array in one answer whose rows carry `email`, `_id`, `state`,
        `stateSystem`, `companyName` and the campaign's own variables, the
        names only when the campaign has them, and never `status`,
        `lastState`, `contactId` nor `isPaused`; so every word is optional,
        `state` is read before `lastState`, and `stateSystem` is kept as
        said. An object carrying the rows under `leads` or `data` is read too
        (not documented). A lead id given twice is kept once."""
        response = self._request(
            "GET",
            f"/v2/campaigns/{ref.campaign_id}/export/leads",
            params={"state": "all", "format": "json"},
        )
        found: dict[str, ProviderLeadIdentity] = {}
        for row in _rows(response, "leads", "data", what="leads export"):
            lead_id = _text(row.get("_id"))
            if lead_id and lead_id not in found:
                found[lead_id] = ProviderLeadIdentity(
                    lead_id=lead_id,
                    email=_text(row.get("email")),
                    first_name=_text(row.get("firstName")),
                    last_name=_text(row.get("lastName")),
                    company_name=_text(row.get("companyName")),
                    status=_text(row.get("status")),
                    last_state=_text(row.get("state")) or _text(row.get("lastState")),
                    state_system=_text(row.get("stateSystem")),
                )
        return list(found.values())

    def lookup_contacts(self, ids_or_emails: Sequence[str]) -> list[ProviderContact]:
        """Contacts by id or by address (`GET /contacts?idsOrEmails=`, 100
        values at most per call, comma separated): the address, the names
        (`fields.firstName`, `fields.lastName`) and, per campaign and lead,
        the lead's status there (`campaigns[].leadState`, a status value).
        The company is not part of this answer. A query naming addresses is
        logged without them (`_AddressFreeRequestLog`)."""
        wanted = list(dict.fromkeys(c for c in ids_or_emails if c))
        found: dict[str, ProviderContact] = {}
        for start in range(0, len(wanted), _CONTACTS_PER_CALL):
            response = self._request(
                "GET",
                "/contacts",
                params={"idsOrEmails": ",".join(wanted[start : start + _CONTACTS_PER_CALL])},
            )
            for row in _rows(response, "contacts", "data", what="contacts lookup"):
                contact_id = _text(row.get("_id"))
                if not contact_id or contact_id in found:
                    continue
                found_fields = row.get("fields")
                fields: dict[str, Any] = found_fields if isinstance(found_fields, dict) else {}
                statuses: dict[tuple[str, str], str] = {}
                for entry in row.get("campaigns") or []:
                    if not isinstance(entry, dict):
                        continue
                    campaign_id = _text(entry.get("campaignId"))
                    lead_id = _text(entry.get("leadId"))
                    state = _text(entry.get("leadState"))
                    if campaign_id and lead_id and state:
                        statuses[(campaign_id, lead_id)] = state
                found[contact_id] = ProviderContact(
                    contact_id=contact_id,
                    email=_text(row.get("email")),
                    first_name=_text(fields.get("firstName")),
                    last_name=_text(fields.get("lastName")),
                    lead_statuses=statuses,
                )
        return list(found.values())

    # ── events ───────────────────────────────────────────────────────────

    def _activities(self, params: dict[str, Any]) -> list[ProviderEvent]:
        return self._read_activities(params, _MAX_ACTIVITY_PAGES).events

    def _read_activities(self, params: dict[str, Any], max_pages: int) -> ProviderActivityRead:
        """Activities by pages of 100 (`offset` grows by the page size) until
        a short page, `max_pages` at most: the tracked email events, each
        once, and what was read."""
        events: dict[str, ProviderEvent] = {}
        read = 0
        for page in range(max_pages):
            batch = self._request(
                "GET",
                "/activities",
                params={
                    **params,
                    "version": "v2",
                    "limit": _ACTIVITY_PAGE,
                    "offset": page * _ACTIVITY_PAGE,
                },
            )
            rows = _json_list(batch)
            read += len(rows)
            for event in (normalize_activity(r) for r in rows):
                if event is not None:
                    events.setdefault(event.event_id, event)
            if len(rows) < _ACTIVITY_PAGE:
                return ProviderActivityRead(list(events.values()), read, page + 1, complete=True)
        return ProviderActivityRead(list(events.values()), read, max_pages, complete=False)

    def read_activities(self, ref: CampaignRef) -> ProviderActivityRead:
        """The campaign's whole history, for an import: every activity of
        the campaign (`GET /activities?version=v2&campaignId=`, no date), up
        to `_MAX_IMPORT_ACTIVITY_PAGES` pages; `complete` False past it."""
        return self._read_activities({"campaignId": ref.campaign_id}, _MAX_IMPORT_ACTIVITY_PAGES)

    def sync_campaign(self, ref: CampaignRef, since: datetime | None) -> list[ProviderEvent]:
        params: dict[str, Any] = {"campaignId": ref.campaign_id}
        if since is not None:
            params["minDate"] = since.astimezone(UTC).isoformat()
        return self._activities(params)

    def sync_contact(self, ref: CampaignRef, lead: LeadRef) -> list[ProviderEvent]:
        return self._activities({"campaignId": ref.campaign_id, "leadId": lead.lead_id})

    def get_messages(self, lead: LeadRef) -> list[ProviderMessage]:
        """The contact's inbox thread: sent emails and replies, each with its
        HTML body (`message`), page by page while Lemlist names a next one.
        Keyed by the CONTACT id; empty without one."""
        if not lead.contact_id:
            return []
        messages: list[ProviderMessage] = []
        for page in range(1, _MAX_INBOX_PAGES + 1):
            response = self._request(
                "GET",
                f"/inbox/{lead.contact_id}",
                params={"limit": _INBOX_PAGE, "page": page},
            )
            body = _json(response)
            data: Any = body.get("data") if body else _json_list(response)
            rows = [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []
            messages.extend(m for m in (_message(row) for row in rows) if m is not None)
            pagination = body.get("pagination")
            more = isinstance(pagination, dict) and bool(pagination.get("nextPage"))
            if not rows or not more:
                break
        return messages

    # ── account (platform routes, outside the EmailProvider protocol) ────

    def read_team(self) -> LemlistTeam | None:
        """The team the key opens (`GET /team`): its id, name and users; None
        when Lemlist names no team. A refusal or a failure is raised: the
        platform's account route says it rather than guessing."""
        data = _json(self._request("GET", "/team"))
        team_id = data.get("_id")
        if not isinstance(team_id, str) or not team_id:
            return None
        name = data.get("name")
        return LemlistTeam(
            team_id=team_id,
            name=str(name) if name else None,
            user_ids=tuple(sorted({str(u) for u in data.get("userIds") or [] if u})),
        )

    def read_plan(self) -> str | None:
        """The account's plan as Lemlist names it, read from `GET /user/channels`
        (`"plan"`): neither `/team` nor `/team/credits` says it (Prism,
        measured 2026-10-06). The API does not tell Starter from Pro: check it
        in Lemlist's app before relying on API launches. None when the answer
        carries no plan name."""
        plan = _json(self._request("GET", "/user/channels")).get("plan")
        if not isinstance(plan, str):
            return None
        return plan.strip() or None

    def list_hooks(self) -> list[LemlistHook]:
        """The team's webhooks (`GET /hooks`). Their secret never comes back."""
        hooks: list[LemlistHook] = []
        for row in _json_list(self._request("GET", "/hooks")):
            hook = _hook(row)
            if hook is not None:
                hooks.append(hook)
        return hooks

    def create_hook(self, target_url: str, secret: str) -> LemlistHook:
        """Create the team's webhook for every event: no `type`, since Lemlist
        keeps one hook per target URL and a hook has one type or none. Its
        `secret` comes back in each event's body and can never be changed
        (rotating it means deleting and recreating the hook). The secret is
        never logged, nor kept in an error."""
        try:
            response = self._request(
                "POST", "/hooks", json={"targetUrl": target_url, "secret": secret}
            )
        except ProviderError as exc:
            message = str(exc).replace(secret, "***") if secret else str(exc)
            raise ProviderError(
                message, code=exc.code, status=exc.status, params=exc.params
            ) from None
        hook = _hook(_json(response))
        if hook is None:
            raise ProviderError("Lemlist created no webhook id.", code="BAD_RESPONSE")
        return hook

    def ensure_hook(self, target_url: str, secret: str) -> tuple[LemlistHook, bool]:
        """The webhook for exactly `target_url`: the existing one, untouched
        (its secret cannot be checked: Lemlist never returns it), else a new
        one. True when it was created by this call."""
        for hook in self.list_hooks():
            if hook.target_url == target_url:
                return hook, False
        return self.create_hook(target_url, secret), True

    def delete_hook(self, hook_id: str) -> None:
        """Delete one webhook (`DELETE /hooks/{hookId}`, developer.lemlist.com,
        webhooks/delete-webhook): a 404 ("Webhook not found") means it is
        gone already."""
        self._request("DELETE", f"/hooks/{hook_id}", ok_codes=(404,))

    def recreate_hook(self, target_url: str, secret: str) -> tuple[LemlistHook, bool]:
        """The webhook for exactly `target_url`, created again with `secret`:
        the existing one for that URL deleted first (a secret is immutable at
        Lemlist: rotating it means deleting and recreating the hook), then a
        new one. A webhook of any other URL is never touched. True when one
        was deleted."""
        deleted = False
        for hook in self.list_hooks():
            if hook.target_url == target_url:
                self.delete_hook(hook.hook_id)
                deleted = True
        return self.create_hook(target_url, secret), deleted


def _hook(row: dict[str, Any]) -> LemlistHook | None:
    hook_id = row.get("_id")
    if not isinstance(hook_id, str) or not hook_id:
        return None
    target = row.get("targetUrl")
    kind = row.get("type")
    return LemlistHook(
        hook_id=hook_id,
        target_url=str(target) if target else None,
        type=str(kind) if kind else None,
    )


def _sending_mailboxes(campaign: dict[str, Any]) -> set[str]:
    """The mailboxes a Lemlist campaign sends from, lower-cased: its senders'
    addresses and, per sending user, the mailboxes it rotates."""
    found = {
        str(s["email"]).strip().lower()
        for s in campaign.get("senders") or []
        if isinstance(s, dict) and s.get("email")
    }
    for user in campaign.get("sendUsers") or []:
        if isinstance(user, dict):
            found.update(
                str(m["email"]).strip().lower()
                for m in user.get("mailboxes") or []
                if isinstance(m, dict) and m.get("email")
            )
    return found


def _says(response: httpx.Response, *phrases: str) -> bool:
    text = response.text.lower()
    return any(phrase in text for phrase in phrases)


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _json_list(response: httpx.Response) -> list[dict[str, Any]]:
    try:
        data = response.json()
    except ValueError:
        return []
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _rows(response: httpx.Response, *keys: str, what: str) -> list[dict[str, Any]]:
    """The rows of a listing: an array, or (never documented, read
    defensively) an object carrying it under one of `keys`. An empty body
    has no row; a body that is not JSON is refused."""
    if not (response.text or "").strip():
        return []
    try:
        data = response.json()
    except ValueError as exc:
        raise ProviderError(
            f"Lemlist answered the {what} with no JSON.", code="BAD_RESPONSE"
        ) from exc
    if isinstance(data, dict):
        data = next((data[key] for key in keys if isinstance(data.get(key), list)), [])
    return [row for row in data if isinstance(row, dict)] if isinstance(data, list) else []


def _text(value: Any) -> str | None:
    """A provider value as text, trimmed; None when absent or blank."""
    if value is None or isinstance(value, dict | list | bool):
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int:
    """A provider number as an int; 0 when absent or not a number."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _lead(row: dict[str, Any], fallback_id: str | None = None) -> ProviderLead | None:
    lead_id = row.get("_id") or row.get("id") or fallback_id
    if not lead_id:
        return None
    state = str(row["state"]) if row.get("state") else None
    lowered = (state or "").lower()
    return ProviderLead(
        lead_id=str(lead_id),
        email=row.get("email"),
        # Same defensive reading as Prism's legacy sync: some payloads only say `state`.
        is_paused=bool(row.get("isPaused")) or lowered == "paused",
        state=state,
        contact_id=str(row["contactId"]) if row.get("contactId") else None,
        # The state is the lead's last activity type (an unsubscribe, the
        # contact's included); `status` "unsubscribed" is documented too.
        unsubscribed=bool(row.get("isUnsubscribed"))
        or lowered in _UNSUBSCRIBED_STATES
        or str(row.get("status") or "").lower() == "unsubscribed",
        raw={k: row[k] for k in _LEAD_KEYS if k in row},
    )


#: Inbox message types, mapped to a direction. Other channels are not Nidria's.
_INBOX_DIRECTIONS = {"emailsSent": "OUTBOUND", "emailsReplied": "INBOUND"}


def _message(row: dict[str, Any]) -> ProviderMessage | None:
    direction = _INBOX_DIRECTIONS.get(str(row.get("type") or ""))
    message_id = row.get("_id")
    if direction is None or not message_id:
        return None
    step = row.get("sequenceStep")
    body = row.get("message")
    return ProviderMessage(
        message_id=str(message_id),
        direction=direction,
        occurred_at=_parse_time(row.get("createdAt")),
        campaign_id=row.get("campaignId"),
        lead_id=row.get("leadId"),
        contact_id=row.get("contactId"),
        step_position=(int(step) + 1) if isinstance(step, int) else None,
        subject=row.get("subject"),
        body_html=body if isinstance(body, str) else None,
        # The body is stored apart; the rest of the record is kept for audit.
        payload={k: v for k, v in row.items() if k != "message"},
    )


def _code(body: dict[str, Any]) -> str | None:
    """Lemlist's machine-readable error code: `code`, or `error` when it is one."""
    for key in ("code", "error"):
        value = body.get(key)
        if (
            isinstance(value, str)
            and value
            and value.replace("_", "").replace("-", "").isalnum()
            and " " not in value
        ):
            return value
    return None


def _error(response: httpx.Response) -> ProviderError:
    body = _json(response)
    detail = (
        body.get("message") or body.get("error") or response.text[:200] or response.reason_phrase
    )
    text = str(detail)
    raw = _code(body) or ""
    code = {
        401: "UNAUTHORIZED",
        402: "PLAN_REQUIRED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
    }.get(response.status_code, "PROVIDER_ERROR")
    lowered = f"{text} {response.text[:200]}".lower()
    if response.status_code in (402, 403) and ("plan" in lowered or "upgrade" in lowered):
        # Launching a lead needs the emailPro plan. Nidria never upgrades it.
        code = "PLAN_REQUIRED"
    elif raw == "LEAD_ALREADY_IN_OTHER_CAMPAIGN" or "already in other campaign" in lowered:
        code = "ALREADY_IN_OTHER_CAMPAIGN"
    elif raw == "LEAD_ALREADY_IN_CAMPAIGN" or "already in the campaign" in lowered:
        code = "ALREADY_IN_CAMPAIGN"
    elif "graveyard" in lowered:
        code = "EMAIL_UNSUBSCRIBED"
    elif raw == "campaign-reroute-in-progress":
        code = "RETRY_LATER"
    elif raw == "remove-step-on-sequence-in-use":
        # Measured: refused for any step once a lead entered, running or paused.
        code = "STEP_IN_USE"
    elif raw == "SEQUENCE_STEP_INSERT_ON_SEQUENCE_IN_USE":
        code = "INSERT_IN_USE"
    return ProviderError(f"Lemlist: {text}", code=code, status=response.status_code)
