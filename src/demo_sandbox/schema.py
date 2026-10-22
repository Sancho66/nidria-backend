import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.core.i18n import Language


class DemoSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(pattern=r"^[a-f0-9]{32}$")


class DemoSessionResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = 7200
    case_id: uuid.UUID
    language: Language
    read_only: Literal[True] = True
