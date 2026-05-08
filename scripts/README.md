# Scripts

## `generate_class_catalog.py` — one-time AI batch (eliminates per-request AI calls)

This script materialises every class listed in
`app/data/standards/class_catalog_spec.json` (91 entries) to disk under
`app/data/classes/{class_code}.json`. After it completes, production PMS
generation reads from these files instead of calling the Anthropic API
on every request.

### How the catalog flow works

```
  Browser → /api/generate-pms
            │
            ▼
  pms_service.generate_pms()
    │
    ├─ L1 (in-memory cache) hit?  → return
    │
    ├─ L2 (Postgres cache) hit?   → return
    │
    ├─ class_catalog.lookup_pms(code)?  ← THIS is the disk catalog
    │      │
    │      ├─ found  → use it as ai_data, run post-processor, return
    │      │
    │      └─ missing → fall back to live AI (slow, with a log line
    │                   telling you to run this script for the class)
```

Once every class in the spec is materialised, the AI fallback is dead
code in production. You can remove it entirely if you want a hard error
on uncatalogued classes — see "Optional: hard-fail mode" below.

### Running the batch

#### Prerequisites

```sh
export ANTHROPIC_API_KEY=sk-ant-...
```

The key needs the same model access as production — the script uses
`settings.anthropic_model`.

#### Standard run (generate everything missing)

```sh
cd /path/to/pms-generator
python -m scripts.generate_class_catalog
```

The script is **idempotent** — it skips classes that already have a JSON
file and only generates the missing ones. So a partial run + retry is
safe.

Expected runtime: **~80 calls × ~90s each ≈ 2 hours.** Plus a 2s delay
between calls (configurable via `--delay-seconds`) to stay under the
RPM cap.

#### Targeted runs

```sh
# Just one class (e.g. you added a new code to the spec):
python -m scripts.generate_class_catalog --only G1N

# A few classes:
python -m scripts.generate_class_catalog --only F1N,G1N,G2LN

# Force regeneration of everything (e.g. AI prompt rules changed):
python -m scripts.generate_class_catalog --all

# Dry-run (lists what would be generated, no API calls):
python -m scripts.generate_class_catalog --dry-run
```

#### Verifying

```sh
ls app/data/classes/*.json | wc -l    # should be 91 after a full run
```

Or programmatically:

```python
from app.services import class_catalog
print(f"materialised: {len(class_catalog.materialised_class_codes())}")
print(f"missing:      {class_catalog.missing_class_codes()}")
```

### Adding a new class

1. Append the entry to `app/data/standards/class_catalog_spec.json`
   (rating, material, CA, service per the §5.5 convention).
2. `python -m scripts.generate_class_catalog --only NEWCODE`
3. Commit `app/data/classes/NEWCODE.json` to the repo.

### Updating a class (AI prompt rules changed)

The catalog stores the AI's output, so when the prompt rules change,
existing files are stale.

```sh
# Regenerate one class:
python -m scripts.generate_class_catalog --only A1 --all

# Or remove the file and re-run normally:
rm app/data/classes/A1.json
python -m scripts.generate_class_catalog
```

### Optional: hard-fail mode

The default behavior in `pms_service._generate_from_ai()` falls back to
the live AI call when a class isn't catalogued. To switch to hard-fail
(make the AI dependency fully optional / removable), find this block in
`app/services/pms_service.py`:

```python
cached_ai_data = class_catalog.lookup_pms(req.piping_class)
if cached_ai_data is not None:
    ai_data = cached_ai_data
else:
    logger.info(...)
    try:
        ai_data = await generate_pms_with_ai(...)
    except AIGenerationError as e:
        ...
```

…and replace the `else:` arm with:

```python
else:
    raise RuntimeError(
        f"Class '{req.piping_class}' is not in the on-disk catalog. "
        f"Run `python -m scripts.generate_class_catalog --only "
        f"{req.piping_class}` to materialise it."
    )
```

Once all classes are materialised and you've switched to hard-fail mode,
you can:
- Remove the `from app.services.ai_service import …` imports
- Delete `app/services/ai_service.py` entirely
- Remove the `anthropic` package from requirements.txt
- Remove `ANTHROPIC_API_KEY` from production env

### Failure handling

If a class fails (rate limit, malformed JSON, anthropic outage), the
script logs the error, leaves that class unwritten, and continues with
the rest. Re-run when the issue is resolved; only the missing ones get
retried.

The atomic write pattern (`tempfile + rename`) guarantees you'll never
end up with a half-written file. A `Ctrl+C` at any point leaves the
catalog consistent.
