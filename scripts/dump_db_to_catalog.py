"""Dump every cached PMS response from Postgres into the on-disk catalog.

Production has been calling AI for ~3 months and storing each result in the
`pms_cache` table. That table holds the AI-generated body for every class
that's ever been generated — which is exactly what we want in the on-disk
catalog. This script transfers them in one shot:

  pms_cache (Postgres)  ──►  app/data/classes/{class_code}.json

After running, every class that's been generated at least once is served
from disk (no AI call). For classes that have NEVER been generated,
either run them once via the UI (they'll be cached and appear here on
the next dump) or run scripts/generate_class_catalog.py.

Usage
-----
  # Dump everything in pms_cache:
  python -m scripts.dump_db_to_catalog

  # Overwrite existing catalog files (default: skip if file exists):
  python -m scripts.dump_db_to_catalog --overwrite

  # Just show what would be dumped:
  python -m scripts.dump_db_to_catalog --dry-run

The script
  • Reads `DATABASE_URL` from the same env vars production uses
    (see app/config.py).
  • Writes atomically (temp file → rename).
  • Skips rows whose class code isn't in `class_catalog_spec.json`
    (probably stale / one-off custom classes — log and ignore).
  • Strips the `version` field so a hand-edit later doesn't carry
    a misleading version number.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import class_catalog, db_service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("dump_db_to_catalog")

_OUT_DIR = Path(__file__).resolve().parents[1] / "app" / "data" / "classes"


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Fields stamped onto the PMSResponse by the post-AI build pipeline that
# we DON'T want frozen into the catalog — they're either dynamic per
# request (version, generation_mode) or rebuilt at serve time (P-T table,
# branch_charts, hydrotest_pressure). Stripping them keeps the catalog
# files small and lets the serve path re-derive them from authoritative
# sources every time.
_DYNAMIC_FIELDS = {
    "version",          # bumped per-regenerate; meaningless for a static catalog
    "generation_mode",  # marker for whether this came from AI/derivation/catalogue
}


def _scrub_for_catalog(response: dict) -> dict:
    """Return a copy of `response` with dynamic fields removed."""
    return {k: v for k, v in response.items() if k not in _DYNAMIC_FIELDS}


async def _fetch_all() -> list[tuple[str, dict]]:
    """Read every (piping_class, response_json) pair from pms_cache."""
    if not db_service.is_available():
        # Try to initialise the pool if it isn't already.
        await db_service.init_pool()
    if not db_service._pool:
        raise SystemExit(
            "Postgres pool is not available. Set DATABASE_URL and ensure "
            "the database is reachable, or run scripts/generate_class_catalog.py "
            "instead (calls AI directly, ~2 hours)."
        )
    rows = []
    async with db_service._pool.acquire() as conn:
        records = await conn.fetch(
            "SELECT piping_class, response_json FROM pms_cache "
            "ORDER BY piping_class"
        )
        for r in records:
            data = r["response_json"]
            if isinstance(data, str):
                data = json.loads(data)
            rows.append((r["piping_class"], data))
    return rows


async def _run(overwrite: bool, dry_run: bool) -> int:
    rows = await _fetch_all()
    logger.info("Found %d cached PMS rows in Postgres.", len(rows))

    spec_codes = set(class_catalog.all_class_codes())
    written = 0
    skipped_existing = 0
    skipped_unknown = 0

    for code, response in rows:
        code_upper = code.upper().strip()

        if code_upper not in spec_codes:
            logger.info(
                "skip %-8s — not in class_catalog_spec.json (custom / stale)",
                code_upper,
            )
            skipped_unknown += 1
            continue

        out_path = _OUT_DIR / f"{code_upper}.json"
        if out_path.exists() and not overwrite:
            logger.info("skip %-8s — already on disk (use --overwrite to replace)", code_upper)
            skipped_existing += 1
            continue

        if dry_run:
            logger.info("dry  %-8s — would write %s (%d bytes)",
                        code_upper, out_path.name, len(json.dumps(response)))
            continue

        scrubbed = _scrub_for_catalog(response)
        _atomic_write_json(out_path, scrubbed)
        logger.info("OK   %-8s — wrote %s", code_upper, out_path.name)
        written += 1

    materialised = len(class_catalog.materialised_class_codes())
    missing = class_catalog.missing_class_codes()

    print()
    print(f"Summary:")
    print(f"  Read from Postgres:   {len(rows)}")
    print(f"  Wrote to catalog:     {written}")
    print(f"  Skipped (existing):   {skipped_existing}")
    print(f"  Skipped (unknown):    {skipped_unknown}")
    print(f"  Catalog now has:      {materialised} of {len(spec_codes)} classes")
    if missing:
        print(f"  Still missing:        {', '.join(missing[:10])}"
              + (f" ... +{len(missing) - 10} more" if len(missing) > 10 else ""))
        print(f"")
        print(f"To materialise the missing classes, either:")
        print(f"  • run them once via the UI (they'll cache, then re-run this script), or")
        print(f"  • run `python -m scripts.generate_class_catalog` (calls AI for all missing).")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--overwrite", action="store_true",
                   help="Replace existing catalog files. Default: skip them.")
    p.add_argument("--dry-run", action="store_true",
                   help="Report what would be dumped without writing.")
    args = p.parse_args()
    return asyncio.run(_run(args.overwrite, args.dry_run))


if __name__ == "__main__":
    sys.exit(main())
