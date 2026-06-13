import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.enums import CustomFieldType

# A slug: lowercase letters/digits/underscores. Stable JSONB key.
_KEY_PATTERN = r"^[a-z][a-z0-9_]{0,49}$"
_SELECT_TYPES = {CustomFieldType.SELECT, CustomFieldType.MULTI_SELECT}


class CustomFieldDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    field_type: str
    options: list[str] | None
    required: bool
    position: int
    archived_at: datetime | None


class CustomFieldDefinitionCreate(BaseModel):
    key: str = Field(pattern=_KEY_PATTERN)
    label: str = Field(min_length=1, max_length=200)
    field_type: CustomFieldType
    options: list[str] | None = None
    required: bool = False
    position: int = 0

    @model_validator(mode="after")
    def _check_options(self) -> "CustomFieldDefinitionCreate":
        if self.field_type in _SELECT_TYPES:
            if not self.options:
                raise ValueError(f"{self.field_type} requires a non-empty `options` list.")
            if len(set(self.options)) != len(self.options):
                raise ValueError("`options` must be unique.")
        elif self.options is not None:
            raise ValueError(f"`options` is only valid for {[t.value for t in _SELECT_TYPES]}.")
        return self


class CustomFieldDefinitionUpdate(BaseModel):
    """`key` and `field_type` are IMMUTABLE — deliberately absent."""

    label: str | None = Field(default=None, min_length=1, max_length=200)
    options: list[str] | None = None
    required: bool | None = None
    position: int | None = None
