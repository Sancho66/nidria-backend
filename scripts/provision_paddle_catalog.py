"""Provision the Paddle catalog from the DECLARED truth (src/billing/catalog.py).

Idempotent, matching by stable key (custom_data), DRY-RUN BY DEFAULT (same
convention as cleanup_orphan_requirements): pass --execute to write. A second
execute run is a 100% no-op. Divergent existing prices are NEVER updated —
explicit error instead (price rotation is a human decision).

Sandbox and live use the SAME script and declaration; only PADDLE_ENV +
PADDLE_API_KEY change. The output prints the exact PADDLE_PRICE_IDS JSON to
paste into the env — the script never writes the env itself (the operator
reads, verifies, pastes)."""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.billing.catalog_provisioning import (  # noqa: E402
    align_tax_mode,
    provision_catalog,
    provision_webhook_destination,
)
from src.billing.paddle_client import PaddleClient  # noqa: E402
from src.core.config import get_settings  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually create the missing objects (default: dry-run, read-only).",
    )
    parser.add_argument(
        "--align-tax-mode",
        action="store_true",
        help=(
            "PATCH ONLY the tax_mode of divergent prices to the declared value "
            "(the one sanctioned update — the explicit human decision the "
            "no-update rule reserves). Lists every patched price, touches "
            "nothing else, then exits."
        ),
    )
    args = parser.parse_args()
    dry_run = not args.execute

    settings = get_settings()

    if args.align_tax_mode:
        print(f"Paddle env: {settings.paddle_env} | mode: ALIGN TAX_MODE (patches this field only)")
        patched = await align_tax_mode(client=PaddleClient())
        if not patched:
            print("  nothing to align: every matched price already carries the declared tax_mode.")
        for line in patched:
            print(f"  PATCHED {line}")
        return 0

    print(f"Paddle env: {settings.paddle_env} | mode: {'DRY-RUN' if dry_run else 'EXECUTE'}")
    report = await provision_catalog(dry_run=dry_run, client=PaddleClient())

    for key in report.unchanged_products:
        print(f"  product {key:24} present, conform — no-op")
    for key in report.created_products:
        print(f"  product {key:24} {'WOULD CREATE' if dry_run else 'CREATED'}")
    for key in report.unchanged_prices:
        print(f"  price   {key:24} present, conform — no-op ({report.price_ids[key]})")
    for key in report.created_prices:
        print(
            f"  price   {key:24} {'WOULD CREATE' if dry_run else 'CREATED'}"
            + ("" if dry_run else f" ({report.price_ids[key]})")
        )
    if report.divergences:
        print("\nDIVERGENCES — nothing was updated (price rotation is a human decision):")
        for divergence in report.divergences:
            print(f"  !! {divergence}")
        return 1
    if report.is_noop:
        print("\n100% no-op: Paddle matches the declaration.")
    if not dry_run or report.is_noop:
        print("\nPADDLE_PRICE_IDS to paste into the env:")
        print(json.dumps(report.price_ids, indent=2, sort_keys=True))

    # --- webhook destination (same philosophy; URL from the env only) ------------
    if settings.paddle_webhook_url is None:
        print("\nPADDLE_WEBHOOK_URL absent — destination webhook non geree (catalogue seul).")
        return 0
    destination = await provision_webhook_destination(
        dry_run=dry_run, client=PaddleClient(), url=settings.paddle_webhook_url
    )
    if destination.divergences:
        print("\nDESTINATION DIVERGENTE — rien n'a ete modifie (decision humaine) :")
        for divergence in destination.divergences:
            print(f"  !! {divergence}")
        return 1
    if destination.created and dry_run:
        print(f"\ndestination webhook: WOULD CREATE -> {settings.paddle_webhook_url}")
    elif destination.created:
        print(f"\ndestination webhook: CREATED ({destination.setting_id})")
        print("=" * 72)
        print("SECRET — a poser dans PADDLE_WEBHOOK_SECRET, ne sera PLUS JAMAIS affiche:")
        print(destination.secret)
        print("=" * 72)
    else:
        print(
            f"\ndestination webhook: existante, conforme ({destination.setting_id}) — "
            "secret deja en ta possession, jamais relu."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
