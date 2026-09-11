"""Fetch a bounded historical IEX batch with read-only GETs and a private archive."""

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from atlas.research.data_adapters import (
    AlpacaHistoricalAdapter, DataAdapterError, HistoricalBarsRequest,
    archive_historical_bundle, parse_utc, provider_capabilities,
)
from atlas.research.imports import _external_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("alpaca", "databento", "massive"), default="alpaca")
    parser.add_argument("--capabilities", action="store_true", help="show adapter readiness without files or network")
    parser.add_argument("--key-file", help="external owner-only 0600 JSON with api_key and api_secret")
    parser.add_argument("--symbols", nargs="+", help="explicit stock/ETF tickers; GLD/USO are proxies, not futures")
    parser.add_argument("--start", help="inclusive UTC or timezone-aware RFC3339 start")
    parser.add_argument("--end", help="exclusive UTC or timezone-aware RFC3339 end")
    parser.add_argument("--interval-seconds", type=int, default=300, choices=(60, 300, 900, 1800))
    parser.add_argument("--feed", choices=("iex",), default="iex")
    parser.add_argument("--adjustment", choices=("raw",), default="raw")
    parser.add_argument("--max-pages", type=int, default=10)
    parser.add_argument("--page-limit", type=int, default=1000)
    parser.add_argument("--output-dir", help="new external private archive directory; parent must exist")
    args = parser.parse_args(argv)
    try:
        if args.capabilities:
            print(json.dumps(asdict(provider_capabilities(args.provider)), sort_keys=True))
            return 0
        if args.provider != "alpaca":
            raise DataAdapterError("provider_unavailable_missing_credentials_and_verified_transport_contract")
        if not all((args.key_file, args.symbols, args.start, args.end, args.output_dir)):
            raise DataAdapterError("key_file_symbols_start_end_output_dir_required")
        destination = _external_path(args.output_dir)
        if destination.exists() or not destination.parent.is_dir():
            raise DataAdapterError("historical_archive_requires_new_external_destination")
        request = HistoricalBarsRequest(provider=args.provider, asset_class="equity", symbols=tuple(args.symbols),
                                        start=parse_utc(args.start), end=parse_utc(args.end),
                                        interval_seconds=args.interval_seconds, feed=args.feed,
                                        adjustment=args.adjustment, max_pages=args.max_pages, page_limit=args.page_limit)
        bundle = AlpacaHistoricalAdapter(args.key_file).fetch_bars(request)
        report = archive_historical_bundle(bundle, destination)
        print(json.dumps({**report, "request": request.to_dict(), "quality": bundle.quality,
                          "requested_window_coverage": bundle.receipt["requested_window_coverage"],
                          "current_asset_metadata": bundle.receipt["assets_current_only"]}, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, DataAdapterError) else "historical_data_preflight_failed"
        print(json.dumps({"error": code, "execution_enabled": False, "orders_submitted": 0,
                          "account_mutations": 0, "alpha_validation_eligible": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
