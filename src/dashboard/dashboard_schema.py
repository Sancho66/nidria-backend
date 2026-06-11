from pydantic import BaseModel


class DashboardResponse(BaseModel):
    """Simple counts, agency-scoped. NOTHING more — rich analytics is
    V1.5 and stays there."""

    total_cases: int
    by_status: dict[str, int]
    by_dest_country: dict[str, int]
