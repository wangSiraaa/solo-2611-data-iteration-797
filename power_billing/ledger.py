"""SQLite demand-billing ledger.

Design in one paragraph
-----------------------
The ledger stores, per ``contract_id`` + ``period``, a chain of **posted bills**.
Each posted bill is immutable: it keeps the target charge that was computed for
it, the signed **difference** actually booked when it was posted, and a pointer
to the bill it replaced (``predecessor_id``).  Money movement lives in an
append-only ``entries`` table; each bill contributes exactly one entry equal to
its signed difference.  A ``heads`` table points at the current bill of each
contract+period, so that:

* net(sum of entries) == current head's target charge, always;
* a correction books ``new_target - predecessor_target`` (never the full new
  amount again) and flips the head atomically;
* a predecessor can have at most one successor (``UNIQUE(predecessor_id)``) and
  must equal the current head, so corrections form a single linear chain and
  two concurrent corrections from the same predecessor cannot both advance;
* replaying the same ``settlement_id`` with identical content returns the
  original receipt and changes nothing; a different payload is rejected.

All writes use ``BEGIN IMMEDIATE`` so concurrent writers are serialized by
SQLite; the successor/head checks are performed inside that write transaction.
Rationals are stored as exact numerator/denominator pairs (text), never floats.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from fractions import Fraction
from typing import Any, Iterator, Sequence

from .demand import (
    COVERAGE_COMPLETE,
    SCHEMA,
    Contract,
    TargetCharge,
    require_bookable,
)
from .errors import ConflictError, NotFoundError, ValidationError
from .interval import ZERO, frac_to_json


# --------------------------------------------------------------------------- #
# Exact-value (de)serialization
# --------------------------------------------------------------------------- #
def _pair(x: Fraction) -> tuple[str, str]:
    x = Fraction(x)
    return (str(x.numerator), str(x.denominator))


def _frac(n: Any, d: Any) -> Fraction:
    return Fraction(int(n), int(d))


def period_key(contract_id: str, a: Fraction, b: Fraction) -> str:
    an, ad = _pair(a)
    bn, bd = _pair(b)
    return f"{contract_id}\x1f{an}/{ad}\x1f{bn}/{bd}"


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    period_start_n TEXT NOT NULL, period_start_d TEXT NOT NULL,
    period_end_n TEXT NOT NULL,   period_end_d TEXT NOT NULL,
    window_n TEXT NOT NULL,       window_d TEXT NOT NULL,
    included_n TEXT NOT NULL,     included_d TEXT NOT NULL,
    price_n TEXT NOT NULL,        price_d TEXT NOT NULL,
    PRIMARY KEY (contract_id, revision)
);

CREATE TABLE IF NOT EXISTS current_contracts (
    contract_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS powers (
    trace_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    PRIMARY KEY (trace_id, revision)
);

CREATE TABLE IF NOT EXISTS current_powers (
    trace_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bills (
    settlement_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    period_start_n TEXT NOT NULL, period_start_d TEXT NOT NULL,
    period_end_n TEXT NOT NULL,   period_end_d TEXT NOT NULL,
    coverage TEXT NOT NULL,
    target_revision TEXT NOT NULL,
    power_trace_id TEXT NOT NULL,
    power_revision TEXT NOT NULL,
    charge_n TEXT, charge_d TEXT,           -- this bill's target charge
    predecessor_id TEXT,                    -- replaced relationship
    delta_n TEXT NOT NULL, delta_d TEXT NOT NULL,   -- amount actually booked
    balance_after_n TEXT NOT NULL, balance_after_d TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    product_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL,
    UNIQUE (predecessor_id)
);

CREATE INDEX IF NOT EXISTS bills_chain
    ON bills (contract_id, period_key, created_seq);

CREATE TABLE IF NOT EXISTS entries (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    settlement_id TEXT NOT NULL UNIQUE,     -- exactly one entry per bill
    contract_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    amount_n TEXT NOT NULL, amount_d TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS entries_period
    ON entries (contract_id, period_key, entry_id);

CREATE TABLE IF NOT EXISTS heads (
    contract_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    head_id TEXT NOT NULL,
    PRIMARY KEY (contract_id, period_key)
);

-- Analysis-only results (partial / insufficient / any coverage).  These are
-- never bills, never entries, never heads.
CREATE TABLE IF NOT EXISTS analyses (
    analysis_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    coverage TEXT NOT NULL,
    contract_revision TEXT NOT NULL,
    power_trace_id TEXT NOT NULL,
    power_revision TEXT NOT NULL,
    product_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);
"""


class Ledger:
    """A single SQLite-backed demand-billing ledger connection."""

    def __init__(self, path: str = ":memory:", *, busy_timeout_ms: int = 15000):
        self.path = path
        self._conn = sqlite3.connect(
            path, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(_SCHEMA_SQL)

    # -- lifecycle -------------------------------------------------------- #
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def _write_tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    def _next_seq(self, conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key='seq'").fetchone()
        n = 0 if row is None else int(row["value"])
        n += 1
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('seq', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(n),),
        )
        return n

    # ------------------------------------------------------------------ #
    # Version registry
    # ------------------------------------------------------------------ #
    def register_contract(self, contract: Contract) -> None:
        """Record a contract revision and make it the current one.

        Registering the same revision twice is idempotent; registering an older
        revision simply makes it current again (the caller owns that policy).
        This touches no bills.
        """
        contract.validate()
        a, b = contract.period
        with self._write_tx() as conn:
            conn.execute(
                "INSERT INTO contracts(contract_id, revision, period_start_n, "
                "period_start_d, period_end_n, period_end_d, window_n, window_d, "
                "included_n, included_d, price_n, price_d) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contract_id, revision) DO NOTHING",
                (contract.contract_id, contract.revision, *_pair(a), *_pair(b),
                 *_pair(contract.window_length), *_pair(contract.included_demand),
                 *_pair(contract.price_per_excess_unit)),
            )
            conn.execute(
                "INSERT INTO current_contracts(contract_id, revision) VALUES(?,?) "
                "ON CONFLICT(contract_id) DO UPDATE SET revision=excluded.revision",
                (contract.contract_id, contract.revision),
            )

    def current_contract_revision(self, contract_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT revision FROM current_contracts WHERE contract_id=?",
            (contract_id,),
        ).fetchone()
        return None if row is None else row["revision"]

    def set_current_power(self, trace_id: str, revision: str) -> None:
        """Make ``revision`` the current power version for ``trace_id``.

        This is what applying a power correction calls.  It deliberately books
        **nothing**: a correction only changes which target products are
        currently valid.  Amounts move only through an explicit ``post_target``.
        """
        with self._write_tx() as conn:
            conn.execute(
                "INSERT INTO powers(trace_id, revision) VALUES(?,?) "
                "ON CONFLICT(trace_id, revision) DO NOTHING",
                (trace_id, revision),
            )
            conn.execute(
                "INSERT INTO current_powers(trace_id, revision) VALUES(?,?) "
                "ON CONFLICT(trace_id) DO UPDATE SET revision=excluded.revision",
                (trace_id, revision),
            )

    def current_power_revision(self, trace_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT revision FROM current_powers WHERE trace_id=?", (trace_id,)
        ).fetchone()
        return None if row is None else row["revision"]

    # ------------------------------------------------------------------ #
    # Posting
    # ------------------------------------------------------------------ #
    def post_target(
        self,
        settlement_id: str,
        target: TargetCharge,
        *,
        expected_previous_bill: str | None = None,
    ) -> dict:
        """Post ``target`` as bill ``settlement_id`` and return the receipt.

        * First successful bill of a contract+period: ``expected_previous_bill``
          must be ``None``; the entry equals the full target charge.
        * Correction: ``expected_previous_bill`` must name the current head; the
          entry is ``target.charge - predecessor.charge`` and the head flips.
        * Replay with identical content returns the stored receipt untouched.
        * Any other reuse of an id, stale predecessor, non-current version, or
          non-complete coverage raises and leaves the ledger unchanged.
        """
        if not settlement_id:
            raise ValidationError("settlement_id is required")
        fingerprint = _fingerprint(settlement_id, target, expected_previous_bill)
        a, b = target.period_start, target.period_end
        pkey = period_key(target.contract_id, a, b)

        with self._write_tx() as conn:
            existing = conn.execute(
                "SELECT * FROM bills WHERE settlement_id=?", (settlement_id,)
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ConflictError(
                        f"settlement_id {settlement_id!r} already exists with "
                        "different content"
                    )
                return self._receipt(conn, existing)

            # Only complete products may become successful bills.
            charge = require_bookable(target)
            if target.coverage != COVERAGE_COMPLETE:
                raise ValidationError("only complete coverage may be posted")

            # The product must reflect the *current* contract and power versions.
            cur_c = conn.execute(
                "SELECT revision FROM current_contracts WHERE contract_id=?",
                (target.contract_id,),
            ).fetchone()
            if cur_c is None or cur_c["revision"] != target.contract_revision:
                raise ConflictError(
                    f"target contract revision {target.contract_revision!r} is "
                    f"not current for {target.contract_id!r} "
                    f"(current: {None if cur_c is None else cur_c['revision']!r})"
                )
            cur_p = conn.execute(
                "SELECT revision FROM current_powers WHERE trace_id=?",
                (target.power_trace_id,),
            ).fetchone()
            if cur_p is None or cur_p["revision"] != target.power_revision:
                raise ConflictError(
                    f"target power revision {target.power_revision!r} is not "
                    f"current for trace {target.power_trace_id!r} "
                    f"(current: {None if cur_p is None else cur_p['revision']!r})"
                )

            # The contract's stored period must agree with the product's period.
            crow = conn.execute(
                "SELECT period_start_n, period_start_d, period_end_n, period_end_d "
                "FROM contracts WHERE contract_id=? AND revision=?",
                (target.contract_id, target.contract_revision),
            ).fetchone()
            if crow is None:
                raise ConflictError("contract revision is not registered")
            if (
                _frac(crow["period_start_n"], crow["period_start_d"]) != a
                or _frac(crow["period_end_n"], crow["period_end_d"]) != b
            ):
                raise ConflictError("target period does not match the contract")

            head = conn.execute(
                "SELECT head_id FROM heads WHERE contract_id=? AND period_key=?",
                (target.contract_id, pkey),
            ).fetchone()
            head_id = None if head is None else head["head_id"]

            if expected_previous_bill is None:
                if head_id is not None:
                    raise ConflictError(
                        f"contract {target.contract_id!r} / period already has "
                        f"head {head_id!r}; expected_previous_bill is required"
                    )
                predecessor = None
                prev_target = ZERO
            else:
                if head_id is None:
                    raise ConflictError(
                        "no current bill to correct for this contract/period"
                    )
                if expected_previous_bill != head_id:
                    raise ConflictError(
                        f"expected_previous_bill {expected_previous_bill!r} is "
                        f"not the current head {head_id!r}; re-read and retry"
                    )
                predecessor = conn.execute(
                    "SELECT * FROM bills WHERE settlement_id=?",
                    (expected_previous_bill,),
                ).fetchone()
                if predecessor is None:
                    raise NotFoundError(
                        f"predecessor bill {expected_previous_bill!r} not found"
                    )
                if (predecessor["contract_id"] != target.contract_id
                        or predecessor["period_key"] != pkey):
                    raise ConflictError(
                        "predecessor belongs to a different contract/period"
                    )
                prev_target = _frac(predecessor["charge_n"], predecessor["charge_d"])

            prev_balance = _sum_entries(conn, target.contract_id, pkey)
            if prev_balance != prev_target:
                # Invariant broken means the database was tampered with.
                raise ConflictError(
                    "ledger invariant violated: entry sum != predecessor target"
                )

            delta = charge - prev_target
            new_balance = prev_balance + delta
            if new_balance != charge:
                raise ConflictError("ledger invariant violated: balance != target")
            if predecessor is not None:
                # Structural anti-fork guard in addition to the head check.
                taken = conn.execute(
                    "SELECT settlement_id FROM bills WHERE predecessor_id=?",
                    (expected_previous_bill,),
                ).fetchone()
                if taken is not None:
                    raise ConflictError(
                        f"predecessor {expected_previous_bill!r} already has "
                        f"successor {taken['settlement_id']!r}"
                    )

            seq = self._next_seq(conn)
            # Insert the single money entry first so we can record its id in
            # the immutable bill receipt; a failure below rolls everything back.
            cur = conn.execute(
                "INSERT INTO entries(settlement_id, contract_id, period_key, "
                "amount_n, amount_d, created_seq) VALUES(?,?,?,?,?,?)",
                (settlement_id, target.contract_id, pkey, *_pair(delta), seq),
            )
            entry_id = cur.lastrowid

            product = target.to_json()
            product["settlement_id"] = settlement_id
            product["predecessor_id"] = expected_previous_bill
            product["difference"] = frac_to_json(delta)
            product["entry_id"] = entry_id
            product["balance_after"] = frac_to_json(new_balance)
            product_json = json.dumps(product, sort_keys=True, separators=(",", ":"))

            conn.execute(
                "INSERT INTO bills(settlement_id, contract_id, period_key, "
                "period_start_n, period_start_d, period_end_n, period_end_d, "
                "coverage, target_revision, power_trace_id, power_revision, "
                "charge_n, charge_d, predecessor_id, delta_n, delta_d, "
                "balance_after_n, balance_after_d, fingerprint, product_json, "
                "created_seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    settlement_id, target.contract_id, pkey,
                    *_pair(a), *_pair(b), target.coverage,
                    target.contract_revision, target.power_trace_id,
                    target.power_revision, *_pair(charge), expected_previous_bill,
                    *_pair(delta), *_pair(new_balance), fingerprint, product_json,
                    seq,
                ),
            )
            conn.execute(
                "INSERT INTO heads(contract_id, period_key, head_id) VALUES(?,?,?) "
                "ON CONFLICT(contract_id, period_key) DO UPDATE SET "
                "head_id=excluded.head_id",
                (target.contract_id, pkey, settlement_id),
            )

            # Final defensive check inside the transaction.
            total = _sum_entries(conn, target.contract_id, pkey)
            if total != charge:
                raise ConflictError("ledger invariant violated after posting")

            row = conn.execute(
                "SELECT * FROM bills WHERE settlement_id=?", (settlement_id,)
            ).fetchone()
            return self._receipt(conn, row)

    def record_analysis(self, analysis_id: str, target: TargetCharge) -> dict:
        """Persist an analysis-only product (never a bill, entry, or head).

        Used for partial-coverage and insufficient-evidence results so they can
        be retained and inspected without ever becoming a zero-bill.
        """
        if not analysis_id:
            raise ValidationError("analysis_id is required")
        fingerprint = _fingerprint(analysis_id, target, None)
        pkey = period_key(target.contract_id, target.period_start, target.period_end)
        with self._write_tx() as conn:
            existing = conn.execute(
                "SELECT * FROM analyses WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != fingerprint:
                    raise ConflictError(
                        f"analysis_id {analysis_id!r} already exists with "
                        "different content"
                    )
                return json.loads(existing["product_json"])
            seq = self._next_seq(conn)
            product = target.to_json()
            product["analysis_id"] = analysis_id
            product_json = json.dumps(product, sort_keys=True, separators=(",", ":"))
            conn.execute(
                "INSERT INTO analyses(analysis_id, contract_id, period_key, "
                "coverage, contract_revision, power_trace_id, power_revision, "
                "product_json, fingerprint, created_seq) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (analysis_id, target.contract_id, pkey, target.coverage,
                 target.contract_revision, target.power_trace_id,
                 target.power_revision, product_json, fingerprint, seq),
            )
            return product

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def _receipt(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
        return json.loads(row["product_json"])

    def get_bill(self, settlement_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM bills WHERE settlement_id=?", (settlement_id,)
        ).fetchone()
        return None if row is None else self._receipt(self._conn, row)

    def get_analysis(self, analysis_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT product_json FROM analyses WHERE analysis_id=?", (analysis_id,)
        ).fetchone()
        return None if row is None else json.loads(row["product_json"])

    def current_head(self, contract_id: str, period: tuple[Fraction, Fraction]) -> dict | None:
        pkey = period_key(contract_id, *period)
        row = self._conn.execute(
            "SELECT b.* FROM heads h JOIN bills b ON b.settlement_id=h.head_id "
            "WHERE h.contract_id=? AND h.period_key=?",
            (contract_id, pkey),
        ).fetchone()
        return None if row is None else self._receipt(self._conn, row)

    def successor_of(self, settlement_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM bills WHERE predecessor_id=?", (settlement_id,)
        ).fetchone()
        return None if row is None else self._receipt(self._conn, row)

    def history(self, contract_id: str, period: tuple[Fraction, Fraction]) -> list[dict]:
        pkey = period_key(contract_id, *period)
        rows = self._conn.execute(
            "SELECT * FROM bills WHERE contract_id=? AND period_key=? "
            "ORDER BY created_seq",
            (contract_id, pkey),
        ).fetchall()
        return [self._receipt(self._conn, r) for r in rows]

    def entries(self, contract_id: str, period: tuple[Fraction, Fraction]) -> list[dict]:
        pkey = period_key(contract_id, *period)
        rows = self._conn.execute(
            "SELECT * FROM entries WHERE contract_id=? AND period_key=? "
            "ORDER BY entry_id",
            (contract_id, pkey),
        ).fetchall()
        return [
            {
                "entry_id": r["entry_id"],
                "settlement_id": r["settlement_id"],
                "amount": _frac(r["amount_n"], r["amount_d"]),
            }
            for r in rows
        ]

    def balance(self, contract_id: str, period: tuple[Fraction, Fraction]) -> Fraction:
        return _sum_entries(self._conn, contract_id, period_key(contract_id, *period))

    def verify(self, contract_id: str, period: tuple[Fraction, Fraction]) -> None:
        """Assert that net entries equal the current head's target charge."""
        pkey = period_key(contract_id, *period)
        head = self._conn.execute(
            "SELECT b.charge_n, b.charge_d FROM heads h "
            "JOIN bills b ON b.settlement_id=h.head_id "
            "WHERE h.contract_id=? AND h.period_key=?",
            (contract_id, pkey),
        ).fetchone()
        if head is None:
            raise ConflictError("no current head")
        total = _sum_entries(self._conn, contract_id, pkey)
        target = _frac(head["charge_n"], head["charge_d"])
        if total != target:
            raise ConflictError(f"ledger invariant violated: {total} != {target}")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _sum_entries(conn: sqlite3.Connection, contract_id: str, pkey: str) -> Fraction:
    rows = conn.execute(
        "SELECT amount_n, amount_d FROM entries "
        "WHERE contract_id=? AND period_key=?",
        (contract_id, pkey),
    ).fetchall()
    total = ZERO
    for r in rows:
        total += _frac(r["amount_n"], r["amount_d"])
    return total


def _canonical_target(target: TargetCharge) -> dict:
    d = target.to_json()
    d.pop("settlement_id", None)
    return d


def _fingerprint(settlement_id: str, target: TargetCharge, expected_previous: str | None) -> str:
    """Content fingerprint used for idempotent replay detection.

    It deliberately covers the *bill content* (the target product) and the
    settlement id, but not the ``expected_previous_bill`` hint: a replay may
    legitimately omit or restate that hint and must still return the original
    receipt.  Reusing an id for a different target product is "different
    content" and is rejected.
    """
    payload = {
        "settlement_id": settlement_id,
        "product": _canonical_target(target),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def bill_document(receipt: dict) -> dict:
    """Return exactly the ``demand-bill/v1`` document (safe for bill_reader)."""
    return {
        "schema": SCHEMA,
        "settlement_id": receipt["settlement_id"],
        "coverage": receipt["coverage"],
        "window": receipt.get("window"),
        "demand": receipt.get("demand"),
        "charge": receipt.get("charge"),
        "contract_id": receipt.get("contract_id"),
        "contract_revision": receipt.get("contract_revision"),
        "power_trace_id": receipt.get("power_trace_id"),
        "power_revision": receipt.get("power_revision"),
        "period": receipt.get("period"),
        "window_length": receipt.get("window_length"),
        "predecessor_id": receipt.get("predecessor_id"),
        "difference": receipt.get("difference"),
        "entry_id": receipt.get("entry_id"),
        "balance_after": receipt.get("balance_after"),
        "sources": receipt.get("sources", []),
        "gaps": receipt.get("gaps", []),
    }
