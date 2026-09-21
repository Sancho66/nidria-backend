from datetime import datetime

from pydantic import BaseModel


class EmailQuotaResponse(BaseModel):
    used: int | None
    limit: int | None
    percent: float | None
    period: str | None
    observed_at: datetime | None
    status: str


class EmailUsageResponse(BaseModel):
    daily: EmailQuotaResponse
    monthly: EmailQuotaResponse
    blocked_until: datetime | None
    blocked_reason: str | None
    thresholds: list[int]
