# TODO — purchase-pipeline

Tasks are ordered: **task 0 must come first**, the rest are independent of each
other. Every task assumes the project conventions in `~/.claude-personal/CLAUDE.md`:
write the failing test first, then implement; type-annotate public APIs; update
docs and CHANGELOG; commit with `git-ai-commit`; don't push without being asked.

Origin: all of these were found while processing the 2026-07-24 Sozopol shopping
trip (Billa Sozopol, a beach kiosk, and a fish shop). Where a task has a concrete
reproducer from that trip, it is recorded — those are real regression cases, not
hypotheticals.

---

## 0. Migrate the purchasing code out of inventory-md

**Do this before tasks 3 and 4**, which create new files that would otherwise
land in the wrong repository and have to be moved again with their tests.

Move from `inventory-md/scripts/` to this project:

- `shop_import.py`, `pipeline.py`, `shopping_context.py`
- `ledger.py`
- `tingbok_push.py`, `off_upload.py`, `openprices_publish.py`, `op_auth.py`
- `check_grocery_ledger` (currently `~/bin/check-grocery-ledger`)
- the staging YAML schema and its documentation

Leave in `inventory-md`: `extract_barcodes.py`, `bb_dates.py`, `check_quality.py`,
`inventory_import.py`(?), the vocabulary system, the web UI. Identifying a
physical object is inventory's business; deciding what a purchase *means* is not.

`inventory_import.py` is the genuine judgement call — it reads a staging file
(this project's schema) and writes `inventory.md` (that project's format). Prefer
moving it here and having it call `inventory-md add`, so the schema and its only
consumer stay together.

Set up packaging as part of this task (pyproject, ruff, tests, CI) — the
repository was deliberately created bare so that whoever does the migration picks
a layout that fits what actually lands.

Afterwards, update:

- `~/inventory-md/claude-skills/process-shopping.md` (the generic guide)
- `~/.claude-personal/skills/process-shopping/` (the personal skill)
- `~/solveig-inventory/.claude/settings.local.json` (the command allowlist paths)

---

## 1. `match_shop_osm` must refuse an inexact shop key

**Bug, with a live reproducer.** `shopping_context.py "Billa"` returned
`WAY:1016681733` — the *Varna* branch — for a trip to the **Sozopol** branch.
The documentation claims the matcher "refuses to silently pick among multiple
matches", but with only one Billa cached there is nothing to disambiguate, so it
returns that one, confidently and wrongly. The cache is branch-keyed
(`Billa Varna ул. Андрей Сахаров`); a bare chain name must never resolve.

Fix: require an exact cache-key match. On a partial match, return nothing and
print the candidate branch keys. This closes the bug on its own, without any
receipt parsing.

Optional follow-up: derive the branch key from the receipt. Note that this is
**not** as trivial as it looks — the 2026-07-24 Billa receipt carries two
different addresses, the company's registered address in the header
(`СОЗОПОЛ УЛ. ИНДУСТРИАЛНА 3`) and the actual store address in the card-terminal
footer (`УЛ. РЕПУБЛИКАНСКА 5`). OSM matches the footer. Which address to trust is
a per-chain rule, so it belongs in the receipt-format registry (task 2).

---

## 2. Receipt-format registry, and a mandatory total reconciliation

Two related pieces.

**(a) `receipt-formats.json`**, keyed by chain, recording the layout quirks that
must be known to transcribe a photographed receipt correctly:

- which address line identifies the branch (see task 1)
- whether the `N x unit_price` multiplier line **precedes** or **follows** the
  item it belongs to
- how discounts appear (own line, sign, whether they are already netted)
- deposit/returnable-container lines
- dual-currency totals and the exchange-rate line

Reproducer: Billa prints the multiplier on the line **above** its item. On the
2026-07-24 receipt, `3 x 0.71` sits under `BILLA ПОП КЪРПИ 5Б` but belongs to the
`ШУМЕНСКО` line below it. The naive reading assigns three beers' price to a pack
of cleaning cloths.

**(b) A hard reconciliation check.** Any hand-transcribed receipt must sum to the
printed total before the staging file is accepted; a mismatch is an error, not a
warning. On 2026-07-24 this is the only thing that caught (a) — the line items
summed to 18.12 exactly under the correct reading and not under the naive one.

---

## 3. `osm_resolve.py` — find an existing shop's OSM object

Read-only, no auth, cannot damage anything. Build this before task 4.

```
osm_resolve.py --lat 42.41934 --lon 27.69215 --name "Billa" [--radius 50]
```

Queries **Overpass** for POIs within `--radius` metres (configurable, default
~50 m), ranks candidates by name similarity, prints each with an
`openstreetmap.org` link for eyeballing, and on confirmation writes the pick into
`~/.config/inventory-md/shop-osm.json` under a branch-specific key.

Motivation: on 2026-07-24 the actual friction was not mapping a shop, it was
*finding the node id of a shop that already existed*. That took four hand-rolled
Nominatim round-trips. Nominatim is also the wrong tool — it is a geocoder, and
reverse-geocoding the fish shop's coordinates returned a neighbouring wine shop.
Overpass with a radius is the right query.

---

## 4. `osm_add_shop.py` — create a surveyed shop node

Depends on task 3 (reuses its duplicate query) and on a one-time
`osm_auth.py` (OAuth 2.0, mirroring `op_auth.py`). The `osmapi` package handles
the changeset dance.

```
osm_add_shop.py --lat 42.41934 --lon 27.69215 --name "Sozopol Fish" \
                --shop seafood [--tag k=v]… [--commit]
```

Three constraints are **requirements, not nice-to-haves**:

- **Duplicate check is mandatory.** Query Overpass for anything similar within
  ~30 m and refuse rather than create. Duplicate POIs are the main way
  well-intentioned scripts damage OpenStreetMap.
- **Never generate the data.** Coordinates come from the user's survey or GPS;
  the name comes off the shopfront. The tool formats tags; it must not invent a
  shop, and neither must an AI agent driving it.
- **Honest changesets:** `source=survey`, `created_by=purchase-pipeline/osm_add_shop`.
  This stays within the [Automated Edits code of conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct)
  because it is a human survey with a scripted upload. That stops being true the
  moment it is pointed at a batch — so don't add a batch mode.

Pending real-world case: Sozopol Fish (магазин за риба) at
`42.41934022085787, 27.692148284820842`, unmapped, which is why the 2026-07-24
mussel price could not be published to Open Prices.

---

## 5. `pipeline.py` batch mode

```
pipeline.py A.yaml B.yaml C.yaml --commit
```

Run the commit stages for several staging files, validating **once** at the end
rather than per file. On 2026-07-24 there were three shops in one day, so
`inventory-md parse` + `check_quality.py` ran three times at roughly two minutes
each — long enough that each invocation had to be backgrounded, and long enough
that the runs had to be serialised by hand to avoid racing on `inventory.json`.

Alternative or addition: `--no-validate`, so a caller can skip validation on all
but the last file.
