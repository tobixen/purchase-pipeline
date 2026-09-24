# TODO — purchase-pipeline

Tasks 0–7, all filed while processing the 2026-07-24 Sozopol shopping trip, are
done and have been removed from this file (2026-08-02); see git history for the
notes, and README.md for the design rules they settled. What is left below is
what has not been finished.

Every task assumes the project conventions in `~/.claude-personal/CLAUDE.md`:
write the failing test first, then implement; type-annotate public APIs; update
docs and CHANGELOG; commit with `git-ai-commit`; don't push without being asked.

---

## DONE 2026-08-06: `osm-add-shop --commit` has run against the live API

Created `NODE:14077646501` — ЛЗ Яхтен магазин, a chandlery at Варна Морска гара
that was genuinely absent from OSM. The whole path is now exercised: live
Overpass duplicate check, OAuth token, changeset open/create/close. The user
registered the OAuth application themselves, as this item always said they would
have to.

Two things to fix, both found by this first real run:

* **`osmapi` is imported at the point of upload, so a missing dependency fails
  the run at the very end** — after the duplicate check, the node preview and the
  changeset preview have all been printed, and after an Overpass round-trip has
  been spent. It is in the `publish` extra, which had evidently been installed
  without it (`openfoodfacts`, `Pillow` and `niquests` were all present). Import
  it, or probe for it, before any network call: failing in the first second with
  "pip install osmapi" is strictly better than failing in the last.
* **`osm-auth` cannot be driven through a non-interactive stdin.** The workflow
  guide tells the agent to hand interactive commands to the user via the `!`
  prefix, but that gives the process no tty, so `input("code: ")` dies with
  `EOFError` after the authorize URL has already been printed. Piping the code in
  is not a workaround either, because the PKCE verifier is regenerated per run and
  the code is bound to the previous challenge. Making it resumable — persist the
  verifier next to the printed URL, accept `--code` on a second invocation — would
  let the whole flow be driven from a session instead of needing a second terminal.

## Superseded: `osm-add-shop --commit` has never run against the live API

Every other path is tested and was rehearsed against live Overpass, but the
upload itself is unexercised. It needs two things that cannot be arranged on
demand:

* an OAuth application registered by tobixen — that registration names *them* as
  the editor, so it is not an agent's to do;
* a genuinely unmapped shop.

When the first real one turns up, rehearse against the dev server first:
`--api https://master.apis.dev.openstreetmap.org`.

**Update 2026-08-05: the unmapped shop has turned up, and everything up to the
write now works against live data.** ЛЗ Яхтен магазин (a chandlery at Варна
Морска гара) is not in OSM; the user surveyed it and asked for it to be added.
The run got through the duplicate check against live Overpass ("3 POI(s) nearby,
none of them this shop") and printed the node and changeset, then stopped exactly
where this item predicted, on `❌ no OSM token`. So the only untested code is the
changeset open/create/close. The pending invocation, ready to re-run once
`osm-auth --client-id …` has been done:

```bash
osm-add-shop --lat 43.19229626619297 --lon 27.92132028759554 \
    --name "ЛЗ Яхтен магазин" --shop boat --tag "name:en=LZ Yacht Shop" --commit
```

Worth noting two behaviours that were confirmed by accident and are exactly
right: when Overpass returned 504 the tool refused to write at all rather than
proceed without a duplicate check, and the duplicate check passing meant no
`--not-a-duplicate-of` flags were needed — which is the only honest outcome for
an agent, since it cannot look at a map link.

## DONE 2026-08-05: `lidl-history --fetch` ran against the live API

Lives in [order-scrapers](https://github.com/tobixen/order-scrapers), not here.
Previously exercised only against a stub downloader. On 2026-08-05 the real fetch
ran with a logged-in Lidl+ browser session and appended 48 records; the
`--country` flag was not needed, as the config supplies it. Two notes for the
consumer side, which is this project:

* it writes `~/regnskap/lidl-history.jsonl` now, while `shop-import --receipt`
  still reads shopping-analyzer's `~/regnskap/lidl_receipts.json`. Both are
  produced by the same run, but `shop-import --help` and the workflow guide only
  mention the latter, so which file is which is easy to get wrong.
* trips selected by `--date` worked correctly for two same-chain, different-branch
  visits.

## Optional: derive the Billa branch key from the receipt

`match_shop_osm` now requires an exact branch key, which closed the bug. Filling
that key in automatically is still open, and is not as trivial as it looks — the
2026-07-24 Billa receipt carries two different addresses, the company's
registered address in the header (`СОЗОПОЛ УЛ. ИНДУСТРИАЛНА 3`) and the actual
store address in the card-terminal footer (`УЛ. РЕПУБЛИКАНСКА 5`). OSM matches
the footer. Which address to trust is a per-chain rule, so it belongs in
`src/purchase_pipeline/data/receipt-formats.json`.

## Bug: `staging-to-inventory` does not shop-prefix in-store EANs

Found 2026-08-05. `tingbok-push` rewrites an in-store (GS1 restricted range) code
to `lidl-20404741` before pushing, but `inventory_import` writes the same value
into `inventory.md` bare as `EAN:20404741`. `inventory-md-check-quality` then
flags every one of them:

    Shop-local EAN lacks shop-name prefix … use EAN:<shop>-20404741

Six lines needed correcting by hand with `inventory-md edit` after one day's
shopping, so this is not rare — Lidl's `20xxxxxx` range covers a lot of the deli
counter and own-brand snacks. The two writers should share whatever
`tingbok_push` uses to decide the prefix. Note the shop name has to come from
somewhere: `tingbok-push` derives `lidl-` from the staging `shop:` field, so the
same derivation needs the normalised branch key (see the next item).

## `shop-import` writes a chain name that can never resolve

Found 2026-08-05. `shop-import` sets `shop: Lidl Varna` from the receipt, but
`match_shop_osm` matches the OSM cache on an **exact** branch key
(`Lidl Varna ул. Битоля 1А`) and deliberately refuses bare chain names. So every
Lidl staging file starts with a shop string that is guaranteed not to resolve,
and both the staging file and the already-imported ledger rows have to be
rewritten by hand before Open Prices can run — 44 ledger rows on this trip.

The receipt does carry the branch (`Лидл Варна 192 / гр. Варна, ул. "Битоля", № 1А`),
so this overlaps with "derive the Billa branch key from the receipt" above; it is
the same feature seen from the other end. Until then, `shop-import` could at
least warn that the shop it wrote will not resolve against the cache.

## Not this project: best-before OCR quality

The other half of "populate `ean` + `bb` without human photo inspection".
Association is done here; extraction quality — dot-matrix print, foil, curved
surfaces, low-contrast embossing — is tracked in `~/inventory-md/docs/TODO.md`.
Nothing here can improve a date the OCR could not read.
