# Process Shopping

## Meta

This is a staged, resumable workflow for turning a shopping trip into:

* a spending **ledger**
* **inventory** entries
* **tingbok** product observations
* **Open Food Facts** product data (optional)
* **Open Prices** prices.

This is the generic guide — uses `$INVENTORY_DIR`, `$PHOTO_DIR`, `$LEDGER`,
`$RECEIPTS` as placeholders; your personal skill fills in real paths, shops and
credentials.

It lives in **purchase-pipeline**, which owns the workflow and every command in
it bar three. What an inventory *item* looks like once written — the line format,
field reference, categories, tags, quantities, best-before conventions — belongs
to inventory-md and is documented in its
[`docs/ADDING-ITEMS.md`](https://github.com/tobixen/inventory-md/blob/main/docs/ADDING-ITEMS.md).

User may provide information on where things was purchased, what was purchased, etc

## Important

(This is highlighted as "important" not because it is very important, but because the rules are broken on almost every run - and that's annoying).

The procedure is optimized for minimal AI-usage, and a minimum of permission-prompts for the user, but **only if those rules are followed**:

* The procedure in this file should be followed **point by point**.  Commands not listed in the procedure should only be run if the user requests it.
* **Do not** run commands like git, grep, sed to check the status - use the commands provided in the procedure.
* **Do not** chain together commands.
* **Do not** poll for a slow/background command with shell loops (`while kill -0`, `pgrep`, repeated `sleep`).  These are unlisted commands and each one is a permission-prompt — the user is often AFK during this skill and wants it to run unattended.  When a step (e.g. `extract_barcodes.py`, which is slow) is running in the background, simply **stop and wait**: the harness re-invokes you when the command finishes, or you may read its output file **once**.  Never busy-wait.

Exceptions may apply - but if it's needed to run extra commands, it should be considered (together with the user) to improve the documentation, skill files or scripts.

It's allowed to ask the user questions when needed.

The staging file should be the last human gate - it should be approved by the user.  Everything that cannot be reversed trivially (inventory write, tingbok PUT, OFF/Open Prices publish, git commit) happens *after* the staging file is reviewed.

## Procedure

Start a trip with the context command instead of grepping for conventions:
```bash
shopping-context "SHOP" --ledger $LEDGER --diary $DIARY
```
It prints, for the shop: the cached Open Prices OSM object, recent staging files
(a schema example to copy), recent **ledger rows** (prior prices, EANs and the
receipt-name/category convention), and recent diary expense lines. That is
deliberately everything you'd otherwise `tail`/`grep` for — so don't.

**Looking something up in the inventory itself** (does an item already exist?
what's in a section?) — use the parsed JSON, never `grep inventory.md`:
```bash
inventory-md lookup --match TERM   # existing items by id/name (e.g. an EAN already stocked)
inventory-md container ID          # contents of a section/container (e.g. 'floating')
```
`jq` on `inventory.json` covers anything else structured. These are exact and
allowlisted; grepping the markdown is neither.

## User instructions

- User should photograph the **receipt at the shop** so its EXIF GPS marks the location
  (used for Open Prices). Photograph product labels **upright and legible** — the
  best-before date is read by OCR, which honours EXIF orientation but can't read
  faint/sideways print.
- For products that exists in OFF, there should be one photo with the barcode and the next should be with the expiry date.  If both fits into the same photo, only one photo is taken.  For photos of products not existing in off, there should be photos of the front, ingrediences, nutrition information and package recycling information.

## Stage 1 — import (deterministic)

```bash
# Lidl only: pull the trip off Lidl+ (order-scrapers, not this project; log in
# to Lidl+ in the browser first):
lidl-history --fetch --country bg

# BEFORE transcribing a photographed receipt — this chain's layout quirks:
receipt-formats "Billa Sozopol"

# Barcodes + best-before OCR on every photo (barcode shots included):
~/inventory-md/scripts/extract_barcodes.py --best-before $PHOTO_DIR/IMG_*.jpg --json --out barcodes.json

# Receipt + photos -> human-correctable staging file (EAN candidates via tingbok
# reverse receipt-name search; photos classified barcode/expiry/label):
shop-import --receipt RECEIPT.json --barcodes-json barcodes.json \
    --out $INVENTORY_DIR/staging/shopping-YYYY-MM-DD.yaml
```

**One staging file per shop visit** (canonical flat single-shop schema —
`session, shop, currency, items[]`; no multi-shop `shops:` wrapper). If a day
has more than one visit, suffix the file with the shop, e.g.
`shopping-YYYY-MM-DD-lidl.yaml`; the importer rejects a multi-shop file.

Receipt source: a JSON file from a receipt parser, or OCR/read a photographed
receipt into the same shape (`date, shop, total, items[name,price,quantity]`).
For Lidl that file is `$RECEIPTS` (`~/regnskap/lidl_receipts.json`), filled by
**order-scrapers'** `lidl-history --fetch`, which drives the shopping-analyzer
downloader against Lidl's ticket API. It merges rather than replaces: new trips
are appended and stamped, and a trip already on disk is reported but not
overwritten (`--update-all` takes the fetched copy). If it fails, the session
cookies are usually stale — log in to Lidl+ in the browser and run it again.

**Transcribing from a photo is the one place a human reads numbers off an image,
so it is the one place a wrong reading gets in.** Run `receipt-formats "SHOP"`
first: it prints what is known about that chain's layout — which address line
names the branch, whether an `N x unit_price` multiplier belongs to the line
*above* or *below* it, how discounts and deposits print. Billa prints the
multiplier above its item, and on 2026-07-24 the naive top-down reading billed
three beers to a pack of cleaning cloths. Nothing on the photo distinguishes the
two readings — **only the total does**, which is why the transcribed line items
must sum to the printed `receipt_total`; every consumer refuses the file
otherwise (`staging.reconcile_total`). If the chain has no entry yet, transcribe
conservatively and add one afterwards, with a `source` naming the receipt.
The importer emits one row per line item with `ean_candidates` and
`needs_review` flags, and **fills `ean`/`bb` itself wherever two independent
sources agree** — a scanned EAN that the line's own candidate list already
carried, or (with no photo at all) a candidate scoring 1.0. Every filled field
says what filled it in `ean_source`/`bb_source`. A barcode photo with no date of
its own is paired with the date from the *immediately following* expiry photo;
that pairing is positional, so the `bb_source` names the frame it was read off
and both photos are attached to the row for sanity-checking.

What it does **not** settle stays visible rather than guessed: the row keeps
`ean: null` and the photo stays in `loose_photos` with a `review` string saying
what stopped it (no line lists this EAN — the new-purchase regime below; or
several do). `shop-import` prints those on the way out; **read that list**.

Photos need manual inspection only for what lands in `loose_photos` — barcodes
that don't resolve (`kind: barcode_conflict` or `undecoded`) and best-before
dates the OCR couldn't read. Run the scripts first and wait for them: the point
of `extract_barcodes.py`/`shop-import` is to make photo inspection unnecessary
everywhere else. A `barcode_conflict` carries no `ean` on purpose — its
candidates all have valid check digits, so it is a genuine "open the photo and
read the digits" job, not a pick-the-first.

Default assumption: each photo holds **nothing but a barcode and/or an expiry date**, and a product's best-before is either in its barcode photo, in the immediately following photo, or supplied by the user.

## Stage 2 — review (AI, or by user in an editor)

Edit the staging file: for each item **without** an `ean`, pick the right one
from `ean_candidates` (or add one); for each item that arrived **with** one,
check `ean_source` and treat it as the importer's claim, not as your work —
`photo:…` means a scan and the candidate list agreed, `tingbok_receipt_name:1.0`
means an exact prior observation of this till string. Then set `name`,
`category`, `bb` (from the photo's `bb` candidate, else `:EST`), `location`, and
a unique `inventory_id`. Attach label `photos`. Clear `needs_review`. **Set `to_tingbok: true` for items with a confirmed EAN,
`to_tingbok: false` for by-weight produce and items without a barcode.** The
importer scaffolds `to_tingbok: null` as a deliberate reminder — leave no item
at `null` before committing. This is the checkpoint to fix mistakes **before**
anything irreversible. Re-running stage 1 is safe (idempotent ledger; staging is yours).

**Categories — be specific.** Use the most specific leaf category, not a broad
bucket (`tomatoes`, not `vegetables`/`vegetable`; `cheese/kashkaval`, not
`cheese`; `food/eggs`, `fresh-milk`). Broad buckets are useless for the
shopping-list generator and expiry tracking, and the quality gate **fails**
on them (`vegetables`, `fruit`, `nuts`, `meat`, `dairy`, `cheese`, `misc`, …).
A broad/parent category is allowed only when no narrower concept fits — then
exempt that item with the tag `category-broad-ok` (or run with
`--allow-broad-categories`). Get the canonical slug with
`inventory-md vocabulary lookup TERM` — it reports the concept `id` to use,
checks the local `vocabulary.json` first and transparently queries tingbok for
concepts not yet in it (exit code 1 in that case, with the tingbok result still
printed — that's expected for a category new to your inventory). Don't invent
slugs, and don't hand-roll `curl` calls to `/api/lookup/`. Watch mistranslated receipt names (Bulgarian
`КАРТОФИ ЛИЛАВИ` "purple potatoes" were actually purple **sweet** potatoes).

**Quantities — count, not weight.** For by-weight produce, `qty` is the piece
**count** (3 peppers), never the kg weight (`qty: 0.543` is wrong). Put the
**total** weight in `mass:` (`543g`) and the per-kg price in `price` with
`price_unit: kg`; the importer writes `qty:3 mass:543g/3 price:EUR:.../kg`
(single piece → bare `mass:543g`). Packaged multi-buys use the total too
(2×1l milk → `volume: 2l` → `volume:2l/2`). **Ask the user for the count**
when it isn't obvious from the receipt.

**tingbok cross-check (gate — the user MUST respond to these before you proceed):**
for every `ean` you assign, compare the tingbok record to what you bought:

- tingbok **has** the EAN and its description **matches** the purchase → fine,
  proceed silently.
- tingbok has the EAN but its description **does not match** (wrong product,
  wrong quantity) → **flag it and stop**; the user MUST confirm or correct the
  EAN before anything irreversible.
- the EAN is **not in tingbok** → **flag it**; the user MUST confirm the EAN.
  This applies to **any** barcoded item, food or not — push it once confirmed
  (use `tingbok_name`/`tingbok_categories` so the new tingbok entry is useful).
- a **food** product is not in tingbok → flag it and encourage the user to take
  front/ingredients/nutrition/packaging photos so it can be posted to OFF.

Batch these flags into one round of questions rather than asking item-by-item.

**Matching receipt lines to scanned EANs/label photos.** Two regimes:

- **Repeat purchase (same product, same shop)** — algorithmic, and **the
  importer now does it for you**: a lone `score: 1.0` candidate (an exact prior
  observation) is written straight into `ean` with
  `ean_source: tingbok_receipt_name:1.0`. Verified 2026-07-10: all seven Бурлекс
  names from the day before resolved 1.0 to the correct EAN; even
  truncated/partial/other-shop till strings ranked the right product first at
  ~0.6–0.7. Two candidates both scoring 1.0 (a shop that changed supplier under
  one till string) settle nothing and are left for you, with a note on the row.
- **New purchase** — no algorithm settles it; a candidate with **`score < 1.0`
  is only a fuzzy suggestion** and can be plausibly wrong (a never-seen name
  still returns somebody else's product at ~0.68). The importer therefore fills
  nothing and leaves the scan in `loose_photos` saying "no receipt line lists
  EAN … as a candidate". Matching those to receipt lines is AI/human work.

For new purchases, corroborate wherever the material allows: photos arrive as
an ordered stream and adjacent products' label shots are easy to mix up, so if
the label prints a cross-referencable number — **net weight** (`Нето:`/`kg ℮`),
unit or line price on deli labels — check it against the receipt line before
taking a `bb`, mass or EAN from that photo (real case: a `0,120 kg` бекон
label was almost booked as the `0,420 kg` кебапчета). Often there is **no**
such number — that's normal, and the rule is simply: **any doubt → the user
verifies.** And **flag missing-in-OFF products immediately** when discovered
(not at the Stage-4 upload), while the product is still around and unopened
for front/ingredients/nutrition/packaging photos.

## Stage 3 — commit (script + thin AI, gated)

**Drive it with `purchase-pipeline` — one command, not a hand-chained pipeline.**
Once the staging file is reviewed, it runs the ledger → inventory → tingbok
steps in order (reading/advancing the `status:` block, resumable) and then
validates (`inventory-md parse` + `inventory-md-check-quality`):
```bash
purchase-pipeline $INVENTORY_DIR/staging/shopping-YYYY-MM-DD.yaml           # dry run — plan + previews
purchase-pipeline $INVENTORY_DIR/staging/shopping-YYYY-MM-DD.yaml --commit  # run pending stages + validate
```
**Several shops in one day → one invocation**, not one per file: the closing
validation checks all of `inventory.md` and takes about two minutes, so running
it per file repeats the same answer and makes concurrent runs race on
`inventory.json`.
```bash
purchase-pipeline $INVENTORY_DIR/staging/shopping-YYYY-MM-DD-*.yaml --commit  # stages per file, validate once
```
A failure stops the run at that file; the later ones are not started and nothing
is validated, so fix and re-run the same command — the finished files' `status:`
blocks make them no-ops. `--no-validate` skips the closing gate, for when you
will run it yourself.

A `status:` value of `done` skips a stage; `skipped` skips it permanently (e.g.
`tingbok_push: skipped` only when the visit has **no barcoded items at all** —
NOT for non-food hardware. tingbok is the general EAN/category/**price**
aggregator: barcoded tools, batteries, adhesives and chemicals all belong there
(the food-vs-non-food split governs only OFF vs Open Products Facts). On a stage failure it stops and
leaves the status unchanged, so re-running resumes there. `--from STAGE`
force-restarts at a stage (in every file given) — it re-runs `done` stages but
never a `skipped` one. The remaining steps (photos, publishing,
commit) stay manual — see below. The numbered steps that follow are *what the
driver runs*; run them individually only to debug.

1. **Validate** — every item complete; every item has a unique `ID`; food items
   have a `bb` (or `:EST`); no duplicate IDs. (Folded into the inventory write
   and the final quality gate.)
2. **Ledger** — append/enrich `$LEDGER` (one row per line item):
   ```bash
   purchase-ledger import-staging $INVENTORY_DIR/staging/shopping-YYYY-MM-DD.yaml --ledger $LEDGER
   ```
   Append-or-enrich: a raw row from a receipt importer is later filled in place
   with `ean`/`category`/`inventory_id` by the reviewed staging import (matched on
   `date, shop, receipt_name, qty, unit_price, total`; nulls never overwrite).
3. **Inventory** — write every reviewed row straight from the staging file; do
   **not** hand-edit `inventory.md`:
   ```bash
   staging-to-inventory $INVENTORY_DIR/staging/shopping-YYYY-MM-DD.yaml            # dry run — preview the plan
   staging-to-inventory $INVENTORY_DIR/staging/shopping-YYYY-MM-DD.yaml --commit
   ```
   It reads each item's `location` (→ container), `category`, `inventory_id`,
   `ean`, `bb` (an estimate marked either as a `:EST` suffix or as a separate
   `bb_est: true` — both honoured, contradicting each other is an error),
   `qty`/`unit` (weighed lines → `mass`/`volume`)
   and `price`, formats the line, inserts it in the right section, and runs the
   QA checks as part of the write: duplicate `ID:`, food-without-`bb:` (hard
   error; `--no-bb-check` to override for fresh produce), and category resolution
   (`--strict` to fail on unresolved). `add_to_inventory: false` rows are skipped;
   rows whose `inventory_id` already exists are reported as `exists` and skipped,
   so re-running is safe. Missing `location` defaults to `floating`. So the review
   step (Stage 2) must fill `location`, `category`, `bb` and a unique
   `inventory_id` per row — there is nothing left to edit by hand here.
   This is `inventory-md add` applied per row. The item-line format, the field
   reference, categories, tags, quantities and best-before conventions are
   inventory-md's, not this project's — see
   [`inventory-md/docs/ADDING-ITEMS.md`](https://github.com/tobixen/inventory-md/blob/main/docs/ADDING-ITEMS.md)
   and don't duplicate them here.

   Two cases fall outside the receipt flow entirely and are documented there:
   **one-off additions** (a found item, an installed fixture, items rebuilt from
   an order history) via `inventory-md add`, and **correcting a line already
   written** (a late label photo with the real EAN/bb, a shop-local barcode
   needing its chain prefix) via `inventory-md edit`.

   The one thing worth repeating here, because it bites during a shopping run:
   `staging-to-inventory` does **not** write `--tag`, but `inventory-md add`
   does — needing a tag is **not** a licence to hand-edit `inventory.md`. With
   `add`, `move` and `edit` there is no remaining reason to touch the markdown
   by hand.
4. **Photos** (manual) — copy only **label** photos to `photos/LOCATION-ID/`; skip
   barcode/expiry close-ups; skip fast-consumed items. Never `git add` photos.
5. **tingbok** — push price + receipt-name observations for reviewed EANs (a
   merge PUT; prices/receipt_names appended, re-running is safe). Use the script,
   never a raw `curl`:
   ```bash
   tingbok-push $INVENTORY_DIR/staging/shopping-YYYY-MM-DD.yaml            # dry run
   tingbok-push $INVENTORY_DIR/staging/shopping-YYYY-MM-DD.yaml --commit
   ```
   It pushes only items with `to_tingbok: true` and an `ean`; per-item
   `tingbok_name`/`tingbok_categories`/`tingbok_quantity` override a poor or
   missing tingbok name.
6. **Quality gate** — regenerate and check (flags food without best-before,
   duplicate IDs, unresolvable categories). Two separate commands, not chained:
   ```bash
   inventory-md parse inventory.md
   inventory-md-check-quality inventory.json
   ```
7. **Commit** (manual) `inventory.md` (+ staging file, + photo-registry.md if used).
   The ledger is committed in its own repo. (Personal workflows may add extra
   steps here — see the personal skill.)

## Stage 4 — contribute upstream (optional, gated)

**Missing OFF products** (EANs that don't resolve in OFF) — create them from a
curated YAML with front/ingredients/nutrition/packaging photos:
```bash
off-upload --products off-products.yaml          # dry run
off-upload --products off-products.yaml --commit  # writes to OFF
```

**Open Prices** — publish receipt prices (auth once via `op_auth.py`):
```bash
openprices-publish --shop "Shop" --date YYYY-MM-DD \
    --proof RECEIPT.jpg --osm WAY:NNN [--discount EAN=GROSS:SALE] [--commit]
# barcodeless items as CATEGORY prices:
    --no-products --category-price "en:baguettes=0.17,was=0.45,type=SALE"
```
Shop location is a **confirmed** OSM object (cached per branch), never
auto-geocoded — receipt photos are often taken away from the shop.

To find the object, don't hand-roll geocoder round-trips — that is what
`osm-resolve` is for:
```bash
osm-resolve --lat LAT --lon LON --name "SHOP" [--radius 50]     # ranked candidates + map links
osm-resolve --save-as "CHAIN TOWN STREET" --pick TYPE:ID        # after the human confirms
openprices-publish --coords-from-photo PHOTO                    # EXIF GPS → the command above
```

**Before trusting a photo's GPS, check when the photo was taken against the time
printed on the receipt.** A receipt photographed in the shop carries the shop's
position; the same receipt photographed later carries wherever you happened to be,
and nothing in the EXIF says which you have. The receipt prints its own timestamp,
so the two can simply be subtracted:

```bash
exiftool -n -p '$FileName|$DateTimeOriginal|$GPSLatitude|$GPSLongitude' PHOTO...
```

Read the delta as a confidence rating, not a yes/no:

| Δ (photo − receipt) | Reading |
|---|---|
| seconds, either sign | in the shop — the camera clock is simply a little off |
| under ~2 min | trustworthy |
| 5–15 min | probably still the right building, but widen `--radius` and be sceptical |
| hours or days | **the photo is worthless as a location** — it is your own position |

A *negative* delta of seconds is normal and not a red flag: it means the camera
clock runs slow, not that the photo predates the purchase. Verified 2026-08-05
across eleven receipts: a chandlery shot 27 s *before* its receipt printed and a
grocer shot 1 m 52 s after both landed within 3–13 m of the correct mapped shop,
and a Billa photo taken in-store fell 15 m from an OSM node that had been
confirmed by hand on an earlier trip — an independent check that the method
works. In the same batch three receipts re-photographed at home days later all
returned the boat's mooring, one of them ~120 km from the shop that issued the
receipt. That is exactly the failure the "never auto-geocode" rule above exists
to prevent, and the timestamp is what detects it *before* a wrong location is
cached and published.

Two corollaries worth keeping in mind:

- When the delta is large, the answer is not a wider radius — it is that this
  photo cannot locate the shop at all. Find another photo from the trip, or ask.
- The check also validates a *good* result. `osm-resolve` scoring a candidate 1.0
  a few metres away is much more convincing when the photo was demonstrably taken
  at the till, and it is worth saying so when handing the map link over for
  confirmation.

If `osm-resolve` finds nothing the shop is unmapped, and `osm-add-shop` can put it
on the map — but **that is the user's call, never the agent's**, and it is the one
write here that lands in a public shared database under their name:
```bash
osm-add-shop --lat LAT --lon LON --name "SHOP" --shop seafood   # dry run
osm-add-shop … --commit                                          # public, attributed to the user
```
Do not run it on your own initiative, do not invent coordinates (it refuses
anything under 5 decimal places), and do not produce `--not-a-duplicate-of TYPE:ID`
flags: each one asserts that *a human opened that map link* and found a different
business. Ask, hand over the links, and wait.
It queries Overpass for what is *at* a point. A geocoder answers a different
question — "what address is this point" — and on 2026-07-24 reverse-geocoding the
Sozopol fish shop returned the wine shop 20 m away.

Confirmation is the human's, in a browser: hand over the printed
`https://www.openstreetmap.org/node/NNN` link (e.g. via `xdg-open`) and wait. A
name match is not confirmation — chains have many branches, and OSM's mapped
address may differ from the receipt's legal address even for the right store.
Cache keys must name a branch (`Billa Sozopol ул. Републиканска 5`), not a chain;
`osm-resolve` refuses a one-word key, because a chain-only key would then resolve
exactly and silently to whichever branch was saved first.

PRODUCT prices must not set `price_per`. Both OFF and Open Prices are **public** —
treat as irreversible-ish (Open Prices rows are deletable; you own them).

## Queries

```bash
purchase-ledger query --category food --since YYYY-MM-DD --until YYYY-MM-DD
purchase-ledger consumed --inventory inventory.md --since … --until …
```
`consumed` joins ledger rows to items removed from `inventory.md` (git history) to
cost what was actually used in a period — only resolves for rows enriched (ean/
category/inventory_id) through the reviewed staging flow.

## Tools

Everything below is a console script — on PATH once the projects are installed,
no paths to remember. Three of them belong to inventory-md, not here:
identifying a physical object, and validating the inventory it lands in, is
inventory's business; deciding what a purchase *means* is not.

| Command | Project | Role |
|---|---|---|
| `shopping-context` | purchase-pipeline | read-only trip context: shop OSM, recent staging |
| `receipt-formats` | purchase-pipeline | per-chain receipt layout quirks, before transcribing |
| `lidl-history` | **order-scrapers** | fetch the Lidl+ receipt history into `$RECEIPTS` (Lidl trips only) |
| `shop-import` | purchase-pipeline | receipt + photos → staging YAML |
| `purchase-pipeline` | purchase-pipeline | drive Stage-3 commit (ledger→inventory→tingbok→validate) from `status:` |
| `purchase-ledger` | purchase-pipeline | purchases.jsonl: import / query / consumed |
| `staging-to-inventory` | purchase-pipeline | write reviewed staging rows into `inventory.md` |
| `tingbok-push` | purchase-pipeline | push reviewed price/receipt-name observations to tingbok |
| `off-upload` | purchase-pipeline | create missing OFF products |
| `osm-resolve` | purchase-pipeline | find a shop's OSM object by coordinates (Overpass), cache the confirmed pick |
| `osm-add-shop` / `osm-auth` | purchase-pipeline | put a surveyed shop on the map (ask first — public write) / mint the OSM token |
| `openprices-publish` / `openprices-auth` | purchase-pipeline | publish prices / mint token |
| `check-grocery-ledger` | purchase-pipeline | diary↔ledger coverage gate |
| `~/inventory-md/scripts/extract_barcodes.py --best-before` | inventory-md | barcodes + best-before OCR per photo |
| `inventory_md.bb_dates` | inventory-md | OCR-text → best-before date candidates (library) |
| `inventory-md-check-quality` | inventory-md | validation gate (food-bb, dup IDs, categories) |

`tingbok` (`GET/PUT /api/ean/{ean}`, `GET /api/ean/search?receipt_name=`) is the
EAN/category/price aggregator. There is **no `ean_cache.json`** — use tingbok.
Category/concept lookup goes through `inventory-md vocabulary lookup TERM` (no
raw curl). Ad-hoc **EAN** lookup (an EAN that didn't come through
`extract_barcodes.py`, which resolves scanned codes itself) has no wrapper yet —
a read-only `curl GET /api/ean/{ean}` is the sanctioned fallback for that one case.

## TODO

This skill and the scripts are quite fresh.  For each run, try to pinpoint problems and choke-points and suggest ways to improve the procedure.  Some thoughts:

* Almost every time I run the skill, the Claude agent goes off and breaks all the rules in the "important"-section, why is that and how can it be improved?
  * Root cause (2026-06-19): louder warnings don't help — the agent greps because
    `shopping-context` didn't surface everything it reaches for, and the rules
    live in a *second* file it reads only after it has already grepped on instinct.
  * Done: `shopping-context` now also prints recent **ledger rows** for the shop
    (`--ledger`), so there is no reason to `tail purchases.jsonl`; and the Procedure
    section now points inventory lookups at `inventory-md lookup`/`container` + `jq`,
    never `grep inventory.md`.
  * Still open: hoisting the one hard rule to the very top of the loaded artifact
    (personal SKILL.md / command body), so it is seen *before* the first action.
* Perhaps the directories above should go into a config file?
  * Still open. Would let `shopping-context`/`purchase-pipeline` find `$LEDGER`/`$DIARY`
    without the caller passing them each run. Mild win; only pursue if the path-passing
    keeps biting.
* Quantity vs mass — **mostly resolved** by the Stage-2 "Quantities — count, not weight"
  section (qty = piece count; total weight in `mass:`; per-kg price with `price_unit: kg`;
  packaged multi-buys use the total; ask the user for the count). Remaining decision:
  * Pin down per-unit vs total notation: the importer writes `mass:450g/3` (total/count),
    while `qty:3 mass:150g` (per-pack) was the original wish — pick one and document it.
  * The "rename ledger `qty`→`purchase-qty`" idea is **not** worth doing: the importer
    already distinguishes `mass`/`volume` from the count, so it would be churn.
* Ad-hoc EAN lookup (2026-07-02): category lookups now go through
  `inventory-md vocabulary lookup`, but looking up a *manually read* EAN (from a
  photo the scanner missed) still needs a raw `curl GET /api/ean/{ean}` →
  permission prompt. Consider `inventory-md ean EAN` or a `tingbok_lookup.py`
  helper so the whole skill runs unattended.
* Shop OSM cache too coarse — Lidl/Billa have **many branches per city**, so even
  "Lidl Varna" names one specific store.
  * Done: `match_shop_osm` resolves on an **exact** (case-insensitive) cache key only,
    returning nothing and listing the candidate branches otherwise; cache keys re-keyed
    to include the branch street. It formerly fell back to an *unambiguous* substring
    match, which was not enough: with only one Billa cached there is nothing to be
    ambiguous about, so a 2026-07-24 trip to Billa Sozopol resolved to the Varna branch.
  * When caching a new shop, key it by branch (shop + street), and confirm the OSM object
    is that exact store before publishing Open Prices. `osm-resolve --save-as` now
    enforces the branch key, and refuses a one-word one.
  * Also done (2026-07-29): `osm-resolve` finds the object from coordinates via
    Overpass, so finding the node id of an **existing** shop is one command rather
    than four hand-rolled Nominatim round-trips. The Nominatim reverse-geocode is
    gone: `openprices-publish --coords-from-photo` now prints the EXIF GPS as an
    `osm-resolve` invocation instead of guessing a shop from an address. And when
    the shop is genuinely unmapped, `osm-add-shop` adds it — one node, dry-run by
    default, with a duplicate check that cannot be waived except per object.
* Overpass answers "nothing" in two very different ways (2026-07-29): a real empty
  area, and a mirror that does not hold the region at all. Treat an empty result as
  evidence only when the source is known to cover the question — `osm-add-shop`
  probes for that, `osm-resolve` does not, so do not read its "no POI found" as
  proof a shop is unmapped if you passed `--endpoint`.
* Photo-GPS staleness is checked by hand (2026-08-05, documented in Stage 4): the
  agent has to run `exiftool` itself and subtract the receipt's printed time.
  `--coords-from-photo` already reads the EXIF, so it could print
  `DateTimeOriginal` alongside the coordinates, and — given `--date` or a staging
  file — say outright how far the photo is from the purchase and refuse to suggest
  an `osm-resolve` command for a photo taken days later. Worth doing: on the
  2026-08-05 run, three of eleven receipt photos were re-shots carrying the boat's
  mooring rather than the shop, and only the manual subtraction caught them.
* Overpass was down or rate-limiting for much of 2026-08-05 (504s and 429s across
  `osm-resolve` and `osm-add-shop`). Nothing is wrong with the tools — they fail
  cleanly with the HTTP error and `osm-add-shop` correctly refuses to write when its duplicate check
  cannot run — but a run can stall on it. Consider a documented fallback mirror.
* Fixed 2026-07-09 (Бурлекс run friction):
  * `add_item` crashed on YAML-native dates (`bb: 2026-07-12` → `datetime.date`) —
    now coerced; staging bb values no longer need quoting.
  * `shop_import.py` stamped the Lidl header (shop/total/source) onto hand-transcribed
    receipts — the generic keys `date`/`shop`/`currency`/`total` and per-item
    `unit`/`unit_price` are now honoured.
  * "category does not resolve in local vocabulary" warned for every category merely
    *new to this inventory* — `add_item` now falls back to tingbok before warning.
  * `inventory-md parse` dumped one line per EAN/category lookup (hundreds of lines,
    drowning the pipeline output) — per-item lines now need `parse --verbose`;
    default prints summary counts only.
* `extract_barcodes.py` misses/misreads (2026-07-08: two clearly-photographed EANs
  read manually; one deli label gave three conflicting checksum-valid reads).
  Tuning needs real data: processed barcode/label photos are now **kept** (moved to
  `~/s/photos.tobixen/processed/`, see personal skill) instead of deleted — the
  staging files map filename → confirmed EAN/bb, so they double as a labelled
  training/regression corpus. When enough accumulate, build a regression suite for
  the extractor and try multi-crop/rotation retries; report multiple checksum-valid
  candidates as needs-review instead of picking one.
  * **A third failure mode, worse than a miss (2026-08-05): a confident wrong
    answer.** `IMG_20260726_184535.jpg` is a sharp, well-lit Parodontax barcode
    whose digits are plainly legible as `5054563216953`; the extractor returned
    `0085100000563` and resolved it to a "Hillman 851563 brass plated utility door
    pull". Nothing downstream could tell that was wrong — it is a single decode,
    not a conflict, and it looks up to a real product, so `shop-import` had no
    reason to flag it and a reviewer skimming the staging file would not either.
    Contrast the same run's honest failures: four frames returned NO_DECODE
    (including `3800069005445`, Шуменско, equally legible) which at least announce
    themselves. Worth checking whether the decoder is padding a UPC-E expansion or
    reading a truncated crop; and until it is understood, treat a decoded EAN whose
    product description does not fit the receipt line as suspect rather than as
    evidence, even when it resolves.
  * Corpus note: 55 photos from 2026-07-26…08-04 are now in
    `~/s/photos.tobixen/processed/`, with the confirmed answers in the seven
    `staging/shopping-2026-0[78]-*.yaml` files — including four EANs read by hand
    (`3800069005445`, `5054563216953`, `8033137194375`, `8033137035135`) that the
    extractor missed or got wrong, which are the interesting regression cases.
* Fixed 2026-07-22 (2026-07-21 Lidl run friction):
  * A staging row's `bb_est: true` was silently dropped by `inventory_import.py`, so
    shelf-life *guesses* were written as printed dates (9 rows on 2026-07-21, 9 on
    2026-07-10, none with a `:EST` marker). Both spellings (`bb: …:EST` and the
    separate `bb_est:`) are now honoured, and a contradiction between them is a hard
    error.
  * Correcting a field on an existing line needed a hand-edit — the last remaining
    reason to touch the markdown by hand (bit us 2026-07-09 with a bacon bb/mass
    correction from late-surfacing label photos, and three times on 2026-07-21).
    `inventory-md edit ITEM_ID …` now does it, `--est`/`--no-est` included.
