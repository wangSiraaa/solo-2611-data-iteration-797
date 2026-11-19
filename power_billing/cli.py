"""Replay the material operations against the library and print receipts.

Usage::

    python3 -m power_billing.cli materials/735 [--db /tmp/billing.db]

The CLI is intentionally small; it exists to demonstrate the library on the
provided synthetic materials, not to define any tariff rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .demand import Contract, evaluate
from .ledger import Ledger, bill_document
from .power import TrajectoryStore


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _print_receipt(label: str, receipt: dict) -> None:
    doc = bill_document(receipt)
    print(
        f"{label}: coverage={doc['coverage']} "
        f"window={doc['window']} demand={doc['demand']} charge={doc['charge']} "
        f"predecessor={doc['predecessor_id']} difference={doc['difference']} "
        f"balance_after={doc['balance_after']}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("materials", type=Path)
    ap.add_argument("--db", type=Path, default=None)
    args = ap.parse_args(argv)

    mdir: Path = args.materials
    power = _load(mdir / "power.json")
    contract_obj = _load(mdir / "contract.json")
    correction = _load(mdir / "correction.json")
    ops = [
        json.loads(line)
        for line in (mdir / "operations.jsonl").read_text().splitlines()
        if line.strip()
    ]

    store = TrajectoryStore()
    store.register_json(power)
    contract = Contract.from_json(contract_obj)
    trace_id = power["trace_id"]
    revision = power["revision"]

    if args.db is not None:
        db_path: str = str(args.db)
    else:
        db_path = ":memory:"
    ledger = Ledger(db_path)
    ledger.register_contract(contract)
    ledger.set_current_power(trace_id, revision)

    last_settle: str | None = None
    for op in ops:
        kind = op["op"]
        if kind == "settle":
            sid = op["settlement_id"]
            revision = store.current(trace_id).revision
            target = evaluate(contract, store.trajectory(trace_id), trace_id, revision)
            if not target.bookable:
                info = ledger.record_analysis(sid, target)
                print(
                    f"ANALYSIS {sid}: coverage={info['coverage']} "
                    f"window={info['window']} demand={info['demand']} "
                    "(not booked)"
                )
                continue
            # First bill has no predecessor; later ones correct the current head.
            expected = None
            if op.get("expected_previous_bill") is not None:
                expected = op["expected_previous_bill"]
            elif last_settle is not None:
                expected = last_settle
            receipt = ledger.post_target(sid, target, expected_previous_bill=expected)
            _print_receipt(f"BILL {sid}", receipt)
            last_settle = sid
        elif kind == "correct_power":
            file = op.get("file")
            obj = _load(mdir / file) if file else op.get("correction")
            rev = store.revise_json(trace_id, obj)
            # Applying a correction advances the current *power version* but
            # books nothing; amounts move only via an explicit settle.
            ledger.set_current_power(trace_id, rev.revision)
            print(f"POWER corrected to revision {rev.revision} (no amount booked)")
        elif kind == "read_settlement":
            sid = op["settlement_id"]
            receipt = ledger.get_bill(sid)
            if receipt is None:
                print(f"READ {sid}: not found")
            else:
                _print_receipt(f"READ {sid}", receipt)
        else:
            print(f"unknown op {kind!r}", file=sys.stderr)
            return 2

    ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
