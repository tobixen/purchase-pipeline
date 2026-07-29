# TODO — purchase-pipeline

**Tasks 0, 1, 2 and 3 are done** (2026-07-25/29) — kept below with a `DONE` note
rather than deleted, because each records a real regression case worth keeping.
Tasks 4 and 5 remain, and task 4's motivating case has evaporated (see there).

Tasks were ordered: **task 0 first**, the rest independent of each other. Every task assumes the project conventions in `~/.claude-personal/CLAUDE.md`:
write the failing test first, then implement; type-annotate public APIs; update
docs and CHANGELOG; commit with `git-ai-commit`; don't push without being asked.

Origin: all of these were found while processing the 2026-07-24 Sozopol shopping
trip (Billa Sozopol, a beach kiosk, and a fish shop). Where a task has a concrete
reproducer from that trip, it is recorded — those are real regression cases, not
hypotheticals.

---

## 0. Migrate the purchasing code out of inventory-md — **DONE**

Done: the modules listed below live in `src/purchase_pipeline/`, each exposed as
a console script (see README); their tests moved with them. Packaging is
hatchling + hatch-vcs, ruff, pytest and a CI workflow that checks out
inventory-md first (it is a library dependency and not on PyPI).

Two supporting moves inside inventory-md were needed and are **not** scope creep
away from "leave it in inventory-md" — both files stayed there, they only became
importable/runnable by name instead of being loose scripts:

* `bb_dates.py` → `src/inventory_md/bb_dates.py`, so `shop_import` can import the
  same date parser `extract_barcodes.py` uses.
* `check_quality.py` → `src/inventory_md/check_quality.py` with a new
  `inventory-md-check-quality` console script, so `pipeline` can run the quality
  gate by name (like `inventory-md parse`) rather than hardcoding a path into a
  sibling checkout.

Also done (2026-07-28/29): the workflow guide moved here as
`claude-skills/process-shopping.md` — it is a manual for this project and all
but three of its commands are this project's; the item-adding reference it
duplicated now points at inventory-md's `docs/ADDING-ITEMS.md`. Both
`~/solveig-inventory/.claude/settings.json` (committed) and `settings.local.json`
now allowlist the console scripts. `~/bin/check-grocery-ledger` is deleted from
the dotfiles repo, the project being user-installed so `~/.local/bin` carries it.

Still open from this task:

* `~/.claude/skills/` is registered in `~/.claude` as a gitlink (mode 160000)
  with no `.gitmodules` and no repo inside, so the personal skill edits there are
  unversioned. Worth fixing before relying on them. **This is the only item left
  in this file that is not about code in this repository.**

Done from this task (2026-07-29):

* `check_grocery_ledger` no longer crashes on a directory `--diary`
  (`IsADirectoryError`) — it reads through `shopping_context.read_diary_text`, so
  `--diary` means the same thing in both commands. `main()` also takes `argv` now,
  which is what let the whole module get a test file instead of none.

---

## 0b. (historical) Original migration notes

**Do this before tasks 3 and 4**, which create new files that would otherwise
land in the wrong repository and have to be moved again with their tests.

Move from `inventory-md/scripts/` to this project:

- `shop_import.py`, `pipeline.py`, `shopping_context.py`
- `ledger.py`
- `tingbok_push.py`, `off_upload.py`, `openprices_publish.py`, `op_auth.py` (now the `openprices-auth` command)
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

## 1. `match_shop_osm` must refuse an inexact shop key — **DONE**

Done: resolution requires an exact (case-insensitive, whitespace-stripped) cache
key; a partial match lists the candidate branch keys instead, including when
there is only one — which is exactly the case the old code resolved silently.
The optional follow-up (derive the branch key from the receipt) is **not** done;
the per-chain address rule it needs is now recorded in `receipt-formats.json`.

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

## 2. Receipt-format registry, and a mandatory total reconciliation — **DONE**

Done: `src/purchase_pipeline/data/receipt-formats.json` + the `receipt-formats`
command for (a), and `staging.reconcile_total()` enforced from `require_flat()`
for (b). Only Billa and Lidl have entries — a chain is recorded only once its
receipt has actually been read, and each entry must carry a `source`.

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

## 3. `osm_resolve.py` — find an existing shop's OSM object — **DONE**

Done: `src/purchase_pipeline/osm_resolve.py`, the `osm-resolve` console script.
Overpass radius query, ranked by name similarity, printed with map links;
`--save-as KEY --pick TYPE:ID` records a human's confirmation and nothing else
writes. Verified against the live API on the 2026-07-24 Sozopol coordinates.

Three things came out differently from the sketch below, all worth knowing:

* **The cache moved into its own module.** `shop_osm.py` now owns the path, the
  reader and the writer; `shopping_context` and `openprices_publish` had a copy
  of the path each, and disagreed about `XDG_CONFIG_HOME`.
* **The write is guarded from the other end too.** `save_entry` refuses a bare
  chain key (`Billa`), because task 1 only hardened the *matcher*: a chain-only
  key in the cache matches exactly and so resolves silently, which is that bug
  reintroduced through the cache. It also refuses to repoint an existing key, and
  refuses a `--pick` the query never returned (a typo is not a confirmation).
* **Nominatim is gone rather than merely deprecated.** Leaving the wrong tool
  next to the right one is a trap. `openprices-publish --suggest-from-photo`
  became `--coords-from-photo`, which prints the EXIF GPS as an `osm-resolve`
  invocation and guesses no shop; `nominatim_reverse` and its
  `~/.cache/inventory-md/osm-geocode-cache.json` are deleted (the stale cache
  file can be removed by hand, nothing reads it).

Name ranking scores across `name`, `name:en`, `int_name`, `official_name`,
`alt_name`, `brand` and `operator`: the fish shop's `name` is Cyrillic
(`магазин за риба`) and its Latin form only ever appears in `name:en`, so scoring
`name` alone would have ranked the correct answer at ~0.

Unnamed POIs are ranked last but never filtered out — an unnamed `shop=seafood`
five metres away is precisely what task 4 must see. `amenity` values that cannot
be a shop (bench, waste basket, parking, …) *are* filtered, or a 50 m radius in a
town centre is mostly street furniture.

Original motivation, still accurate: on 2026-07-24 the friction was not mapping a
shop, it was *finding the node id of a shop that already existed*, and that took
four hand-rolled Nominatim round-trips.

---

## 4. `osm_add_shop.py` — create a surveyed shop node

Depends on task 3 (reuses its duplicate query) and on a one-time
`osm_auth.py` (OAuth 2.0, mirroring `op_auth.py` (now the `openprices-auth` command)). The `osmapi` package handles
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

**The motivating case is gone.** Running the new `osm-resolve` against those
coordinates on 2026-07-29 found Sozopol Fish already mapped, 18 m away:

```
osm-resolve --lat 42.41934022085787 --lon 27.692148284820842 --name "Sozopol Fish" --radius 60
  1. score 1.00  NODE:14048113335  18 m  Sozopol Fish  [shop=seafood]
  2. score 0.52   WAY:301280221    10 m  Sozopol marketplace  [amenity=marketplace]
  4. score 0.11  NODE:5002039921   20 m  Грив-56  [shop=wine]
```

Somebody mapped it between 2026-07-24 and now. (Candidate 4 is the wine shop that
Nominatim used to return for these coordinates — the reproducer for task 3's
"a geocoder answers the wrong question", now visible as a ranked candidate that
scores 0.11 instead of being *the* answer.)

So the 2026-07-24 mussel price can be published today with nothing more than:

```
osm-resolve --save-as "Sozopol Fish <street>" --pick NODE:14048113335
```

That removes the only concrete case this task had. Which raises the honest
question of whether to build it at all: the requirements below (mandatory
duplicate check, never generate data, honest changesets, no batch mode) are
sound, but writing an OSM upload tool needs OAuth setup, `osmapi`, and careful
review, and there is now **no** shop waiting for it. Recommendation: leave this
open and unbuilt until an actually-unmapped shop turns up. `osm-resolve` already
answers "is it mapped?" in one command, which is the question that was really
being asked on 2026-07-24 — and when a genuinely unmapped shop does appear, a
one-off edit in iD or Vespucci is likely cheaper than this tool.

---

## 5. `purchase-pipeline` batch mode

```
purchase-pipeline A.yaml B.yaml C.yaml --commit
```

Run the commit stages for several staging files, validating **once** at the end
rather than per file. On 2026-07-24 there were three shops in one day, so
`inventory-md parse` + `inventory-md-check-quality` ran three times at roughly two minutes
each — long enough that each invocation had to be backgrounded, and long enough
that the runs had to be serialised by hand to avoid racing on `inventory.json`.

Alternative or addition: `--no-validate`, so a caller can skip validation on all
but the last file.
