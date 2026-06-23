"""Schemas for saved CRM import mappings (BLOC 3)."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class MappingUpsertRequest(BaseModel):
    # `id` present → EDIT that config (update by id); absent → CREATE a new
    # one (a same-name create conflicts → 409). `name` is part of the natural
    # key now, so it is required.
    id: uuid.UUID | None = None
    journey_template_id: uuid.UUID
    crm_slug: str = Field(min_length=1, max_length=100)
    # Required when crm_slug == "custom" (Autre / CRM générique): the free CRM
    # label. Ignored (nulled) for a referenced CRM.
    custom_crm_name: str | None = Field(default=None, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    mapping: dict[str, str]


class MappingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    journey_template_id: uuid.UUID
    crm_slug: str
    custom_crm_name: str | None
    name: str
    mapping: dict[str, str]


class MappingListResponse(BaseModel):
    mappings: list[MappingResponse]
