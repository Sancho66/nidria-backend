"""The Email module's switch, seen from the platform (superadmin) and read by
every surface that needs to know whether an agency has the module.

Platform gestures (routes gated `agency.create`, superadmin only, on the
pattern of PATCH /agencies/{agency_id}/subscription):
- switch the module on or off for one agency;
- bind the agency to the Lemlist team the platform key opens now, or unbind;
- pause every ACTIVE campaign of the agency, at Lemlist and in Nidria
  (`pause-all`, decision of 08/10/2026: the way to stop an agency's sends,
  a billing-locked one included; nothing automatic on the lock).

The rules (spec 4.1):
- switching off is refused while an ACTIVE campaign exists at Lemlist
  (`email.campaigns_live`): sending must never run without a screen to stop
  it; pause or archive the campaigns first, or switch off with
  `pause_campaigns`, which pauses them all then switches off;
- binding needs real effects and a key, and an answer from Lemlist
  (`email.provider_unavailable`), and a team serves one agency
  (`email.team_bound_elsewhere`);
- unbinding, or rebinding to another team, is refused once a campaign of the
  agency exists at Lemlist (`email.team_has_history`): moving an agency that
  went live to another team is V2.

The platform's Lemlist account (phase 4, spec 10.5; routes gated
`platform.task_manage`, superadmin only): Nidria's answer to "never a probe
in production", two traceable product routes instead of a shell inside the
machine.
- `provider_account`: read only. Without real effects (off, Lemlist
  mocked, or no key) Lemlist is NOT asked: `dry_run` and the local facts
  only. Otherwise the team the key opens (`GET /team`), the plan (`GET
  /user/channels`), the mailboxes, the webhook for EMAIL_WEBHOOK_URL (`GET
  /hooks`), cached 60 s, and the agency bound to that team (read fresh).
  The key is shown as `sha256[:12]` only.
- `ensure_webhook`: the team's webhook for EMAIL_WEBHOOK_URL with
  EMAIL_WEBHOOK_SECRET and no `type` (every event), idempotent: an existing
  one is returned untouched (its secret cannot be checked, Lemlist never
  returns it); with `recreate`, deleted then created again with the current
  secret (its rotation). The secret is never returned nor logged.
"""

import asyncio
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.agency import Agency
from shared.models.agency_email_module import AgencyEmailModule
from shared.models.agent import Agent
from shared.models.email_campaign import PROVIDER_LEMLIST
from src.agencies.agencies_repository import AgenciesRepository
from src.core.config import Settings, get_settings
from src.core.exceptions import ConflictError, NotFoundError
from src.email_campaigns.email_repository import EmailRepository
from src.email_campaigns.email_schema import (
    AgencyEmailModuleResponse,
    EmailBoundAgencyRead,
    EmailCampaignCounts,
    EmailPauseAllResult,
    EmailPauseFailure,
    EmailProviderAccountRead,
    EmailProviderHookRead,
    EmailProviderSenderRead,
    EmailProviderWebhookRead,
    EmailProviderWebhookResult,
    EmailTeamInfo,
)
from src.email_campaigns.flags import (
    email_module_effectively_enabled,
    external_effects_on,
    is_real_provider_id,
    lemlist_mocked,
    provider_state_for,
)
from src.email_campaigns.lemlist_team import (
    LemlistTeam,
    TeamUnknownError,
    fingerprint,
    team_of_key,
)
from src.email_campaigns.providers.base import EmailProvider, ProviderError, ProviderSender
from src.email_campaigns.providers.lemlist import LemlistEmailProvider, LemlistHook
from src.email_campaigns.readiness import team_mismatch as on_other_team

#: How long the account's Lemlist answers are kept (spec 10.5).
ACCOUNT_TTL_SECONDS = 60.0


@dataclass(frozen=True)
class _Account:
    """What Lemlist said of the platform's account, cached."""

    team: EmailTeamInfo | None
    plan: str | None
    senders: tuple[ProviderSender, ...]
    hooks: tuple[LemlistHook, ...]


#: (key fingerprint) -> (monotonic time of the answer, the answer).
_ACCOUNT_CACHE: dict[str, tuple[float, _Account]] = {}


def clear_account_cache() -> None:
    """Forget the cached account answers (tests; a webhook just created)."""
    _ACCOUNT_CACHE.clear()


def lemlist_account(settings: Settings) -> LemlistEmailProvider:
    """The adapter on the platform key, for the account routes. Tests
    replace it by an adapter on a `FakeLemlist` transport."""
    return LemlistEmailProvider(settings.lemlist_api_key)


def _provider_refusal(exc: ProviderError) -> ConflictError:
    """Lemlist's refusal as the module's 409 (`email.provider_*`)."""
    # Imported here: the agent manager is heavy and reads this module's peers.
    from src.email_campaigns.email_manager import provider_refusal

    return provider_refusal(exc)


def _unavailable() -> ConflictError:
    return ConflictError(
        "Lemlist is not reachable from this platform: real effects are off, "
        "Lemlist is mocked, or there is no key.",
        code="email.provider_unavailable",
        params={"reason": "not_configured"},
    )


class EmailAdminManager:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo = EmailRepository(db)

    # --- reads shared with the agency surfaces ------------------------------------

    async def email_enabled_for(self, agency_id: uuid.UUID) -> bool:
        """The agency's EFFECTIVE switch (env master AND its row): the
        `email_enabled` of its fiche."""
        if not get_settings().email_module_enabled:
            return False  # the master switch: no query needed
        return email_module_effectively_enabled(await self.repo.get_module(agency_id))

    async def email_enabled_agency_ids(self, agency_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
        """`email_enabled_for` for many agencies, in one query."""
        if not get_settings().email_module_enabled:
            return set()
        return await self.repo.enabled_agency_ids(agency_ids)

    # --- platform routes ------------------------------------------------------------

    async def get_state(self, agency_id: uuid.UUID) -> AgencyEmailModuleResponse:
        await self._agency(agency_id)
        return await self._state(agency_id)

    async def set_enabled(
        self,
        superadmin: Agent,
        agency_id: uuid.UUID,
        enabled: bool,
        *,
        pause_campaigns: bool = False,
    ) -> AgencyEmailModuleResponse:
        """Switch the module on or off. Off, with `pause_campaigns`: every
        ACTIVE campaign is paused first (`pause_all`), then the usual rule
        holds: a campaign still ACTIVE at Lemlist refuses the switch-off."""
        await self._agency(agency_id)
        now = datetime.now(UTC)
        if enabled:
            row = await self.repo.lock_or_create_module(agency_id)
            if not row.enabled:
                row.enabled = True
                row.enabled_at = now
                row.changed_by_agent_id = superadmin.id
            await self.db.commit()
            return await self._state(agency_id)
        if pause_campaigns:
            await self._pause_campaigns(agency_id)
            now = datetime.now(UTC)
        row_or_none = await self.repo.get_module(agency_id, for_update=True)
        if row_or_none is not None and row_or_none.enabled:
            live = await self.repo.count_live_provider_campaigns(agency_id)
            if live:
                await self.db.rollback()
                raise ConflictError(
                    "Campaigns of this agency are running at Lemlist; archive them "
                    "before switching the Email module off.",
                    code="email.campaigns_live",
                    params={"count": live},
                )
            row_or_none.enabled = False
            row_or_none.disabled_at = now
            row_or_none.changed_by_agent_id = superadmin.id
        await self.db.commit()
        return await self._state(agency_id)

    async def pause_all(self, agency_id: uuid.UUID) -> EmailPauseAllResult:
        """Pause every ACTIVE campaign of the agency, imported ones included,
        at Lemlist and in Nidria (see `_pause_campaigns`). Idempotent. The
        agency is the path's: under a billing lock of ITS own, it is paused
        all the same (the lock reads the actor's agency, the superadmin's)."""
        await self._agency(agency_id)
        paused, already, failed = await self._pause_campaigns(agency_id)
        return EmailPauseAllResult(
            paused=paused,
            already_paused=already,
            failed=failed,
            module=await self._state(agency_id),
        )

    async def _pause_campaigns(
        self, agency_id: uuid.UUID
    ) -> tuple[int, int, list[EmailPauseFailure]]:
        """Each ACTIVE campaign of the agency becomes PAUSED, one commit per
        campaign: (paused, not ACTIVE, failed). One that lives at Lemlist is
        paused there first, through the agency's provider (the rules of a
        status change, `EmailManager._transition`): never while the agency
        does not talk to Lemlist for real (`email.provider_unavailable`),
        nor for a campaign of another Lemlist team (`email.team_mismatch`,
        its ids are never sent to the current team). A dry run's campaign,
        or one never synced, is paused in Nidria only. A failure is told
        with its code and the next campaign goes on."""
        # Imported here: the agent manager is heavy and reads this module's peers.
        from src.email_campaigns.email_manager import (
            EmailManager,
            campaign_ref,
            provider_error_code,
            provider_params,
        )

        manager = EmailManager(self.db)
        by_status = await self.repo.count_campaigns_by_status(agency_id)
        active = await self.repo.campaign_ids(agency_id, "ACTIVE")
        already = sum(count for status, count in by_status.items() if status != "ACTIVE")
        paused = 0
        failed: list[EmailPauseFailure] = []
        # The agency's Lemlist, asked once (at the first real campaign): the
        # provider and the key's team, or why it cannot be reached for real.
        asked = False
        lemlist: EmailProvider | None = None
        key_team: str | None = None
        unreachable: tuple[str, dict[str, Any]] | None = None
        for campaign_id in active:
            campaign = await self.repo.campaign(agency_id, campaign_id, lock=True)
            if campaign is None or campaign.status != "ACTIVE":
                # Paused, archived or deleted meanwhile: nothing to do.
                await self.db.rollback()
                already += 1
                continue
            refusal: tuple[str, dict[str, Any]] | None = None
            if is_real_provider_id(campaign.provider_campaign_id):
                if not asked:
                    asked = True
                    try:
                        provider = await manager.provider_of(await self.repo.get_module(agency_id))
                        # A real id needs real effects (409 email.provider_unavailable).
                        manager.through_provider(provider, campaign.provider_campaign_id)
                        key_team = await asyncio.to_thread(provider.team_id)
                        lemlist = provider
                    except ConflictError as exc:
                        unreachable = (exc.code, dict(exc.params))
                    except ProviderError as exc:
                        unreachable = (provider_error_code(exc), provider_params(exc))
                if lemlist is None:
                    assert unreachable is not None
                    refusal = unreachable
                elif on_other_team(campaign, key_team):
                    # Its ids are never sent to the current team (rule N4).
                    refusal = ("email.team_mismatch", {})
                else:
                    ref = campaign_ref(campaign)
                    assert ref is not None  # a real provider campaign
                    try:
                        await asyncio.to_thread(lemlist.pause_campaign, ref)
                    except ProviderError as exc:
                        refusal = (provider_error_code(exc), provider_params(exc))
            if refusal is not None:
                await self.db.rollback()
                failed.append(
                    EmailPauseFailure(campaign_id=campaign_id, code=refusal[0], params=refusal[1])
                )
                continue
            campaign.status = "PAUSED"
            await self.db.commit()
            paused += 1
        return paused, already, failed

    async def bind_team(self, superadmin: Agent, agency_id: uuid.UUID) -> AgencyEmailModuleResponse:
        """Bind the agency to the team the platform key opens NOW."""
        await self._agency(agency_id)
        team = await self._key_team()
        owner = await self.repo.team_owner(PROVIDER_LEMLIST, team.id)
        if owner is not None and owner[0] != agency_id:
            raise self._bound_elsewhere(*owner)
        row = await self.repo.lock_or_create_module(agency_id)
        if row.provider_team_id is not None and row.provider_team_id != team.id:
            await self._refuse_if_history(agency_id)
        if row.provider_team_id != team.id:
            row.changed_by_agent_id = superadmin.id
        row.provider = PROVIDER_LEMLIST
        row.provider_team_id = team.id
        row.provider_team_name = team.name
        try:
            await self.db.commit()
        except IntegrityError:
            # Another agency bound the same team between our check and commit
            # (uq_agency_email_module_team): the same refusal, named.
            await self.db.rollback()
            owner = await self.repo.team_owner(PROVIDER_LEMLIST, team.id)
            if owner is None or owner[0] == agency_id:
                raise
            raise self._bound_elsewhere(*owner) from None
        return await self._state(agency_id)

    async def unbind_team(
        self, superadmin: Agent, agency_id: uuid.UUID
    ) -> AgencyEmailModuleResponse:
        await self._agency(agency_id)
        row = await self.repo.get_module(agency_id, for_update=True)
        if row is not None and row.provider_team_id is not None:
            await self._refuse_if_history(agency_id)
            row.provider_team_id = None
            row.provider_team_name = None
            row.changed_by_agent_id = superadmin.id
        await self.db.commit()
        return await self._state(agency_id)

    # --- the platform's Lemlist account (spec 10.5) ---------------------------------

    async def provider_account(self) -> EmailProviderAccountRead:
        """The platform's Lemlist account, read only; no call at all without
        real effects (`dry_run`). Never the key itself: its fingerprint."""
        settings = get_settings()
        key = settings.lemlist_api_key
        local = {
            "external_effects": external_effects_on(settings),
            "mocked": lemlist_mocked(settings),
            "key_configured": bool(key),
            "key_fingerprint": fingerprint(key) if key else None,
        }
        if not local["external_effects"]:
            return EmailProviderAccountRead(dry_run=True, **local)
        account = await self._account(settings)
        url = settings.email_webhook_url
        hook = next((h for h in account.hooks if url and h.target_url == url), None)
        owner = (
            await self.repo.team_owner(PROVIDER_LEMLIST, account.team.id)
            if account.team is not None
            else None
        )
        return EmailProviderAccountRead(
            dry_run=False,
            **local,
            team=account.team,
            plan=account.plan,
            senders=[
                EmailProviderSenderRead(user_id=s.user_id, email=s.email, campaigns=s.campaigns)
                for s in account.senders
            ],
            webhook=EmailProviderWebhookRead(
                configured_url=url,
                exists=hook is not None,
                hook_id=hook.hook_id if hook is not None else None,
                has_type=bool(hook is not None and hook.type),
            ),
            bound_agency=(
                EmailBoundAgencyRead(id=owner[0], name=owner[1]) if owner is not None else None
            ),
        )

    async def _account(self, settings: Settings) -> _Account:
        """Lemlist's answers on the account, kept 60 s per key."""
        digest = fingerprint(settings.lemlist_api_key)
        hit = _ACCOUNT_CACHE.get(digest)
        if hit is not None and time.monotonic() - hit[0] < ACCOUNT_TTL_SECONDS:
            return hit[1]
        adapter = lemlist_account(settings)
        try:
            team = await asyncio.to_thread(adapter.read_team)
            plan = await asyncio.to_thread(adapter.read_plan)
            senders = await asyncio.to_thread(adapter.list_senders)
            hooks = await asyncio.to_thread(adapter.list_hooks)
        except ProviderError as exc:
            raise _provider_refusal(exc) from None
        account = _Account(
            team=EmailTeamInfo(id=team.team_id, name=team.name) if team is not None else None,
            plan=plan,
            senders=tuple(senders),
            hooks=tuple(hooks),
        )
        _ACCOUNT_CACHE[digest] = (time.monotonic(), account)
        return account

    async def ensure_webhook(self, *, recreate: bool = False) -> EmailProviderWebhookResult:
        """The team's webhook for EMAIL_WEBHOOK_URL, with no type (every
        event) and EMAIL_WEBHOOK_SECRET: the existing one, untouched, else
        a new one. With `recreate`, the existing one for that URL is
        deleted (`DELETE /hooks/{hookId}`) and created again with the
        current secret: a secret is immutable at Lemlist, so this is its
        rotation; a webhook of another URL is never touched. 409
        `email.provider_unavailable` without real effects; 409
        `email.webhook_not_configured` (params `missing`: `url`, `secret`
        or `both`) when the env lacks the URL or the secret."""
        settings = get_settings()
        if not external_effects_on(settings):
            raise _unavailable()
        url = settings.email_webhook_url
        secret = settings.email_webhook_secret
        if not url or not secret:
            missing = "both" if not url and not secret else "url" if not url else "secret"
            raise ConflictError(
                "The Lemlist webhook needs EMAIL_WEBHOOK_URL and EMAIL_WEBHOOK_SECRET.",
                code="email.webhook_not_configured",
                params={"missing": missing},
            )
        adapter = lemlist_account(settings)
        recreated = False
        try:
            if recreate:
                hook, recreated = await asyncio.to_thread(adapter.recreate_hook, url, secret)
                created = True
            else:
                hook, created = await asyncio.to_thread(adapter.ensure_hook, url, secret)
        except ProviderError as exc:
            # The adapter keeps the secret out of its errors.
            raise _provider_refusal(exc) from None
        finally:
            # Whatever happened at Lemlist, the account is read afresh.
            clear_account_cache()
        return EmailProviderWebhookResult(
            created=created,
            recreated=recreated,
            webhook=EmailProviderHookRead(
                hook_id=hook.hook_id, target_url=hook.target_url, has_type=bool(hook.type)
            ),
        )

    # --- internals --------------------------------------------------------------------

    async def _agency(self, agency_id: uuid.UUID) -> Agency:
        agency = await AgenciesRepository(self.db).get_agency(agency_id)
        if agency is None:
            raise NotFoundError("Agency not found.", code="agency.not_found")
        return agency

    async def _key_team(self) -> LemlistTeam:
        settings = get_settings()
        if not external_effects_on(settings):
            raise _unavailable()
        try:
            team = await asyncio.to_thread(team_of_key, settings)
        except TeamUnknownError:
            raise ConflictError(
                "Lemlist did not say which team the platform key opens.",
                code="email.provider_unavailable",
                params={"reason": "team_unknown"},
            ) from None
        assert team is not None  # effects on: a team, or TeamUnknownError
        return team

    async def _refuse_if_history(self, agency_id: uuid.UUID) -> None:
        count = await self.repo.count_provider_campaigns(agency_id)
        if count:
            await self.db.rollback()
            raise ConflictError(
                "Campaigns of this agency exist in its Lemlist team; its team cannot change.",
                code="email.team_has_history",
                params={"count": count},
            )

    @staticmethod
    def _bound_elsewhere(owner_id: uuid.UUID, owner_name: str) -> ConflictError:
        return ConflictError(
            "This Lemlist team already serves another agency.",
            code="email.team_bound_elsewhere",
            params={"agency_id": str(owner_id), "agency_name": owner_name},
        )

    async def _state(self, agency_id: uuid.UUID) -> AgencyEmailModuleResponse:
        settings = get_settings()
        row: AgencyEmailModule | None = await self.repo.get_module(agency_id)
        by_status = await self.repo.count_campaigns_by_status(agency_id)
        live = await self.repo.count_live_provider_campaigns(agency_id)
        # May ask Lemlist (cached ten minutes): only when the agency is bound,
        # the module effective and effects on.
        provider = await asyncio.to_thread(provider_state_for, row, settings)
        return AgencyEmailModuleResponse(
            agency_id=agency_id,
            enabled=bool(row and row.enabled),
            master_enabled=settings.email_module_enabled,
            effective=email_module_effectively_enabled(row, settings),
            external_effects=external_effects_on(settings),
            provider=row.provider if row is not None else PROVIDER_LEMLIST,
            team=(
                EmailTeamInfo(id=row.provider_team_id, name=row.provider_team_name)
                if row is not None and row.provider_team_id is not None
                else None
            ),
            provider_mode=provider.mode,
            team_mismatch=provider.team_mismatch,
            enabled_at=row.enabled_at if row is not None else None,
            disabled_at=row.disabled_at if row is not None else None,
            changed_by_agent_id=row.changed_by_agent_id if row is not None else None,
            campaigns=EmailCampaignCounts(
                draft=by_status.get("DRAFT", 0),
                active=by_status.get("ACTIVE", 0),
                paused=by_status.get("PAUSED", 0),
                archived=by_status.get("ARCHIVED", 0),
                live_at_provider=live,
            ),
        )
