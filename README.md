# purchase-pipeline

Turns a shopping trip into structured data: a receipt becomes a reviewed staging
file, and from there a spending ledger, inventory entries, product observations,
and published prices.

**Status: empty.** This repository was created to hold code that currently lives
in the wrong place — see [TODO.md](TODO.md). Nothing has been migrated yet.

## Why this exists

The work splits into three domains that are deliberately independent:

| Project | Answers |
|---|---|
| [inventory-md](https://github.com/tobixen/inventory-md) | What do I own, where is it, when does it expire? |
| [diary-md](https://github.com/tobixen/diary-md) | What happened, and what did it cost? |
| **purchase-pipeline** | What did I buy, at what price, and where does that fact need to go? |

Today the third domain has no home. Its code sits inside `inventory-md/scripts/`
(`ledger.py`, `shop_import.py`, `pipeline.py`, `openprices_publish.py`,
`tingbok_push.py`, …), which inverts the dependency: a general-purpose inventory
tool ends up knowing about one particular person's accounting, Open Food Facts
account and receipt formats. Some of it doesn't even manage that — the
diary↔ledger coverage check lives loose in `~/bin` precisely because it belongs
to neither project.

This project depends on the other two and calls their CLIs. They stay ignorant
of it.

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
