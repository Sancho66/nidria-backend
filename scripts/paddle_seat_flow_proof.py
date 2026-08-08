"""Prove the fallen seat ceiling against a REAL Paddle environment.

Replays EXACTLY the call the backend makes when a member gesture crosses
the included tier (BillingManager.sync_seat_quantity → PATCH subscription
items: base ×1 + seat ×N, prorated_immediately), with N beyond the OLD
plan caps — then reads the newest transaction (the prorated invoice) and
REVERTS the quantity (full_next_billing_period) to leave the environment
as found. Born during the 05/08 seat lot: the dev IP was Cloudflare-
blocked by Paddle, so the proof runs from CI (paddle-align workflow).

DRY-RUN BY DEFAULT (house convention): --execute to write.

  --list                       inventory the subscriptions (read-only)
  --subscription sub_... --plan cabinet --cycle mensuel \
      --quantity 3 [--revert-quantity 2] [--execute]
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.billing.paddle_client import PaddleClient  # noqa: E402
from src.core.config import get_settings  # noqa: E402


def _key_of(price_id: str | None, ids: dict[str, str]) -> str:
    return next((k for k, v in ids.items() if v == price_id), price_id or "?")


def _items(ids: dict[str, str], plan: str, cycle: str, seats: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"price_id": ids[f"{plan}_{cycle}"], "quantity": 1}]
    if seats > 0:
        out.append({"price_id": ids[f"seat_{plan}_{cycle}"], "quantity": seats})
    return out


async def list_subscriptions(client: PaddleClient, ids: dict[str, str]) -> None:
    subs = await client._request_page("/subscriptions?per_page=50")
    print(f"{len(subs)} subscription(s):")
    for sub in subs:
        items = ", ".join(
            f"{_key_of((it.get('price') or {}).get('id'), ids)}×{it.get('quantity')}"
            for it in sub.get("items") or []
        )
        sched = (sub.get("scheduled_change") or {}).get("action")
        print(
            f"  {sub['id']}  {sub['status']:10}"
            f"{(' (scheduled ' + sched + ')') if sched else '':24} | {items}"
        )


async def push_proof(
    client: PaddleClient,
    ids: dict[str, str],
    *,
    subscription: str,
    plan: str,
    cycle: str,
    quantity: int,
    revert_quantity: int | None,
    execute: bool,
) -> None:
    if not execute:
        print(f"DRY-RUN — would PATCH {subscription} with:")
        for item in _items(ids, plan, cycle, quantity):
            print(f"  {_key_of(item['price_id'], ids)} × {item['quantity']}")
        print("  proration_billing_mode=prorated_immediately (the backend's exact call)")
        if revert_quantity is not None:
            print(f"  then REVERT to seat×{revert_quantity} (full_next_billing_period)")
        return

    print(f"PUSH seat×{quantity} on {subscription} (prorated_immediately)…")
    updated = await client.update_subscription_items(
        subscription,
        items=_items(ids, plan, cycle, quantity),
        proration_billing_mode="prorated_immediately",
    )
    for it in updated.get("items") or []:
        print(f"  item {_key_of((it.get('price') or {}).get('id'), ids)} × {it.get('quantity')}")

    transactions = await client._request_page(
        f"/transactions?subscription_id={subscription}&per_page=1&order_by=created_at[DESC]"
    )
    for t in transactions:
        totals = (t.get("details") or {}).get("totals") or {}
        print(
            f"  newest transaction {t['id']} status={t['status']}: "
            f"subtotal {totals.get('subtotal')} tax {totals.get('tax')} "
            f"total {totals.get('total')} {totals.get('currency_code')}"
        )
        for li in ((t.get("details") or {}).get("line_items") or [])[:6]:
            print(
                f"    line ×{li.get('quantity')} "
                f"{((li.get('price') or {}).get('description') or '?')[:44]!r} "
                f"→ {(li.get('totals') or {}).get('total')}"
                f"{' (prorated)' if li.get('proration') else ''}"
            )

    if revert_quantity is not None:
        print(f"REVERT to seat×{revert_quantity} (full_next_billing_period)…")
        reverted = await client.update_subscription_items(
            subscription,
            items=_items(ids, plan, cycle, revert_quantity),
            proration_billing_mode="full_next_billing_period",
        )
        print(
            "  reverted items: "
            + ", ".join(
                f"{_key_of((it.get('price') or {}).get('id'), ids)}×{it.get('quantity')}"
                for it in reverted.get("items") or []
            )
        )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="Inventory only (read-only).")
    parser.add_argument("--subscription")
    parser.add_argument("--plan", choices=["cabinet", "agence"])
    parser.add_argument("--cycle", choices=["mensuel", "annuel"])
    parser.add_argument("--quantity", type=int)
    parser.add_argument("--revert-quantity", type=int, default=None)
    parser.add_argument("--execute", action="store_true", help="Actually write (default: dry-run).")
    args = parser.parse_args()

    settings = get_settings()
    print(f"Paddle env: {settings.paddle_env}")
    client = PaddleClient()
    # Stable key → price id, read from Paddle itself (custom_data) — the
    # script is self-sufficient, no PADDLE_PRICE_IDS env needed on a runner.
    ids: dict[str, str] = {
        key: p["id"]
        for p in await client.list_prices()
        if (key := (p.get("custom_data") or {}).get("stable_key"))
    }

    if args.list:
        await list_subscriptions(client, ids)
        return 0
    if not (args.subscription and args.plan and args.cycle and args.quantity is not None):
        parser.error("--subscription, --plan, --cycle and --quantity are required without --list")
    await push_proof(
        client,
        ids,
        subscription=args.subscription,
        plan=args.plan,
        cycle=args.cycle,
        quantity=args.quantity,
        revert_quantity=args.revert_quantity,
        execute=args.execute,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
