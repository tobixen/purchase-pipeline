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

Usage::

    pipeline.py staging/shopping-DATE.yaml             # dry run — show plan + previews
    pipeline.py staging/shopping-DATE.yaml --commit    # run pending stages, update status
    pipeline.py staging/shopping-DATE.yaml --commit --from inventory   # force-restart at a stage
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


def next_pending(status: dict[str, str], stages: list[Stage]) -> list[Stage]:
    """Stages whose status is neither ``done`` nor ``skipped`` (missing = pending)."""
    out = []
    for st in stages:
        val = status.get(st.status_key, "pending").strip().lower()
        if val not in ("done", "skipped"):
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("staging", type=Path)
    ap.add_argument("--commit", action="store_true", help="Run stages and update status (default: dry run)")
    ap.add_argument("--inventory", type=Path, default=Path("inventory.md"))
    ap.add_argument("--inventory-json", type=Path, help="Path to inventory.json (default: alongside inventory.md)")
    ap.add_argument("--ledger", type=Path, help="Override ledger path (default: ledger.py's own default)")
    ap.add_argument("--from", dest="from_stage", help="Force-restart at this stage, ignoring its status")
    args = ap.parse_args(argv)

    try:
        import yaml
    except ImportError:
        sys.exit("pyyaml required")

    text = args.staging.read_text(encoding="utf-8")
    staging = yaml.safe_load(text)
    status = read_status(staging)
    inv_json = args.inventory_json or args.inventory.with_name("inventory.json")

    if args.from_stage:
        names = [s.name for s in STAGES]
        if args.from_stage not in names:
            sys.exit(f"--from must be one of {names}")
        start = names.index(args.from_stage)
        todo = STAGES[start:]
    else:
        todo = next_pending(status, STAGES)

    print(f"# Pipeline for {args.staging}")
    print("  status:", ", ".join(f"{s.status_key}={status.get(s.status_key, 'pending')}" for s in STAGES))
    print("  to run:", ", ".join(s.name for s in todo) or "(nothing pending)")

    if not args.commit:
        for st in todo:
            # Only preview the stages that have a real dry-run; ledger always writes.
            if st.name in ("inventory", "tingbok"):
                _run(_stage_cmd(st, args.staging, args.inventory, args.ledger, inv_json, commit=False))
            else:
                print(f"\n(skip preview for {st.name}: no dry-run; would run on --commit)")
        print("\nDRY RUN — pass --commit to execute, update status, and validate.")
        _print_followups(status)
        return 0

    for st in todo:
        rc = _run(_stage_cmd(st, args.staging, args.inventory, args.ledger, inv_json, commit=True))
        if rc != 0:
            print(f"\n✗ stage '{st.name}' failed (exit {rc}); status left unchanged so re-running resumes here.")
            return rc
        text = set_status_in_text(text, st.status_key, "done")
        args.staging.write_text(text, encoding="utf-8")
        print(f"  ✓ {st.status_key}: done")

    # Validate (not status-tracked): regenerate JSON and run the quality gate.
    if _run(["inventory-md", "parse", str(args.inventory)]) != 0:
        print("\n✗ inventory-md parse failed")
        return 1
    rc = _run([CHECK_QUALITY_CMD, str(inv_json)])
    if rc != 0:
        print("\n✗ quality gate failed — fix inventory.md before committing")
        return rc

    print("\n✓ commit stages done + quality gate passed.")
    _print_followups(read_status(yaml.safe_load(text)))
    return 0


def _print_followups(status: dict[str, str]) -> None:
    print("\nManual follow-ups (not driven here):")
    print("  · diary-update  — one expense line per category (split a mixed card charge by hand)")
    for key, hint in (
        ("off_upload", "off_upload.py --products ... --commit"),
        ("open_prices", "openprices_publish.py --shop ... --commit"),
    ):
        val = status.get(key, "pending").strip().lower()
        if val not in ("done", "skipped"):
            print(f"  · {key} pending — public write: {hint}")
    print("  · git add inventory.md staging/ && git commit   (ledger/diary commit in their own repos)")


if __name__ == "__main__":
    sys.exit(main())
