from functools import lru_cache
from typing import Annotated

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# OWASP's floor for bcrypt, and the value this app shipped with. Named here
# so the default and the boot guard can never drift apart.
BCRYPT_PRODUCTION_MINIMUM = 12


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
    # Password hashing work factor (bcrypt). Each +1 doubles the cost.
    # PRODUCTION FLOOR is BCRYPT_PRODUCTION_MINIMUM below; the test suite
    # lowers it via BCRYPT_ROUNDS because hashing was MEASURED as the single
    # biggest cost of the run (2697 hashes, 829s = 24.6% of worker time at
    # factor 12). Lowering it is safe ONLY in test, so `_refuse_weak_bcrypt`
    # refuses to boot anywhere else — the trade must never be silent.
    # verify_password reads the factor FROM the stored digest, so rows
    # hashed at 12 keep verifying at 12: no rehash, no migration.
    bcrypt_rounds: int = BCRYPT_PRODUCTION_MINIMUM
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
    # ONBOARDING MAIL (spec Eric 13/08, P1) — one mail ~10 min after an
    # agency is created, to the admin who created it.
    #   enabled: `bool | None` like `mock_email` — None = derive from the
    #   environment (production only), True/False force it. Default None
    #   means a local seed of 20 agencies sends ZERO mail without anyone
    #   having to remember a flag.
    onboarding_email_enabled: bool | None = None
    onboarding_email_delay_minutes: int = 10
    # How long past its due time an agency may still receive the mail. Also
    # what keeps the sweep off the EXISTING park: every agency created before
    # this feature is far outside the window, so no backfill is needed and
    # nobody gets a "welcome" months late. A job outage under 24 h catches up.
    onboarding_email_catchup_hours: int = 24
    # Internal addresses that never receive it (substring match on the
    # lowercased creator email) — comma-separated in env, never hardcoded.
    onboarding_email_excluded_patterns: Annotated[list[str], NoDecode] = [
        "@nidria.com",
        "@nidria.app",
        "+test@",
    ]
    # Eric's 3-minute walkthrough. FRENCH whatever the recipient language
    # (his decision): the mail is translated, the video is not.
    onboarding_video_url: str = "https://youtu.be/ciyXd7KZpnU"
    # Own kill switch, same idiom as `onboarding_email_enabled` (bool | None:
    # None = derive from the environment, production only). SEPARATE on
    # purpose: muting the agency's welcome mail must never silently blind the
    # team to new signups, nor the reverse. What the two mails DO share is the
    # exclusion list below — that, and not the switch, is the single
    # definition of "test account".
    signup_alert_enabled: bool | None = None
    # INTERNAL signup alert (demande Eric 13/08) — NOT the onboarding mail:
    # that one goes TO the agency at J+10 min, this one goes to US, at once,
    # so a signup can be traced and called back the same day. Recipients are
    # CONFIG (comma-separated) precisely so adding a colleague tomorrow is an
    # env change, not a deploy. Empty list = alert off.
    # The test-account guards are NOT duplicated here: this alert reuses
    # `onboarding_email_enabled` and `onboarding_email_excluded_patterns`
    # verbatim, so "test account" keeps ONE definition across both mails.
    signup_alert_recipients: Annotated[list[str], NoDecode] = [
        "mr.schalk.eric@gmail.com",
    ]
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
    # 30 days (décision 10/08) — a VALUE, never a hardcoded literal:
    # the stamp lands on agent_invitation.expires_at AT CREATION, so a
    # change of mind never resurrects already-expired rows.
    agent_invitation_expires_days: int = 30
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
    # Task attachments are a SEPARATE whitelist (superadmin ops backlog):
    # they also take .txt/.md notes. Deliberately NOT shared with the
    # case-documents list above — documents must never accept txt/md here.
    allowed_task_attachment_extensions: Annotated[list[str], NoDecode] = [
        "pdf",
        "jpg",
        "jpeg",
        "png",
        "txt",
        "md",
    ]

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
    # paddle_env drives the API base URL; prod runs PADDLE_ENV=live with
    # real subscriptions on it (confirmé Alexandre 08/08/2026 — the old
    # "KYB in progress" note was stale); dev stays on sandbox.
    # Offer kill switch, INDEPENDENT of the Paddle env: the offer opens
    # only when Eric validates (legal docs, tested invoices). Default
    # FALSE — closed by
    # default, opened explicitly, never the reverse. Gates the checkout
    # ENTRANCE only: an already-converted agency keeps full management,
    # and webhooks stay live (a living subscription keeps living).
    billing_checkout_enabled: bool = False
    # Billing-lock grace after a subscription enters past_due: Paddle's
    # dunning runs DURING past_due (it is posed at the FIRST failed
    # payment), so we leave the innocent expired-card case time to recover
    # before the workspace turns read-only.
    billing_past_due_grace_days: int = 7
    # Cloudflare Turnstile (signup anti-abuse), FLAG pattern: absent = the
    # check is skipped entirely; arming it = setting the variable, no
    # deploy (same doctrine as the billing kill switch).
    turnstile_secret: str | None = None
    # Boot catalog check opt-out for DEV (each uvicorn reload = one Paddle
    # GET; a dev session = dozens → Cloudflare 429, lived 2026-07-17).
    # Default TRUE (prod-safe: forgetting the var keeps the check that
    # validated the go-live); local .env sets it false — the catalog does
    # not change between two file saves.
    paddle_boot_check: bool = True
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
    # The PUBLIC URL of our webhook endpoint, for the destination
    # provisioning (localhost tunnel today, staging, then prod) — the
    # script never knows a URL; only the env does.
    paddle_webhook_url: str | None = None

    # E-signatures (méga-lot 28/07). GLOBAL feature flag, default FALSE —
    # flag off = no request materialized, no provider call, no credit
    # touched, client/webhook endpoints answer as if the feature did not
    # exist (same closed-by-default doctrine as billing_checkout_enabled).
    signatures_enabled: bool = False
    # DocuSeal (the only provider, behind the SignatureProvider port).
    # No separate sandbox HOST: the sandbox is a test-account API key on
    # the same base URL — configurable for on-prem instances.
    docuseal_api_key: str | None = None
    docuseal_base_url: str = "https://api.docuseal.com"
    # Webhook authenticity (méthode DocuSeal constatée : en-têtes SECRETS
    # personnalisés configurés dans leur console, pas de HMAC du corps) —
    # la même valeur est posée ici et dans le header X-Docuseal-Secret de
    # la config webhook DocuSeal ; comparaison constante côté endpoint.
    docuseal_webhook_secret: str | None = None
    # Builder embeddé (méga-lot modèles) : user_email du JWT = l'email du
    # compte DocuSeal propriétaire de la clé API (constat sonde).
    docuseal_account_email: str | None = None
    docuseal_builder_token_expires_minutes: int = 10
    signature_request_expires_days: int = 30
    # Credit packs (lot 2): Paddle price id → credits granted, JSON env —
    # same env-specific mapping doctrine as paddle_price_ids. The three
    # expected products (created MANUALLY in the Paddle dashboard):
    #   "Crédits signature ×50"  → 45,00 €  (0,90 €/crédit)
    #   "Crédits signature ×200" → 180,00 €
    #   "Crédits signature ×500" → 450,00 €
    # e.g. {"pri_sig50": 50, "pri_sig200": 200, "pri_sig500": 500}
    signature_credit_packs: dict[str, int] = {}
    # Lot grille 30/07 : les packs RETIRÉS de la vente (archivés Paddle)
    # mais dont un webhook re-livré doit TOUJOURS créditer — jamais
    # affichés par l'endpoint grille, honorés par le webhook seulement.
    signature_credit_packs_legacy: dict[str, int] = {}
    # Low-balance notification threshold DEFAULT; each agency may override
    # via settings["signature_credits_low_threshold"].
    signature_credits_low_threshold_default: int = 10
    # KPI de travail accompli (volet 2, 31/07) — env MAÎTRE, défaut false :
    # rien ne se sert flag off (même doctrine que les signatures). Eric
    # doit bénir le cadrage avant toute visibilité.
    kpi_enabled: bool = False
    # Barème « temps gagné » (lot 31/07) — EN CONFIG, jamais en dur :
    # minutes créditées par geste automatisé, ajustables sans release
    # (KPI_TIME_SAVED_MINUTES={"signature_completed": 45, ...} fusionne).
    # Chaque valeur doit tenir DEVANT UN DIRIGEANT, minute par minute —
    # c'est le critère, pas la générosité. L'ordre suit le parcours de
    # travail (créer, avancer, collecter, relancer, signer, clore, importer).
    kpi_time_saved_minutes: dict[str, int] = {
        # Le dossier monté depuis un parcours : la checklist n'est pas
        # réécrite, les étapes ni les délais non plus.
        "case_created_from_template": 20,
        # Une étape franchie : le point d'avancement qu'on ne fait pas au
        # téléphone, et le statut du dossier qui suit tout seul.
        "step_completed": 5,
        # Une pièce arrivée par le portail : ni relance, ni pièce jointe à
        # ranger, ni scan à renommer.
        "client_document_collected": 8,
        # Une relance AUTOMATIQUE : le suivi qu'aucun agent n'a eu à
        # penser, à rédiger, ni à recaler dans son agenda.
        "auto_reminder_sent": 10,
        # Une signature aboutie : impression, rendez-vous, scan, archivage.
        # La plus grosse valeur du barème, et la plus facile à défendre.
        "signature_completed": 25,
        # Un dossier clos : la vérification finale et le classement.
        "case_closed": 10,
        # Une fiche importée : la saisie manuelle qui n'a pas eu lieu.
        # Deux minutes, volontairement modeste — c'est le volume qui parle.
        "profile_imported": 2,
    }

    @field_validator("paddle_webhook_url", mode="before")
    @classmethod
    def _empty_url_is_none(cls, v: str | None) -> str | None:
        # An EMPTY env var means "absent" — it must override a .env value
        # (e.g. a live run shadowing the local tunnel URL), not become "".
        return v or None

    @field_validator(
        "cors_origins",
        "allowed_document_extensions",
        "allowed_task_attachment_extensions",
        "onboarding_email_excluded_patterns",
        "signup_alert_recipients",
        mode="before",
    )
    @classmethod
    def _parse_comma_list(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @model_validator(mode="after")
    def _refuse_weak_bcrypt(self) -> "Settings":
        """A cheap hashing factor is a silent password-cracking hole. The
        test suite is the ONLY place it is allowed (ENVIRONMENT=test), and
        anywhere else the process refuses to start rather than run fast and
        weak. Same posture as assert_all_routes_bound: fail loud at boot."""
        if self.bcrypt_rounds < BCRYPT_PRODUCTION_MINIMUM and self.environment != "test":
            raise ValueError(
                f"BCRYPT_ROUNDS={self.bcrypt_rounds} is below the production floor "
                f"({BCRYPT_PRODUCTION_MINIMUM}) and ENVIRONMENT={self.environment!r} is "
                "not 'test'. Refusing to start: this would silently weaken every "
                "password hash written from now on."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
