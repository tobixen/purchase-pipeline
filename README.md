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
| `purchase-pipeline` | drive the commit stages (ledger → inventory → tingbok → validate) from the file's `status:` |
| `purchase-ledger` | `purchases.jsonl`: import / query / consumed |
| `staging-to-inventory` | write reviewed staging rows into `inventory.md` |
| `tingbok-push` | push reviewed price/receipt-name observations to tingbok |
| `off-upload` | create missing Open Food Facts products |
| `osm-resolve` | find a shop's OSM object from surveyed coordinates (Overpass), and cache the confirmed pick per branch |
| `openprices-publish` / `openprices-auth` | publish prices to Open Prices / mint a token |
| `check-grocery-ledger` | the diary↔ledger coverage gate (was `~/bin/check-grocery-ledger`) |

## The staging file is the human gate

`shop-import` does only mechanical work. It never decides which EAN a line is and
never reads a best-before off a photo — those are judgement calls left to the
review step. Everything irreversible (inventory write, tingbok PUT, OFF/Open
Prices publish) happens *after* a human has reviewed the staging YAML.

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

## Relationship to inventory-md

This project depends on inventory-md. inventory-md knows nothing about this one.

The dependency is a **library** dependency, not merely a CLI one:

* `shop_import` parses dates off receipts with `inventory_md.bb_dates`, the same
  parser `extract_barcodes.py` uses on photos;
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
