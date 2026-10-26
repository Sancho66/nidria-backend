import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.core.i18n import Language


class DemoTourEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step: int = Field(ge=0, le=8)
    status: Literal["started", "completed", "skipped"] = "started"

    @model_validator(mode="after")
    def valid_progress(self) -> "DemoTourEvent":
        if (self.status == "completed" and self.step != 8) or (
            self.status == "started" and self.step == 0
        ):
            raise ValueError("Invalid tour progress")
        return self


class DemoSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[a-f0-9]{32}$")
    face: Literal["agent", "expat"] | None = None
    case_id: uuid.UUID | None = None
    visit_id: uuid.UUID | None = None
    tour: DemoTourEvent | None = None


class DemoSessionResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = 7200
    case_id: uuid.UUID
    language: Language
    read_only: Literal[True] = True
    face: Literal["agent", "expat"] = "expat"
    agency_name: str = ""
    agency_demo: bool = False
    country: str | None = None
    expires_at: str | None = None
    persona: str | None = None
