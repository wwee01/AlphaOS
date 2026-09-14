"""Explicit manual capture/offline reconciliation; no journal or service writes.

Usage: python -m alphaos.reports.common_valuation_capture --input bundle.json
       --output result.json
Broker GETs require the separate --collect flag. Output is a new private file.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from alphaos.reports.common_valuation import collect_common_valuation, reconcile_common_valuation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--collect", action="store_true", help="Perform bounded GET-only paper/data capture")
    mode.add_argument("--input", type=Path, help="Reconcile an existing raw bundle or capture result offline")
    parser.add_argument("--output", required=True, type=Path, help="New private evidence file; existing files are refused")
    args = parser.parse_args(argv)
    if args.output.exists() or not args.output.parent.is_dir():
        parser.error("output must be a new file in an existing directory")
    try:
        if args.collect:
            from alphaos.config.settings import load_settings
            from alphaos.reports.common_valuation_source import CommonValuationSource
            result = collect_common_valuation(CommonValuationSource(load_settings()))
        else:
            with args.input.open() as handle:
                value = json.load(handle)
            result = reconcile_common_valuation(value.get("raw_evidence", value))
    except Exception as exc:
        # Keep provider errors honest without recording credential-bearing
        # URLs, response bodies, environment values or account identifiers.
        result = {"status": "capture_failed", "comparison_eligible": False,
                  "error_type": type(exc).__name__, "reason": "capture incomplete; no valuation or return certified"}
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(result, handle, sort_keys=True, indent=2, allow_nan=False)
        handle.write("\n")
    print(f"{result['status']}; strict comparison unavailable; evidence saved to {args.output}")
    return 0 if result["status"] == "reconstructed_candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
