#!/usr/bin/env python3
"""Drive the Stage-3 commit steps of the process-shopping pipeline.

A reviewed staging file carries a ``status:`` block. This script reads it and
runs the pending commit stages **in order**, each as a sub-process of the
existing single-purpose script, updating the ``status:`` value after each
success so an interrupted run resumes cleanly. It exists so the whole commit
stage is *one* allowlisted command instead of a hand-chained
``ledger && inventory && tingbok && parse && check`` — a chained shell string
can't be pre-approved, so chaining is what forces the approval prompts.

Stages driven (in order)::

    ledger     ledger.py import-staging STAGING            (idempotent upsert)
    inventory  inventory_import.py STAGING --commit         (skips existing IDs)
    tingbok    tingbok_push.py STAGING --commit             (merge PUT; skipped if to_tingbok all false)
    validate   inventory-md parse + check_quality.py        (always, not status-tracked)

Deliberately NOT driven here:

* **diary** — lives in a separate repo and may split one card charge across
  expense categories; do it by hand with ``diary-update``.
* **off_upload / open_prices** — public writes; keep them an explicit, separate
  step so the staging review stays the single checkpoint before publishing.

A status value of ``done`` skips the stage; ``skipped`` skips it permanently
(e.g. ``tingbok_push: skipped`` for non-food hardware); ``pending`` or a missing
key runs it.

Several staging files may be given at once. Their stages run file by file, in
the order given, and the closing validation runs **once at the end** rather than
per file: it re-parses and quality-checks the whole of ``inventory.md``, so it
says the same thing however many files were just written, at about two minutes a
go. One day with three shops is the normal case for this.

Usage::

    pipeline.py staging/shopping-DATE.yaml             # dry run — show plan + previews
    pipeline.py staging/shopping-DATE.yaml --commit    # run pending stages, update status
    pipeline.py staging/shopping-DATE.yaml --commit --from inventory   # force-restart at a stage
    pipeline.py staging/A.yaml staging/B.yaml staging/C.yaml --commit   # batch: validate once, at the end
    pipeline.py staging/A.yaml --commit --no-validate  # validate later (e.g. by hand, or with the last file)
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The quality gate lives in inventory-md, which owns the inventory format.
#: Invoked by console-script name rather than by path — like ``inventory-md
#: parse`` just below it — so this project never has to know where a sibling
#: checkout of inventory-md happens to sit.
CHECK_QUALITY_CMD = "inventory-md-check-quality"


@dataclass(frozen=True)
class Stage:
    name: str  # short label / --from selector
    status_key: str  # key in the staging `status:` block (or "" if untracked)


# Order matters: ledger enriches the row, inventory writes the item, tingbok
# records the price observation. validate runs last and is not status-tracked.
STAGES: list[Stage] = [
    Stage("ledger", "ledger"),
    Stage("inventory", "inventory"),
    Stage("tingbok", "tingbok_push"),
]


def read_status(staging: dict[str, Any]) -> dict[str, str]:
    """Return the ``status:`` mapping (or ``{}``), values coerced to str."""
    block = staging.get("status") or {}
    if not isinstance(block, dict):
        return {}
    return {k: str(v) for k, v in block.items()}


def next_pending(status: dict[str, str], stages: list[Stage], force: bool = False) -> list[Stage]:
    """Stages whose status is neither ``done`` nor ``skipped`` (missing = pending).

    With *force* (a ``--from`` restart) a ``done`` stage is included again, but a
    ``skipped`` one is still left out: ``done`` is a record of what has happened
    and re-running it is the point of a restart, whereas ``skipped`` is a
    reviewer's decision that the stage must never run for this file.
    """
    out = []
    for st in stages:
        val = status.get(st.status_key, "pending").strip().lower()
        if val == "skipped" or (val == "done" and not force):
            continue
        out.append(st)
    return out


def set_status_in_text(text: str, key: str, value: str) -> str:
    """Set ``status.<key>`` to *value* by line-editing only inside the status block.

    Preserves comments and the rest of the file (a YAML round-trip would drop the
    reviewer's comments). Only the first indented ``key:`` line that follows the
    top-level ``status:`` line — before the block ends at the next unindented
    non-blank line — is rewritten, so a same-named top-level key is left alone.
    """
    lines = text.splitlines(keepends=True)
    in_block = False
    for i, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if not in_block:
            if re.match(r"status\s*:\s*(#.*)?$", stripped):
                in_block = True
            continue
        # End of block: a non-blank, unindented line.
        if stripped and not stripped[0].isspace():
            break
        m = re.match(rf"(\s+{re.escape(key)}\s*:\s*)\S+(.*)$", line)
        if m:
            lines[i] = f"{m.group(1)}{value}{m.group(2)}\n"
            break
    return "".join(lines)


def _run(cmd: list[str]) -> int:
    print(f"\n$ {' '.join(cmd)}")
    return subprocess.run(cmd).returncode


def _stage_cmd(
    stage: Stage, staging: Path, inventory: Path, ledger: Path | None, inv_json: Path, commit: bool
) -> list[str]:
    # ``-m`` rather than the console-script names: this works from a plain
    # checkout and inside a venv whose bin/ is not on PATH, and it guarantees the
    # stage runs under the *same* interpreter as the driver.
    py = [sys.executable, "-m"]
    if stage.name == "ledger":
        cmd = [*py, "purchase_pipeline.ledger", "import-staging", str(staging)]
        if ledger:
            cmd += ["--ledger", str(ledger)]
        return cmd
    if stage.name == "inventory":
        cmd = [*py, "purchase_pipeline.inventory_import", str(staging), "--inventory", str(inventory)]
        return cmd + ["--commit"] if commit else cmd
    if stage.name == "tingbok":
        cmd = [*py, "purchase_pipeline.tingbok_push", str(staging)]
        return cmd + ["--commit"] if commit else cmd
    raise ValueError(stage.name)


def _process_file(staging: Path, args: argparse.Namespace, inv_json: Path, yaml: Any) -> tuple[int, dict[str, str]]:
    """Run one staging file's pending stages. Returns ``(exit code, final status)``.

    Validation is deliberately *not* run here: it checks the whole of
    ``inventory.md``, not this file's rows, so it belongs to the run and not to
    the file — see ``main``.
    """
    text = staging.read_text(encoding="utf-8")
    status = read_status(yaml.safe_load(text))

    if args.from_stage:
        # A --from selector applies to every file in the run: the stages it
        # re-runs are all idempotent (ledger upserts, inventory skips existing
        # IDs, tingbok merges), so this is a restart, not a duplication.
        #
        # It overrides `done` — that is what a restart is for — but not
        # `skipped`, which says the stage must never run for this file. The
        # distinction only became load-bearing with batch mode: `--from ledger`
        # over a day's files would otherwise push a hardware trip marked
        # `tingbok_push: skipped` to tingbok, for the sake of re-running ledger.
        todo = next_pending(status, STAGES[[s.name for s in STAGES].index(args.from_stage) :], force=True)
    else:
        todo = next_pending(status, STAGES)

    print(f"# Pipeline for {staging}")
    print("  status:", ", ".join(f"{s.status_key}={status.get(s.status_key, 'pending')}" for s in STAGES))
    print("  to run:", ", ".join(s.name for s in todo) or "(nothing pending)")

    if not args.commit:
        for st in todo:
            # Only preview the stages that have a real dry-run; ledger always writes.
            if st.name in ("inventory", "tingbok"):
                _run(_stage_cmd(st, staging, args.inventory, args.ledger, inv_json, commit=False))
            else:
                print(f"\n(skip preview for {st.name}: no dry-run; would run on --commit)")
        return 0, status

    for st in todo:
        rc = _run(_stage_cmd(st, staging, args.inventory, args.ledger, inv_json, commit=True))
        if rc != 0:
            print(f"\n✗ stage '{st.name}' failed (exit {rc}); status left unchanged so re-running resumes here.")
            return rc, status
        text = set_status_in_text(text, st.status_key, "done")
        staging.write_text(text, encoding="utf-8")
        print(f"  ✓ {st.status_key}: done")

    return 0, read_status(yaml.safe_load(text))


def _validate(inventory: Path, inv_json: Path) -> int:
    """Regenerate ``inventory.json`` and run the quality gate (not status-tracked)."""
    if _run(["inventory-md", "parse", str(inventory)]) != 0:
        print("\n✗ inventory-md parse failed")
        return 1
    rc = _run([CHECK_QUALITY_CMD, str(inv_json)])
    if rc != 0:
        print("\n✗ quality gate failed — fix inventory.md before committing")
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "staging",
        type=Path,
        nargs="+",
        metavar="STAGING",
        help="Reviewed staging file(s). Several run in order, validating once at the end.",
    )
    ap.add_argument("--commit", action="store_true", help="Run stages and update status (default: dry run)")
    ap.add_argument("--inventory", type=Path, default=Path("inventory.md"))
    ap.add_argument("--inventory-json", type=Path, help="Path to inventory.json (default: alongside inventory.md)")
    ap.add_argument("--ledger", type=Path, help="Override ledger path (default: ledger.py's own default)")
    ap.add_argument("--from", dest="from_stage", help="Force-restart at this stage, ignoring its status")
    ap.add_argument(
        "--no-validate",
        action="store_true",
        help="Skip the closing inventory-md parse + quality gate (you then owe it before committing)",
    )
    args = ap.parse_args(argv)

    try:
        import yaml
    except ImportError:
        sys.exit("pyyaml required")

    if args.from_stage and args.from_stage not in (names := [s.name for s in STAGES]):
        sys.exit(f"--from must be one of {names}")

    inv_json = args.inventory_json or args.inventory.with_name("inventory.json")

    done: list[tuple[Path, dict[str, str]]] = []
    for i, staging in enumerate(args.staging):
        rc, status = _process_file(staging, args, inv_json, yaml)
        if rc != 0:
            remaining = args.staging[i + 1 :]
            if remaining:
                print(f"  {len(remaining)} later file(s) not started: {', '.join(str(p) for p in remaining)}")
            # No validation after a failure: the stage's own error is what to
            # fix, and a quality gate run over a half-written inventory.md
            # reports that half-written state as if it were the problem.
            print("  (not validated — re-run once the failure is fixed)")
            return rc
        done.append((staging, status))

    if not args.commit:
        print("\nDRY RUN — pass --commit to execute, update status, and validate.")
    elif args.no_validate:
        print("\n✓ commit stages done; validation skipped (--no-validate).")
        print(f"  Still owed before committing: inventory-md parse {args.inventory} && {CHECK_QUALITY_CMD} {inv_json}")
    else:
        rc = _validate(args.inventory, inv_json)
        if rc != 0:
            return rc
        print("\n✓ commit stages done + quality gate passed.")

    _print_followups(done)
    return 0


def _print_followups(done: list[tuple[Path, dict[str, str]]]) -> None:
    """The steps this driver deliberately does not take, per file and overall.

    The public writes are per staging file (each trip publishes its own prices);
    the diary and the git commit are per run, and printing them once per file
    would read as three separate commits to make.
    """
    print("\nManual follow-ups (not driven here):")
    print("  · diary-update  — one expense line per category (split a mixed card charge by hand)")
    for staging, status in done:
        prefix = f"  · [{staging}] " if len(done) > 1 else "  · "
        for key, hint in (
            ("off_upload", "off_upload.py --products ... --commit"),
            ("open_prices", "openprices_publish.py --shop ... --commit"),
        ):
            val = status.get(key, "pending").strip().lower()
            if val not in ("done", "skipped"):
                print(f"{prefix}{key} pending — public write: {hint}")
    print("  · git add inventory.md staging/ && git commit   (ledger/diary commit in their own repos)")


if __name__ == "__main__":
    sys.exit(main())
