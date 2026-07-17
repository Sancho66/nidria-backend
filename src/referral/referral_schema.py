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
    # expires_at in the future — the credit still counts in the tier.
    active: bool


class ReferralNextChange(BaseModel):
    """The NEXT tier drop, announced in advance — nobody discovers a
    decrease in their invoice. Derived from the ledger: the first expiry
    among active credits, and the tier that will remain after it
    (percent=0 means the discount ends there)."""

    date: datetime
    percent: int


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
    # None when no credit is active (nothing will change by itself).
    next_change: ReferralNextChange | None = None
