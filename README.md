# purchase-pipeline

Turns a shopping trip into structured data: a receipt becomes a reviewed staging
file, and from there a spending ledger, inventory entries, product observations,
and published prices.

## Why this exists

The work splits into three domains that are deliberately independent:

| Project | Answers |
|---|---|
| [inventory-md](https://github.com/tobixen/inventory-md) | What do I own, where is it, when does it expire? |
| [diary-md](https://github.com/tobixen/diary-md) | What happened, and what did it cost? |
| **purchase-pipeline** | What did I buy, at what price, and where does that fact need to go? |

The third domain used to have no home. Its code sat inside
`inventory-md/scripts/`, which inverted the dependency: a general-purpose
inventory tool ended up knowing about one particular person's accounting, Open
Food Facts account and receipt formats. Some of it didn't even manage that — the
diary↔ledger coverage check lived loose in `~/bin` precisely because it belonged
to neither project.

## Install

```bash
pip install -e ~/inventory-md     # not on PyPI; see below
pip install -e .
```

The console scripts must be on `PATH` for the workflow guide and the Claude
allowlists to work — a user install (`pip install --user -e .`, scripts landing
in `~/.local/bin`) is enough.

## The workflow guide

[`claude-skills/process-shopping.md`](claude-skills/process-shopping.md) is the
generic, staged procedure for turning a trip into all of the above. It lives
here because all but three of its commands do. What an inventory *item* looks
like once written is inventory-md's business and stays in its
[`docs/ADDING-ITEMS.md`](https://github.com/tobixen/inventory-md/blob/main/docs/ADDING-ITEMS.md).

## Commands

Every module is a console script; none of them need a path.

| Command | Role |
|---|---|
| `shopping-context` | read-only trip context: shop OSM object, recent staging files, prior ledger rows, diary lines |
| `receipt-formats` | per-chain receipt layout quirks — **run before transcribing a photographed receipt** |
| `shop-import` | receipt + barcode scan → human-correctable staging YAML |
| `purchase-pipeline` | drive the commit stages (ledger → inventory → tingbok → validate) from the file's `status:`; takes several staging files at once |
| `purchase-ledger` | `purchases.jsonl`: import / query / consumed |
| `staging-to-inventory` | write reviewed staging rows into `inventory.md` |
| `tingbok-push` | push reviewed price/receipt-name observations to tingbok |
| `off-upload` | create missing Open Food Facts products |
| `osm-resolve` | find a shop's OSM object from surveyed coordinates (Overpass), and cache the confirmed pick per branch |
| `osm-add-shop` / `osm-auth` | create one surveyed shop node in OpenStreetMap / mint the OSM token |
| `openprices-publish` / `openprices-auth` | publish prices to Open Prices / mint a token |
| `check-grocery-ledger` | the diary↔ledger coverage gate (was `~/bin/check-grocery-ledger`) |

## The staging file is the human gate

`shop-import` does only mechanical work. It reads no judgement into a photo:
where it fills a field, two independent sources agreed, and the row records
which (`ean_source`, `bb_source`). Everything irreversible (inventory write,
tingbok PUT, OFF/Open Prices publish) happens *after* a human has reviewed the
staging YAML.

Two guards protect that gate, both earned from real mistakes:

* **The line items must sum to `receipt_total`.** Transcribing a photographed
  receipt is the only point in this pipeline where a human reads numbers off an
  image, and the sum is the only cross-check that exists for that reading. A
  mismatch is an error, not a warning (`staging.reconcile_total`).
* **`receipt-formats` records what makes a transcription go wrong** — per chain,
  because a chain's receipts look the same in every town. Billa prints the
  `N x unit_price` multiplier on the line *above* its item, so a naive top-down
  reading of the 2026-07-24 receipt billed three beers to a pack of cleaning
  cloths. Nothing on the photo distinguishes the two readings; only the total
  does.

An entry exists in the registry only for a chain whose receipt has actually been
read, and must carry a `source` naming it. An unrecorded chain prints as
unrecorded — a guessed layout gets trusted exactly like a known one.

## Where the Lidl receipts come from

`shop-import --receipt` and `purchase-ledger lidl` both read
`~/regnskap/lidl_receipts.json`, which this project does **not** produce.
Building a purchase history out of a web shop is
[order-scrapers](https://github.com/tobixen/order-scrapers)' job — it is AGPL
precisely so it can sit next to
[shopping-analyzer](https://github.com/tobixen/shopping-analyzer), the project
that talks to Lidl's ticket API with cookies from a logged-in browser:

```bash
lidl-history --fetch --country bg     # order-scrapers; log in to Lidl+ first
```

That refreshes `lidl_receipts.json` and ingests it into order-scrapers' own
JSONL history. This pipeline then reads the **raw** file rather than that JSONL,
because the raw receipts carry the per-line discounts and net totals the ledger
books and Open Prices publishes.

Point `[lidl] input` in `~/.config/order-scrapers/config.toml` at the same path
this project defaults to, or the two will disagree about where the file lives.

## Which line was that barcode?

A scan proves an EAN was in the basket. It says nothing about which till string
it was rung up as — and the till string is what the receipt, the ledger and the
price observation are keyed by. That gap is `photo_match`'s job, and the split
with inventory-md is along it: *extraction* (what does this photo say?) is
`extract_barcodes.py` over there; *association* (which line does it say it
about?) is here.

Association needs two independent sources to agree: a photo's EAN fills a line
only when that line already listed it among its `ean_candidates` — tingbok's
reverse receipt-name lookup saying "this till string has been that EAN before".
A repeat purchase with no photo at all is filled from a candidate scoring 1.0,
which is not a similarity score but an exact prior observation. Everything else
stays visibly unresolved: the line keeps `ean: null`, and the photo stays in
`loose_photos` with a `review` string saying what stopped it — no line lists this
EAN (a new purchase), or several do. Guessing between them is the reviewer's job
precisely because it cannot be done mechanically, and a guess written into `ean:`
is indistinguishable from a fact by the time the price is published.

The extractor's own verdicts survive the trip. A photo whose barcode decoded two
parity-confusable ways arrives as **one** `barcode_conflict` entry with no `ean`
at all — every candidate has a valid check digit, since a parity misdecode
recomputes the checksum over the corrupted digits, so reading the first one and
moving on would turn a deliberate "look at this" back into a confident answer.
A barcode that is present but unreadable arrives as `undecoded`; a losing read of
a resolved conflict is dropped rather than left sitting next to the winner.

## A day is often several shops

`purchase-pipeline A.yaml B.yaml C.yaml --commit` runs each file's commit stages
in order and then validates **once**, at the end. The closing
`inventory-md parse` + quality gate check the whole of `inventory.md` rather than
the rows just written, so per file they answer the same question three times over
at about two minutes an answer — and having to background and hand-serialise
those runs to keep them off each other's `inventory.json` is what the 2026-07-24
trip actually cost. `--no-validate` skips the gate for a caller who will run it
themselves.

Batching *files* is not batching *writes*: each file remains a separately
reviewed human gate, the status block still advances per stage, and a failure
stops the run there — later files are not started, and nothing is validated over
a half-written inventory. This is the opposite of `osm-add-shop`, which must stay
one shop per invocation: that one writes to a shared public database, where a
batch is exactly what the Automated Edits code of conduct is about.

## A price points at one store, not at a chain

An Open Prices row names an OSM object, so the shop→OSM cache
(`~/.config/inventory-md/shop-osm.json`) is keyed by **branch** —
`Billa Sozopol ул. Републиканска 5`, never `Billa`. `shop_osm` is the only module
that knows this, and it guards the key from both ends: `match_shop_osm` resolves
an exact key and nothing else, and `save_entry` refuses to *store* a bare chain
name, since such a key would then match exactly and resolve silently to whichever
branch was saved first. Both guards exist because a 2026-07-24 trip to Billa
**Sozopol** asked for `"Billa"`, found the single cached Billa, and confidently
returned the **Varna** branch.

`osm-resolve` fills that cache. It asks Overpass what is *within a radius* of a
surveyed point, ranks the answers by name similarity (across `name`, `name:en`,
`brand`, … — a Bulgarian shopfront and a Bulgarian receipt may disagree about
script), and prints each with a map link. Confirming a candidate is a human act
performed in a browser; the command only records the outcome, when given
`--save-as KEY --pick TYPE:ID`. It replaced a Nominatim reverse-geocode, which
answers a different question — "what address is this point" — and returned a
neighbouring wine shop for a fish shop's coordinates.

Coordinates come from a survey or a photo's GPS
(`openprices-publish --coords-from-photo`). Neither this tool nor an agent driving
it may invent them.

## Adding a shop to the map is the one genuinely public write

If `osm-resolve` finds nothing, the shop is unmapped and `osm-add-shop` creates
**one** node from a survey. Everything else in this pipeline is private data or a
reversible row you own; this is an edit to a shared database under the user's own
name, so it carries stricter guards than anything else here:

* **The duplicate check cannot be waived wholesale.** There is no `--force`. Each
  blocking object must be named individually with `--not-a-duplicate-of TYPE:ID`,
  which asserts you opened that link and it is a different business. An agent
  cannot honestly produce those flags — it has not looked at anything.
* **An empty answer is corroborated before it is trusted.** Many Overpass mirrors
  are *regional extracts*, and one of those reports "nothing here" for the rest of
  the planet — identical, to the caller, to a clear site. So when nothing is
  found, the endpoint is asked whether it holds any road within a kilometre. Found
  the hard way: `overpass.osm.ch` cheerfully returned `[]` for Sozopol.
* **Coordinates need 5+ decimal places.** A rounded figure was typed or invented;
  a GPS fix is not round.
* **The changeset is honest** — `source=survey`,
  `created_by=purchase-pipeline/osm_add_shop` — which keeps this inside the
  [Automated Edits code of conduct](https://wiki.openstreetmap.org/wiki/Automated_Edits_code_of_conduct):
  a human survey with a scripted upload. That stops being true the moment it is
  pointed at a batch, so there is one shop per invocation and **no batch mode**.
  Don't add one.

Dry run by default. `osm-auth` mints the token once, requesting only `write_api`;
registering the OAuth application is the user's act, since it names them as the
editor.

## Relationship to inventory-md

This project depends on inventory-md. inventory-md knows nothing about this one.

The dependency is a **library** dependency, not merely a CLI one:

* `photo_match` (run by `shop_import`) parses best-before dates with
  `inventory_md.bb_dates`, the same parser `extract_barcodes.py` uses on photos;
* `inventory_import` calls `inventory_md.additem` and `inventory_md.parser`
  directly to write `inventory.md`;
* `pipeline` shells out to `inventory-md parse` and `inventory-md-check-quality`
  by console-script name, so it never needs to know where a sibling checkout of
  inventory-md sits.

inventory-md is not on PyPI, so it must be installed from a checkout or from git.

`inventory_import.py` lives here rather than in inventory-md because it reads a
staging file (this project's schema) and writes `inventory.md` (that project's
format) — the schema and its only consumer stay together.

## Scope

Owns:

- the staging YAML schema (the reviewed, human-gated representation of a receipt)
- receipt import, including per-chain receipt-format quirks
- the purchases ledger (`purchases.jsonl`)
- the commit pipeline: ledger → inventory → product observations
- publishing: Open Prices, Open Food Facts, tingbok
- shop ↔ OpenStreetMap resolution
- the diary↔ledger coverage gate

Does **not** own:

- inventory format, category vocabulary, barcode/OCR extraction → `inventory-md`
- diary format and reconciliation → `diary-md`
- the EAN/category/price aggregator itself → `tingbok`
- personal paths, credentials and preferences → the personal skill file

## Development

```bash
pip install -e ".[dev]"
pytest
ruff check .
pre-commit install && pre-commit install --hook-type commit-msg --hook-type pre-push
```
