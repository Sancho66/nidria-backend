"""Pydantic contracts of the Email module.

Phase 1 holds the platform surface only (activation and Lemlist team binding,
`/agencies/{agency_id}/email-module`); the agent routes `/email/*` add theirs
here later.
"""

import uuid
from datetime import datetime

from pydantic import BaseModel

from src.email_campaigns.flags import ProviderMode


class EmailTeamInfo(BaseModel):
    """A Lemlist team: its id and, when Lemlist gave one, its name."""

    id: str
    name: str | None = None


class EmailCampaignCounts(BaseModel):
    """An agency's campaigns by status, every status present (0 when none)."""

    draft: int = 0
    active: int = 0
    paused: int = 0
    archived: int = 0
    # ACTIVE campaigns that exist at Lemlist (a real provider id, not a
    # dry-run one): what makes switching the module off refused.
    live_at_provider: int = 0


class AgencyEmailModuleResponse(BaseModel):
    """The Email module's state for one agency, as the platform sees it."""

    agency_id: uuid.UUID
    # The agency's own switch (its `agency_email_module` row; False without a row).
    enabled: bool
    # EMAIL_MODULE_ENABLED, the platform's master switch.
    master_enabled: bool
    # Both locks open: what the agency's agents get (`email_enabled` on its fiche).
    effective: bool
    # EMAIL_PROVIDER_EXTERNAL_EFFECTS AND Lemlist not mocked AND a key.
    external_effects: bool
    provider: str
    # The Lemlist team bound to this agency, or null (dry run).
    team: EmailTeamInfo | None
    # LIVE: real Lemlist calls. DRY_RUN: nothing leaves. UNAVAILABLE: bound,
    # effects on, and Lemlist did not say which team the key opens.
    provider_mode: ProviderMode
    # Bound, effects on, and the key opens ANOTHER team than the bound one.
    team_mismatch: bool
    enabled_at: datetime | None
    disabled_at: datetime | None
    # The last superadmin who changed the switch or the team.
    changed_by_agent_id: uuid.UUID | None
    campaigns: EmailCampaignCounts


class AgencyEmailModuleUpdateRequest(BaseModel):
    """PATCH /agencies/{agency_id}/email-module: the agency's switch."""

    enabled: bool
