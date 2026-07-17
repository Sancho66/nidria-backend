"""The referrer's view (GET /agencies/me/referrals): their code, the
discount currently POSED on their subscription (the existing read,
reused), and their godchildren — name and referral status ONLY, never the
godchild's plan, amounts or activity (the referrer introduced them, they
know the name; the rest is the godchild's business)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from src.billing.billing_schema import ReferralDiscountState


class ReferralCreditView(BaseModel):
    granted_at: datetime
    expires_at: datetime
    # expires_at in the future — the credit still weighs in the discount.
    active: bool


class ReferralEntry(BaseModel):
    agency_name: str
    # trial = essai en cours ; converted = converted_at posed ;
    # expired = trial calendar elapsed without conversion.
    status: Literal["trial", "converted", "expired"]
    referred_at: datetime
    # None until the godchild converts (the grant creates the row).
    credit: ReferralCreditView | None = None


class ReferrerViewResponse(BaseModel):
    referral_code: str | None
    current_discount: ReferralDiscountState | None = None
    referrals: list[ReferralEntry]
