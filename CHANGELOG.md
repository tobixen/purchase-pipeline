# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **First release: the purchasing pipeline now has its own project.** Receipt import, the staging schema, the purchases ledger, the commit pipeline and the Open Prices / Open Food Facts / tingbok publishers moved here out of `inventory-md/scripts/`, where they inverted the dependency — a general-purpose inventory tool knowing about one person's accounting and receipt formats. The diary↔ledger coverage check moved here too, from `~/bin`, where it lived because it belonged to neither project.
- **Everything is a console script**: `shopping-context`, `receipt-formats`, `shop-import`, `purchase-pipeline`, `purchase-ledger`, `staging-to-inventory`, `tingbok-push`, `off-upload`, `osm-resolve`, `openprices-publish`, `openprices-auth`, `check-grocery-ledger`. No more `~/inventory-md/scripts/foo.py` paths to remember or allowlist.
- **A staging file must balance**: its line items have to sum to `receipt_total`, or every consumer refuses it. Transcribing a photographed receipt is the only point in the pipeline where a human reads numbers off an image, and the sum is the only cross-check that exists for that reading.
- **The workflow guide moved here too** (`claude-skills/process-shopping.md`, from `inventory-md/claude-skills/`) — it is a manual for this project, and all but three of its commands are this project's. The duplicated item-adding reference it carried (the `inventory-md add` / `inventory-md edit` blocks) is gone in favour of pointing at inventory-md's `docs/ADDING-ITEMS.md`, which is where the item-line format, field reference, categories, tags and best-before conventions belong.
- **`osm-resolve`** answers "which OSM object is this shop, and is it mapped at all?" in one command — `--lat/--lon/--name` lists what Overpass finds within a radius, ranked by name similarity and distance, each with a map link to eyeball. Confirming a candidate is still yours to do in a browser; `--save-as KEY --pick TYPE:ID` records the answer afterwards. It replaces the reverse-geocode `openprices-publish --suggest-from-photo` used to print, which answered a different question and once suggested the wine shop next door; that flag is now `--coords-from-photo` and prints a photo's GPS as an `osm-resolve` command to run.
- **A cached shop must name a branch.** `Billa Sozopol ул. Републиканска 5` is a key; `Billa` is refused, because a price has to point at one store and a chain-only key would quietly resolve to whichever branch was saved first. Repointing an existing key needs an explicit override too.
- **`receipt-formats`** records per-chain receipt layout quirks — which address line names the branch, whether an `N x unit_price` multiplier belongs to the line above or below it, how discounts and deposits print — and prints them as a checklist before transcription.
