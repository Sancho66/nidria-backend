from functools import lru_cache
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Database
    database_url: str
    database_url_sync: str

    # Auth (JWT) — two access audiences (agent / expat) with separate
    # secrets, plus a single refresh secret. The refresh token carries
    # an `audience` claim validated by each refresh endpoint.
    jwt_agent_secret: str
    jwt_expat_secret: str
    jwt_refresh_secret: str
    jwt_algorithm: str = "HS256"
    access_token_expires_minutes: int = 30
    refresh_token_expires_days: int = 7
    # Impersonation: short-lived access token, NO refresh — expiry IS the exit.
    impersonation_token_expires_minutes: int = 30
    password_reset_token_expires_minutes: int = 60
    # 2FA (bloc 2): lifetime of the ephemeral login step-2 token, and the
    # server-side attempts cap per challenge (then back to step 1).
    mfa_token_expires_minutes: int = 5
    mfa_max_attempts: int = 5
    # Onboarding links (first admin of a created agency) reuse the reset
    # machinery but are INVITATIONS: 24h, not the 60-minute reset window
    # (Sidney locked out at H+1, demande Eric).
    onboarding_link_expires_minutes: int = 24 * 60
    # Usage trackers bloc 1: free-trial length set by the agency wizard.
    trial_days: int = 30
    # Nurture bloc 3 — trial emails. From AND Reply-To: Eric's personal
    # brand address (Cloudflare routes replies to his business inbox),
    # distinct from the transactional email_from. Sent via the same
    # verified Resend domain.
    nurture_from: str = "eric@nidria.com"
    # Eric's booking link, injected into the J+28 mails. EMPTY = the
    # J+28 slot is held back (pending_config), never sent with a hole.
    nurture_booking_url: str = ""
    # Never nurtured: the platform agency + internal test agencies.
    nurture_excluded_slugs: list[str] = ["nidria-demo"]
    # AI translation (journey templates, GLM via Z.ai OpenAI-compatible API).
    ai_translation_base_url: str = "https://api.z.ai/api/paas/v4"
    ai_translation_api_key: str = ""
    ai_translation_model: str = "glm-4.7-flash"
    # Flash models fit a full journey in ~20-25s with thinking disabled;
    # raise locally/prod if the provider slows down.
    ai_translation_timeout_seconds: float = 30.0
    # Monthly per-agency quota in POINTS (1 point = a tenth of a cent of
    # model cost, floor 1 per successful call) — debited on success only.
    ai_translation_monthly_points: int = 200  # = 20 cents/month (Alex, 2026-07-05)
    # Model list prices (USD per Mtoken) — estimation AND debit follow the
    # CONFIGURED model through these. Defaults = glm-4.7-flash (0.06/0.40);
    # for the full glm-4.7 set 0.40/1.75 alongside the model switch.
    ai_translation_price_input_usd_per_mtok: float = 0.06
    ai_translation_price_output_usd_per_mtok: float = 0.40
    agent_invitation_expires_days: int = 7
    # Expats are clients, not staff — longer runway than agent invites.
    case_invitation_expires_days: int = 14

    # Scheduler (reminder dispatch). Job crons live in DATA (job_config);
    # only the auto-follow-up thresholds are global config.
    scheduler_enabled: bool = False
    auto_reminder_thresholds_days: list[int] = [20, 30]

    # Global mock toggle. When True (default, for safety), all external
    # services return realistic mock data instead of hitting the real
    # APIs. Per-service overrides below let you flip a single integration
    # to real while keeping the rest mocked.
    mock_services: bool = True
    # Per-service override, `bool | None`:
    #   None  → fall back to the global `mock_services`
    #   True  → force mock even if the global is False
    #   False → force real calls even if the global is True
    mock_email: bool | None = None
    mock_storage: bool | None = None

    # Documents (immigration pieces: scanned passports, certificates,
    # photos — doc/docx waits for a real ask)
    max_document_size_mb: int = 10
    allowed_document_extensions: Annotated[list[str], NoDecode] = ["pdf", "jpg", "jpeg", "png"]

    # API
    cors_origins: Annotated[list[str], NoDecode] = [
        "http://localhost:3000",
        "http://localhost:5173",
    ]
    environment: str = "development"
    frontend_url: str = "http://localhost:5173"

    # Resend transactional email (invitations + mail reminders).
    resend_api_key: str | None = None
    email_from: str = "Nidria <no-reply@nidria.com>"

    # Supabase Storage — documents bucket. `supabase_service_role_key`
    # is the SERVICE ROLE key (not the anon key): it bypasses RLS so
    # the backend can upload/delete/sign on a private bucket. Optional
    # so the app boots in test/CI without these secrets; the storage
    # client lazily errors at first use if they're missing.
    supabase_url: str | None = None
    supabase_service_role_key: str | None = None
    supabase_storage_bucket: str = "documents"

    # Paddle (Merchant of Record, self-serve billing). Optional so the app
    # boots without them (billing_mode="manual" everywhere works Paddle-less);
    # the billing endpoints error explicitly at first use if missing.
    # paddle_env drives the API base URL; the LIVE account does not exist yet
    # (KYB in progress) — everything is built against sandbox.
    paddle_env: str = "sandbox"  # sandbox | live
    paddle_api_key: str | None = None
    paddle_webhook_secret: str | None = None
    # The 8 price ids, JSON env (they DIFFER between sandbox and live). Keys
    # follow the enum values (plan + French cycle, structure F vocabulary):
    # {"cabinet_mensuel": "pri_...", "cabinet_annuel": ..., "agence_mensuel":
    #  ..., "agence_annuel": ..., "seat_cabinet_mensuel": ...,
    #  "seat_cabinet_annuel": ..., "seat_agence_mensuel": ...,
    #  "seat_agence_annuel": ...}
    paddle_price_ids: dict[str, str] = {}

    @field_validator("cors_origins", "allowed_document_extensions", mode="before")
    @classmethod
    def _parse_comma_list(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
