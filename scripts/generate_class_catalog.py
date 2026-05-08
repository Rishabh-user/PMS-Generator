"""One-time AI batch script — materialise every class in
`class_catalog_spec.json` to disk under `app/data/classes/`.

Run this exactly once after the project moves from the per-request AI
flow to the on-disk catalog. After it completes (~80 calls × 1.5 min ≈
2 hours, depending on model latency and API tier), production lookups
go through `class_catalog.lookup_pms()` and never call the AI again.

Usage
-----
  # Generate everything that's missing (idempotent — skips existing files):
  python -m scripts.generate_class_catalog

  # Force regeneration of every class (e.g. after AI prompt rules change):
  python -m scripts.generate_class_catalog --all

  # Generate only specific classes:
  python -m scripts.generate_class_catalog --only A1,F1N,G1N

  # Dry-run (just report what would be generated):
  python -m scripts.generate_class_catalog --dry-run

The script
  • Honours rate-limiting via a configurable per-call delay
    (default 2 s — keeps you well under tier-1 RPM caps).
  • Writes each result atomically (temp file → rename) so a Ctrl+C
    leaves the catalog consistent.
  • Logs progress (N/91, ETA) to stderr.
  • Is idempotent — re-running picks up where it left off.

Failure handling
----------------
If a class fails (rate limit, malformed JSON, anthropic outage), the
script logs the error, leaves that class unwritten, and continues. Run
again when the issue is resolved; it'll only retry the missing ones.

ANTHROPIC_API_KEY must be set (same as production).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

# Allow `python -m scripts.generate_class_catalog` from project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import class_catalog
from app.services.ai_service import generate_pms_with_ai, AIGenerationError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("generate_class_catalog")

_OUT_DIR = Path(__file__).resolve().parents[1] / "app" / "data" / "classes"


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write `data` to `path` atomically — temp file in same dir, then
    rename. A Ctrl+C mid-write leaves either the old file or nothing,
    never a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)  # atomic on POSIX and Windows
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def _generate_one(spec: class_catalog.ClassSpec, delay_s: float) -> bool:
    """Call AI for one class, write result to disk. Returns True on
    success, False on failure (logged, not raised — keeps the batch
    going for the other 90 classes)."""
    code = spec["code"]
    out_path = _OUT_DIR / f"{code}.json"

    t0 = time.monotonic()
    try:
        ai_data = await generate_pms_with_ai(
            piping_class=code,
            material=spec["material"],
            corrosion_allowance=spec["corrosion_allowance"],
            service=spec["service"],
            rating=spec["rating"],
            reference_entries=[],
        )
    except AIGenerationError as e:
        logger.error("FAIL %s — AI: %s", code, e)
        return False
    except Exception as e:  # noqa: BLE001
        logger.error("FAIL %s — unexpected: %s", code, e, exc_info=True)
        return False

    if not isinstance(ai_data, dict) or not ai_data:
        logger.error("FAIL %s — AI returned empty / non-dict", code)
        return False

    _atomic_write_json(out_path, ai_data)
    elapsed = time.monotonic() - t0
    logger.info("OK   %s — %.1fs (wrote %s)", code, elapsed, out_path.name)

    if delay_s > 0:
        await asyncio.sleep(delay_s)
    return True


async def _run(targets: list[class_catalog.ClassSpec], delay_s: float, dry_run: bool) -> int:
    if dry_run:
        for spec in targets:
            print(f"  would generate: {spec['code']:<8} "
                  f"({spec['rating']}, {spec['material']}, "
                  f"{spec['corrosion_allowance']}, {spec['service']})")
        return 0

    n = len(targets)
    if n == 0:
        logger.info("Nothing to do — every class is already in the catalog.")
        return 0

    eta_min = (n * 90) / 60  # rough: 90s per class average
    logger.info("Starting batch: %d classes (~%.0f min total).", n, eta_min)
    t0 = time.monotonic()
    ok = 0
    for i, spec in enumerate(targets, 1):
        logger.info("--- [%d/%d] %s ---", i, n, spec["code"])
        if await _generate_one(spec, delay_s):
            ok += 1

    elapsed = time.monotonic() - t0
    logger.info(
        "Batch done — %d/%d succeeded in %.1f min (avg %.1fs/class).",
        ok, n, elapsed / 60, elapsed / n,
    )
    return 0 if ok == n else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--all", action="store_true",
        help="Regenerate every class even if its file already exists. Use "
             "after the AI prompt rules change. Default: only generate missing.",
    )
    parser.add_argument(
        "--only",
        help="Comma-separated class codes to generate (overrides --all). "
             "Useful when adding a single new class or fixing one.",
    )
    parser.add_argument(
        "--delay-seconds", type=float, default=2.0,
        help="Sleep between successful calls to stay under RPM caps. Default: 2.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List what would be generated without calling the API.",
    )
    args = parser.parse_args()

    spec_index = {c: class_catalog.get_spec(c) for c in class_catalog.all_class_codes()}
    if not spec_index:
        logger.error("class_catalog_spec.json is empty or missing.")
        return 2

    if args.only:
        only = [c.strip().upper() for c in args.only.split(",") if c.strip()]
        unknown = [c for c in only if c not in spec_index]
        if unknown:
            logger.error("Unknown class code(s): %s. Add them to "
                         "class_catalog_spec.json first.", unknown)
            return 2
        targets = [spec_index[c] for c in only]
    elif args.all:
        targets = [spec_index[c] for c in class_catalog.all_class_codes()]
    else:
        targets = [spec_index[c] for c in class_catalog.missing_class_codes()]

    return asyncio.run(_run(targets, args.delay_seconds, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
