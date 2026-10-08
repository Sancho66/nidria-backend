"""The provider used while external effects are off (the default).

Answers like the real provider with deterministic identifiers, records every
operation it was asked for, and sends nothing anywhere. A campaign "synced" in
dry-run is complete on the Nidria side and ready for a real sync later.

Ported as is from Prism's `providers/dry_run.py` (reference commit 629a2b0),
plus `resume_campaign` (lot C), recorded like the others. It reads nothing
for an import (`ImportReader`): an import needs the real provider.
"""

import hashlib
import logging
from datetime import datetime
from typing import Any

from src.email_campaigns.providers.base import (
    CampaignRef,
    CampaignSpec,
    LeadRef,
    LeadSpec,
    ProviderCampaign,
    ProviderEvent,
    ProviderLead,
    ProviderLeadListing,
    ProviderMessage,
    ProviderSender,
    ProviderStep,
    StepContent,
    StepSpec,
)

logger = logging.getLogger(__name__)


#: Every identifier this provider hands out starts with it, so a dry-run id is
#: never mistaken for a real one (see `is_dry_run_id`).
DRY_RUN_PREFIX = "dry_"


def is_dry_run_id(value: str | None) -> bool:
    return bool(value) and str(value).startswith(DRY_RUN_PREFIX)


def _id(prefix: str, *parts: object) -> str:
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]
    return f"{DRY_RUN_PREFIX}{prefix}_{digest}"


class DryRunEmailProvider:
    name = "LEMLIST"
    external = False

    def __init__(self) -> None:
        #: (operation, arguments) in call order: what a real sync would have done.
        self.operations: list[tuple[str, dict[str, Any]]] = []
        self._paused: set[str] = set()

    def _record(self, operation: str, **arguments: Any) -> None:
        self.operations.append((operation, arguments))
        logger.info("[email dry-run] %s %s", operation, sorted(arguments))

    def create_campaign(self, spec: CampaignSpec) -> CampaignRef:
        self._record("create_campaign", name=spec.name, schedule=spec.schedule)
        campaign_id = _id("cam", spec.name, datetime.now().isoformat())
        return CampaignRef(
            campaign_id=campaign_id,
            sequence_id=_id("seq", campaign_id),
            schedule_id=_id("skd", campaign_id),
        )

    def update_campaign(self, ref: CampaignRef, spec: CampaignSpec) -> None:
        self._record(
            "update_campaign", campaign=ref.campaign_id, name=spec.name, schedule=spec.schedule
        )

    def start_campaign(self, ref: CampaignRef, *, mailbox: str | None = None) -> None:
        self._record("start_campaign", campaign=ref.campaign_id)

    def resume_campaign(self, ref: CampaignRef) -> None:
        self._record("resume_campaign", campaign=ref.campaign_id)

    def pause_campaign(self, ref: CampaignRef) -> None:
        self._record("pause_campaign", campaign=ref.campaign_id)

    def archive_campaign(self, ref: CampaignRef) -> None:
        self._record("archive_campaign", campaign=ref.campaign_id)

    def list_senders(self) -> list[ProviderSender]:
        # Lemlist is not asked in a dry run: nobody to choose from.
        return []

    def team_id(self) -> str | None:
        # Lemlist is not asked in a dry run; dry-run ids belong to no team.
        return None

    def read_campaign(self, ref: CampaignRef) -> ProviderCampaign:
        return ProviderCampaign(ref.campaign_id, exists=True, status="dry-run")

    def create_step(self, ref: CampaignRef, step: StepSpec) -> str:
        self._record("create_step", campaign=ref.campaign_id, position=step.position)
        return _id("stp", ref.campaign_id, step.position, step.name)

    def update_step(self, ref: CampaignRef, step_id: str, step: StepSpec) -> None:
        self._record("update_step", step=step_id, delay_days=step.delay_days)

    def delete_step(self, ref: CampaignRef, step_id: str) -> None:
        self._record("delete_step", step=step_id)

    def reorder_steps(self, ref: CampaignRef, steps: list[tuple[str, StepSpec]]) -> None:
        self._record("reorder_steps", campaign=ref.campaign_id, steps=[s for s, _ in steps])

    def read_steps(self, ref: CampaignRef) -> list[ProviderStep]:
        # Nothing exists anywhere: no step left over to reuse.
        return []

    def park_step(self, ref: CampaignRef, step_id: str) -> None:
        self._record("park_step", step=step_id)

    def add_contact(self, ref: CampaignRef, lead: LeadSpec) -> LeadRef:
        self._record("add_contact", campaign=ref.campaign_id)
        return LeadRef(
            lead_id=_id("lea", ref.campaign_id, lead.email), contact_id=_id("ctc", lead.email)
        )

    def set_step_content(
        self, ref: CampaignRef, lead: LeadRef, step_id: str, content: StepContent
    ) -> bool:
        self._record("set_step_content", lead=lead.lead_id, step=step_id)
        return True

    def launch_contact(self, lead: LeadRef) -> None:
        self._record("launch_contact", lead=lead.lead_id)

    def pause_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._record("pause_contact", lead=lead.lead_id)
        self._paused.add(lead.lead_id)

    def resume_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._record("resume_contact", lead=lead.lead_id)
        self._paused.discard(lead.lead_id)

    def remove_contact(self, ref: CampaignRef, lead: LeadRef) -> None:
        self._record("remove_contact", lead=lead.lead_id)

    def get_contact(self, lead: LeadRef) -> ProviderLead | None:
        return ProviderLead(
            lead_id=lead.lead_id, email=None, is_paused=lead.lead_id in self._paused
        )

    def list_contacts(self, ref: CampaignRef) -> ProviderLeadListing:
        return ProviderLeadListing(leads=[], complete=True)

    def sync_campaign(self, ref: CampaignRef, since: datetime | None) -> list[ProviderEvent]:
        self._record("sync_campaign", campaign=ref.campaign_id)
        return []

    def get_messages(self, lead: LeadRef) -> list[ProviderMessage]:
        return []

    def sync_contact(self, ref: CampaignRef, lead: LeadRef) -> list[ProviderEvent]:
        return []
