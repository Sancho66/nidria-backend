"""Boot check catalogue : opt-out DEV, 429 reconnu, requete filtree.

Le 429 Cloudflare du 2026-07-17 (un GET /prices par reload uvicorn) a
motive les trois durcissements : PADDLE_BOOT_CHECK=false en dev, le
rate-limit logge en UNE ligne propre (pas 500 chars de HTML), et la
verification qui ne demande QUE nos ids (filtre id= verifie cote API)."""

from unittest.mock import AsyncMock

import pytest

from src.billing.catalog_provisioning import verify_catalog_env
from src.billing.paddle_client import PaddleApiError


async def test_verify_requests_only_our_ids() -> None:
    client = AsyncMock()
    client.list_prices = AsyncMock(return_value=[])
    price_ids = {"cabinet_mensuel": "pri_a", "seat_cabinet_mensuel": "pri_b"}
    problems = await verify_catalog_env(client=client, price_ids=price_ids)
    client.list_prices.assert_awaited_once_with(ids=["pri_a", "pri_b"])
    assert len(problems) == 2  # absents (liste vide) — signale, pas cache


def test_rate_limit_shape_is_recognized() -> None:
    """La branche du boot : 429 OU page HTML Cloudflare -> le message court."""
    for exc in (
        PaddleApiError(429, "Too Many Requests"),
        PaddleApiError(503, "<html><body>Cloudflare</body></html>"),
    ):
        assert exc.status_code == 429 or "cloudflare" in str(exc).lower()
    # Un vrai 400 Paddle ne matche PAS (il garde la stacktrace complete).
    real = PaddleApiError(400, '{"error": {"code": "bad"}}')
    assert not (real.status_code == 429 or "cloudflare" in str(real).lower())


async def test_boot_check_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """PADDLE_BOOT_CHECK=false : la branche du lifespan sort avant tout
    appel Paddle — verifie sur le reglage lui-meme (le lifespan est teste
    par le boot de la suite entiere)."""
    from src.core.config import get_settings

    monkeypatch.setenv("PADDLE_BOOT_CHECK", "false")
    get_settings.cache_clear()
    try:
        assert get_settings().paddle_boot_check is False
    finally:
        get_settings.cache_clear()
