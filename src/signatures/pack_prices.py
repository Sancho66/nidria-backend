"""Prix des packs signatures, relus de Paddle — cache TTL en mémoire.

Choix du mécanisme (argumenté au rapport) : **TTL process-local, 1 h,
best-effort** — pas de montant en config (c'est précisément ce que
l'extension élimine), pas de lecture au boot seul (un prix changé chez
Paddle resterait faux jusqu'au redeploy, et le boot ne doit pas dépendre
du réseau). Un échec Paddle sert le cache PÉRIMÉ s'il existe, sinon des
montants absents — le front a déjà son fallback sans prix (cartes
honnêtes, le checkout les affiche). Mock/tests : jamais de réseau
(mock_services ou clé absente → vide), seam `override` pour les contrats.
"""

import logging
import time

from src.core.config import get_settings

logger = logging.getLogger(__name__)

TTL_SECONDS = 3600.0

# price_id → (unit_amount en centimes, currency_code)
_cache: dict[str, tuple[int, str]] = {}
_fetched_at: float = 0.0

# Seam de test (pattern provider.override) : dict price_id → (amount, currency).
override: dict[str, tuple[int, str]] | None = None


def _serve(price_ids: list[str]) -> dict[str, tuple[int, str]]:
    return {pid: _cache[pid] for pid in price_ids if pid in _cache}


async def pack_prices(price_ids: list[str]) -> dict[str, tuple[int, str]]:
    """Les montants des packs demandés — depuis le cache si frais, sinon
    relus de Paddle. Toujours best-effort : jamais une exception vers
    l'endpoint grille."""
    global _cache, _fetched_at
    if override is not None:
        return {pid: override[pid] for pid in price_ids if pid in override}
    settings = get_settings()
    if settings.mock_services or not settings.paddle_api_key:
        return {}
    now = time.monotonic()
    fresh = _cache and (now - _fetched_at) < TTL_SECONDS
    if fresh and all(pid in _cache for pid in price_ids):
        return _serve(price_ids)
    try:
        from src.billing.paddle_client import PaddleClient

        rows = await PaddleClient().list_prices(price_ids)
        refreshed: dict[str, tuple[int, str]] = {}
        for row in rows:
            unit = row.get("unit_price") or {}
            try:
                refreshed[str(row["id"])] = (
                    int(unit["amount"]),
                    str(unit["currency_code"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        _cache = refreshed
        _fetched_at = now
    except Exception:  # noqa: BLE001 — best-effort : le périmé bat l'absent
        logger.warning(
            "pack price refresh failed; serving %s",
            "stale cache" if _cache else "no amounts",
        )
    return _serve(price_ids)
