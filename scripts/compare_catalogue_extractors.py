#!/usr/bin/env python3
"""Compare legacy PDF parser vs OpenRouter extraction for catalogue PDFs.

Usage:
  python scripts/compare_catalogue_extractors.py "FINAL Catalogue_Upload_v2.pdf"
  OPENROUTER_ENABLED=true OPENROUTER_API_KEY=... OPENROUTER_MODEL=... \\
    python scripts/compare_catalogue_extractors.py "Draft 2026 Agility Nationals Catalogue.pdf"
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load .env when present (local validation runs).
_env_file = ROOT / ".env"
if _env_file.is_file():
    for line in _env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split("#", 1)[0].strip()
        if key and key not in os.environ:
            os.environ[key] = value

from app.scraper.catalogue import parse_catalogue_pdf, parse_catalogue_pdf_bytes
from app.scraper.openrouter_catalogue import is_openrouter_enabled


def _summarize(entries: list[dict]) -> dict:
    heights = Counter(e["height_group"] for e in entries)
    days = Counter(e["day"] for e in entries)
    events = sorted({e["event_name"] for e in entries})
    null_dogs = sum(1 for e in entries if not e.get("dog_name"))
    return {
        "count": len(entries),
        "days": dict(sorted(days.items())),
        "heights": dict(sorted(heights.items())),
        "event_count": len(events),
        "events_sample": events[:10],
        "null_dog_name": null_dogs,
        "nfc_count": sum(1 for e in entries if e.get("nfc")),
    }


def _compare_keys(legacy: list[dict], openrouter: list[dict]) -> dict:
    legacy_keys = {
        (e["day"], e["event_name"], e["height_group"], e["cat_number"], e.get("ring_number"))
        for e in legacy
    }
    openrouter_keys = {
        (e["day"], e["event_name"], e["height_group"], e["cat_number"], e.get("ring_number"))
        for e in openrouter
    }
    return {
        "legacy_only": len(legacy_keys - openrouter_keys),
        "openrouter_only": len(openrouter_keys - legacy_keys),
        "shared": len(legacy_keys & openrouter_keys),
    }


async def _run(pdf_path: Path, use_openrouter: bool) -> int:
    data = pdf_path.read_bytes()
    print(f"PDF: {pdf_path.name} ({len(data):,} bytes)")

    legacy = parse_catalogue_pdf(data)
    legacy_summary = _summarize(legacy)
    print("\nLegacy parser:")
    print(json.dumps(legacy_summary, indent=2))

    if not use_openrouter:
        print("\nOpenRouter: disabled (set OPENROUTER_ENABLED=true and OPENROUTER_API_KEY)")
        return 0

    if not is_openrouter_enabled():
        print("\nOpenRouter: enabled flag set but API key/model missing")
        return 1

    try:
        from app.scraper.openrouter_catalogue import extract_catalogue_from_pdf

        openrouter = await extract_catalogue_from_pdf(
            data,
            filename=pdf_path.name,
            trial_external_id="validation",
        )
    except Exception as exc:
        print(f"\nOpenRouter extraction failed: {exc}")
        return 1

    openrouter_summary = _summarize(openrouter)
    print("\nOpenRouter:")
    print(json.dumps(openrouter_summary, indent=2))

    diff = _compare_keys(legacy, openrouter)
    print("\nKey comparison (day, event, height, cat#, ring):")
    print(json.dumps(diff, indent=2))

    count_delta = abs(legacy_summary["count"] - openrouter_summary["count"])
    if count_delta > max(5, legacy_summary["count"] * 0.02):
        print(f"\nWARNING: entry count delta {count_delta} exceeds tolerance")
        return 1

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="Path to catalogue PDF")
    parser.add_argument(
        "--openrouter",
        action="store_true",
        help="Also run OpenRouter extraction (requires env vars)",
    )
    args = parser.parse_args()

    if not args.pdf.is_file():
        raise SystemExit(f"File not found: {args.pdf}")

    raise SystemExit(asyncio.run(_run(args.pdf, args.openrouter)))


if __name__ == "__main__":
    main()
