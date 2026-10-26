"""Fail closed before opening a database or accepting a public demo session."""

from sqlalchemy.engine import make_url

from src.core.config import Settings

SANDBOX_DB_NAME = "nidria_demo_sandbox"
SANDBOX_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "nidria-demo-db.internal"})


def assert_isolated_settings(settings: Settings, *, seed: bool = False) -> None:
    if settings.environment != "demo_sandbox":
        raise RuntimeError("The demo runtime requires ENVIRONMENT=demo_sandbox")
    for address in (settings.database_url, settings.database_url_sync):
        url = make_url(address)
        if url.host not in SANDBOX_DB_HOSTS or url.database != SANDBOX_DB_NAME:
            raise RuntimeError("The demo runtime refuses this database destination")
        expected_user = "nidria_demo_seed" if seed else "nidria_demo_reader"
        if url.username != expected_user:
            raise RuntimeError("The demo runtime requires its dedicated database role")
    if (
        not settings.mock_services
        or settings.mock_email is not True
        or settings.mock_storage is not True
    ):
        raise RuntimeError("All demo service adapters must be mocked explicitly")
    if (
        settings.mock_lemlist is not True
        or settings.scheduler_enabled
        or settings.email_provider_external_effects
    ):
        raise RuntimeError("Demo providers and scheduled jobs must remain disabled")
    if settings.billing_checkout_enabled or settings.resend_monitor_enabled:
        raise RuntimeError("Billing and provider monitoring are forbidden in the sandbox")
    if any(
        getattr(settings, field)
        for field in (
            "resend_api_key",
            "resend_webhook_secret",
            "supabase_url",
            "supabase_service_role_key",
            "paddle_api_key",
            "paddle_webhook_secret",
            "ai_translation_api_key",
            "docuseal_api_key",
            "docuseal_webhook_secret",
            "lemlist_api_key",
        )
    ):
        raise RuntimeError("The demo runtime must not receive external-service credentials")
    secrets = (settings.jwt_agent_secret, settings.jwt_expat_secret, settings.jwt_refresh_secret)
    if len(set(secrets)) != 3 or any(len(secret) < 32 for secret in secrets):
        raise RuntimeError("The sandbox requires three independent authentication secrets")
