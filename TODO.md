# TODO — purchase-pipeline

**Tasks 0–4 are done** (2026-07-25/29) — kept below with a `DONE` note rather
than deleted, because each records a real regression case worth keeping. Only
task 5 remains. Task 4's `--commit` path is written and tested but has never run
against the live API; see the note there.

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

## 4. `osm_add_shop.py` — create a surveyed shop node — **DONE**

Done: `src/purchase_pipeline/osm_add_shop.py` (`osm-add-shop`) and
`src/purchase_pipeline/osm_auth.py` (`osm-auth`, OAuth 2.0 + PKCE with the
out-of-band redirect, since a CLI has no callback server and OSM has no password
grant). `osmapi` handles the changeset dance and is in the `publish` extra.

All three constraints are implemented, and the duplicate check got a fourth guard
that was not in the sketch — see "Two live findings" below. The waiver is
per-object: there is deliberately **no `--force`**, only
`--not-a-duplicate-of TYPE:ID` repeated once per blocker, because an agent cannot
honestly produce those flags for objects it has not looked at.

**Not verified:** the `--commit` path has never run. It needs an OAuth
application registered by tobixen (that registration names *them* as the editor,
so it is not mine to do) and, more to the point, a genuinely unmapped shop. Every
other path is covered by tests and was rehearsed against live Overpass. When the
first real shop turns up, rehearse against the dev server first:
`--api https://master.apis.dev.openstreetmap.org`.

Original notes follow.

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

**The Sozopol case is closed, but not by this task going away.** Running the new
`osm-resolve` against those coordinates on 2026-07-29 finds Sozopol Fish mapped,
18 m out:

```
osm-resolve --lat 42.41934022085787 --lon 27.692148284820842 --name "Sozopol Fish" --radius 60
  1. score 1.00  NODE:14048113335  18 m  Sozopol Fish  [shop=seafood]
  2. score 0.52   WAY:301280221    10 m  Sozopol marketplace  [amenity=marketplace]
  4. score 0.11  NODE:5002039921   20 m  Грив-56  [shop=wine]
```

tobixen added it **by hand**, and then asked for these tools precisely so as not
to have to do that again. So the requirement stands; only the test fixture is
gone. Do not read "already mapped" as "not needed" — that mistake was made once
in this file already.

(Candidate 4 is the wine shop Nominatim used to return for these coordinates —
task 3's reproducer, now a ranked candidate scoring 0.11 rather than being *the*
answer. Candidate 2 matters for this task: a marketplace way enclosing the shop
means the duplicate check has to distinguish "a POI of the same kind is here"
from "this shop is here".)

The 2026-07-24 mussel price is publishable today with:

```
osm-resolve --save-as "Sozopol Fish <street>" --pick NODE:14048113335
```

### Two live findings, both from actually running it (2026-07-29)

Neither would have shown up in unit tests, and both are now regression-tested.

**1. Overpass fails often, and "could not check" must never read as "clear".**
The very first live run got `HTTP 504 Gateway Timeout` and died with a traceback.
It happened to be safe — the exception aborted the run — but by accident rather
than by design, which is not a guard. `overpass_query` now raises
`OverpassError` for transport/status/parse failures, distinct from an empty
answer, and both `osm-resolve` and `osm-add-shop` refuse cleanly on it. The main
instance 504'd repeatedly over ~20 minutes, so this is a normal path, not an
exotic one.

**2. A regional-extract mirror silently disables the duplicate check.** Added
`--overpass-endpoint` as a 504 workaround, tried
`https://overpass.osm.ch/api/interpreter`, and it reported a **clear site** for
the Sozopol coordinates — where a mapped `shop=seafood` sits 18 m away. It is a
Switzerland-only extract: `[]` for Bulgaria, full data for Zürich. So the flag
introduced to work around finding #1 would have waved through exactly the damage
this whole task exists to prevent.

An empty answer is now corroborated before it is trusted: `has_coverage()` asks
whether the endpoint holds any `highway` way within 1 km, and a no refuses the
run. Roads rather than POIs — somewhere with a shop has a road within a
kilometre, but may legitimately have no *mapped shop*. The probe only fires when
the duplicate query came back empty; if it returned POIs, coverage is self-evident.

The general lesson is worth keeping beyond this task: **an empty result is not
evidence of absence until the source is known to cover the question.** It is the
same shape as the task-1 bug (one cached Billa is not evidence of being the right
Billa) and the task-2 bug (a receipt that parses is not evidence of parsing
correctly). Three for three, on this trip's worth of code.

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

---

## 6. Script the Lidl+ shopping-history download

Migrated from inventory-md's TODO on 2026-07-29: it was filed there as "the
integration with the Lidl+ shopping history downloader should be scripted better
and included in the inventory system", but a shop's receipt history is this
project's business, not the inventory format's.

`shop_import` and `ledger` both read `~/regnskap/lidl_receipts.json`
(`shop_import --receipt`, `ledger lidl --receipt`), and nothing in either
repository produces that file — it arrives by a manual, undocumented step. So the
first task is to write down what that step currently is, before automating it.

What "scripted better" should mean:

- a command that fetches the history and writes/updates `lidl_receipts.json`
- append rather than replace, so already-imported trips are not re-fetched and a
  hand-corrected entry is not silently overwritten
- record where each receipt came from, as the other importers already do via
  `source`
- credentials handled like the other authenticated integrations
  (`openprices-auth`, `osm_auth.py`), never inline in a script

Worth checking whether an existing library already does the Lidl Plus API
(there are third-party clients) before writing a scraper.

---

## 7. Populate a staging file's `ean` + `bb` without human photo inspection

Migrated from inventory-md's TODO on 2026-07-29, **split across both projects** —
neither half delivers the goal alone, so this entry and its inventory-md
counterpart cross-reference each other.

Goal: the agent never has to open a product photo. A reviewed staging file should
arrive with `ean` and `bb` already populated, and only genuinely unresolved items
flagged for the user.

This project's half is **association** — deciding which item an extracted code or
date belongs to. `classify_photo_result()` in `shop_import.py` sorts an
`extract_barcodes.py --json` result into barcode / expiry / label, and the pairing
rule (a barcode photo with the expiry in that photo, or in the immediately
following one) plus matching the pair to a receipt line lives here.

inventory-md's half is **extraction** quality — best-before OCR against
dot-matrix printer fonts, curved and foil surfaces, and low-contrast embossing.
Tracked under "Best-before OCR is not reliable enough to skip reading photos" in
`~/inventory-md/docs/TODO.md`. Orientation is already handled there.

Note that inventory-md's extractor now emits `tag:TODO` review blocks for a
conflicting barcode read and for a photo whose barcode is present but undecodable.
Whatever consumes extractor output here has to route those to the reviewer rather
than drop them or treat them as items.
