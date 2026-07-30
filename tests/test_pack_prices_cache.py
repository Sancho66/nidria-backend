"""Cache TTL des prix de packs (extension 30/07) — unitaire, zéro réseau.

Mécanisme retenu (rapport) : TTL process-local 1 h, best-effort — un échec
Paddle sert le cache PÉRIMÉ s'il existe, sinon des montants absents."""

import pytest

from src.core.config import get_settings
from src.signatures import pack_prices as mod

pytestmark = pytest.mark.usefixtures("rbac_baseline")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(mod, "_cache", {})
    monkeypatch.setattr(mod, "_fetched_at", 0.0)
    # Sortir du mode mock POUR CE MODULE seulement : la clé est fausse, le
    # client Paddle est monkeypatché — toujours zéro réseau.
    monkeypatch.setenv("MOCK_SERVICES", "false")
    monkeypatch.setenv("PADDLE_API_KEY", "test-cache-key")
    monkeypatch.setenv("PADDLE_ENV", "sandbox")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_client(calls: list, rows: list[dict] | Exception):
    class FakeClient:
        def __init__(self) -> None: ...

        async def list_prices(self, price_ids: list[str]) -> list[dict]:
            calls.append(list(price_ids))
            if isinstance(rows, Exception):
                raise rows
            return rows

    return FakeClient


async def test_ttl_one_fetch_then_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    rows = [
        {"id": "pri_a", "unit_price": {"amount": "1500", "currency_code": "EUR"}},
        {"id": "pri_b", "unit_price": {"amount": "2400", "currency_code": "EUR"}},
    ]
    import src.billing.paddle_client as pc

    monkeypatch.setattr(pc, "PaddleClient", _fake_client(calls, rows))
    out = await mod.pack_prices(["pri_a", "pri_b"])
    assert out == {"pri_a": (1500, "EUR"), "pri_b": (2400, "EUR")}
    out = await mod.pack_prices(["pri_a", "pri_b"])
    assert out == {"pri_a": (1500, "EUR"), "pri_b": (2400, "EUR")}
    assert len(calls) == 1  # le second passage sert le cache, zéro appel

    # TTL expiré → re-fetch.
    monkeypatch.setattr(mod, "_fetched_at", -mod.TTL_SECONDS * 2)
    await mod.pack_prices(["pri_a", "pri_b"])
    assert len(calls) == 2


async def test_failure_serves_stale_then_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    rows = [{"id": "pri_a", "unit_price": {"amount": "1500", "currency_code": "EUR"}}]
    import src.billing.paddle_client as pc

    monkeypatch.setattr(pc, "PaddleClient", _fake_client(calls, rows))
    assert await mod.pack_prices(["pri_a"]) == {"pri_a": (1500, "EUR")}
    # Paddle tombe + TTL expiré : le PÉRIMÉ bat l'absent.
    monkeypatch.setattr(pc, "PaddleClient", _fake_client(calls, RuntimeError("down")))
    monkeypatch.setattr(mod, "_fetched_at", -mod.TTL_SECONDS * 2)
    assert await mod.pack_prices(["pri_a"]) == {"pri_a": (1500, "EUR")}
    # Jamais eu de cache : montants absents, jamais une exception.
    monkeypatch.setattr(mod, "_cache", {})
    assert await mod.pack_prices(["pri_a"]) == {}


async def test_mock_mode_never_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOCK_SERVICES", "true")
    get_settings.cache_clear()
    calls: list = []
    import src.billing.paddle_client as pc

    monkeypatch.setattr(pc, "PaddleClient", _fake_client(calls, []))
    assert await mod.pack_prices(["pri_a"]) == {}
    assert calls == []
