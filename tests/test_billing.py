"""Tests for the exact rational power/demand billing library.

Run with::

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from fractions import Fraction as F
from pathlib import Path

from power_billing import (
    COVERAGE_COMPLETE,
    COVERAGE_INSUFFICIENT,
    COVERAGE_PARTIAL,
    Contract,
    Ledger,
    Segment,
    Trajectory,
    TrajectoryStore,
    evaluate,
)
from power_billing.demand import require_bookable
from power_billing.errors import (
    ConflictError,
    ContractError,
    InsufficientEvidence,
    RevisionError,
    SegmentationError,
)
from power_billing.interval import period_charge
from power_billing.ledger import bill_document

MATERIALS = Path(__file__).resolve().parent.parent / "materials" / "735"


def seg(sid, start, end, power):
    return Segment(sid, F(start), F(end), F(power))


def material_contract():
    return Contract.from_json(json.loads((MATERIALS / "contract.json").read_text()))


def material_power():
    return json.loads((MATERIALS / "power.json").read_text())


def material_p2_segments():
    data = material_power()
    for s in data["segments"]:
        if s["segment_id"] == "S3":
            s["power"] = 9
    return data


class IntervalAndDemandTests(unittest.TestCase):
    def test_material_p1_demand_and_window_start(self):
        traj = Trajectory([seg("S0", 0, 10, 2), seg("S1", 10, 20, 8),
                           seg("S2", 20, 30, 2), seg("S3", 30, 40, 2)])
        best_i, best_s, sources = traj.demand_max(F(0), F(40), F(15))
        self.assertEqual(best_s, F(5))
        self.assertEqual(best_i, F(90))
        self.assertEqual(best_i / F(15), F(6))
        # The optimal window starts strictly between segment breakpoints.
        self.assertNotIn(best_s, [F(0), F(10), F(20), F(30)])
        self.assertEqual([s.segment_id for _, _, s in sources], ["S0", "S1"])

    def test_material_p2_demand_and_charge(self):
        traj = Trajectory([seg("S0", 0, 10, 2), seg("S1", 10, 20, 8),
                           seg("S2", 20, 30, 2), seg("S3", 30, 40, 9)])
        c = material_contract()
        target = evaluate(c, traj, "P", "p2")
        self.assertEqual(target.coverage, COVERAGE_COMPLETE)
        self.assertEqual(target.window, (F(25), F(40)))
        self.assertEqual(target.demand, F(20, 3))
        self.assertEqual(target.charge, F(2))

    def test_p3_reverts_to_zero(self):
        traj = Trajectory([seg("S0", 0, 10, 2), seg("S1", 10, 20, 8),
                           seg("S2", 20, 30, 2), seg("S3", 30, 40, 2)])
        target = evaluate(material_contract(), traj, "P", "p3")
        self.assertEqual(target.demand, F(6))
        self.assertEqual(target.charge, F(0))

    def test_integral_exact_fractional_power(self):
        traj = Trajectory([seg("A", 0, F(3, 2), F(1, 3)),
                           seg("B", F(3, 2), 3, F(2, 7))])
        self.assertEqual(traj.integral(F(0), F(3)), F(1, 2) + F(3, 7))

    def test_window_equals_whole_period(self):
        traj = Trajectory([seg("A", 0, 10, 4), seg("B", 10, 27, 1)])
        best_i, best_s, _ = traj.demand_max(F(0), F(27), F(27))
        self.assertEqual(best_s, F(0))
        self.assertEqual(best_i, F(57))
        self.assertEqual(best_i / 27, F(57, 27))

    def test_flat_peak_tie_takes_earliest_start(self):
        # Constant power 5 everywhere: every window is a tie -> earliest start.
        traj = Trajectory([seg("A", F(1), F(100), F(5))])
        best_i, best_s, _ = traj.demand_max(F(1), F(100), F(10))
        self.assertEqual(best_s, F(1))
        self.assertEqual(best_i, F(50))

    def test_tie_between_two_plateaus_takes_earliest(self):
        # Two identical triangular-ish peaks; earliest window wins.
        traj = Trajectory([
            seg("A", 0, 10, 0), seg("B", 10, 20, 10),
            seg("C", 20, 30, 0), seg("D", 30, 40, 10),
            seg("E", 40, 50, 0),
        ])
        best_i, best_s, _ = traj.demand_max(F(0), F(50), F(10))
        self.assertEqual(best_s, F(10))  # first peak, even though second ties
        self.assertEqual(best_i, F(100))

    def test_truncated_head_and_tail(self):
        # Period starts inside a segment and ends inside another.  The best
        # window is fully inside the high segment B: [100,120) at power 7.
        traj = Trajectory([seg("A", 0, 100, 3), seg("B", 100, 200, 7)])
        best_i, best_s, _ = traj.demand_max(F(5), F(150), F(20))
        self.assertEqual(best_s, F(100))
        self.assertEqual(best_i, F(140))
        # A window that straddles the boundary sees the truncated head/tail.
        self.assertEqual(traj.integral(F(90), F(110)), F(10 * 3 + 10 * 7))

    def test_gap_marks_partial_and_charge_none(self):
        # Gap in [40,50); W=10, so a fully-known window still exists.
        traj = Trajectory([seg("A", 0, 20, 1), seg("B", 20, 40, 4),
                           seg("C", 50, 70, 1)])
        c = Contract("K", "k1", F(0), F(70), F(10), F(0), F(1))
        target = evaluate(c, traj, "T", "r1")
        self.assertEqual(target.coverage, COVERAGE_PARTIAL)
        self.assertIsNone(target.charge)
        self.assertEqual(target.demand, F(4))  # best all-known window
        self.assertEqual(target.window, (F(20), F(30)))
        self.assertEqual(target.gaps, ((F(40), F(50)),))
        with self.assertRaises(InsufficientEvidence):
            require_bookable(target)

    def test_no_known_window_is_insufficient(self):
        # Only 5 minutes of known power at a time, W=10 -> no full window.
        traj = Trajectory([seg("A", 0, 5, 1), seg("B", 10, 15, 2)])
        c = Contract("K", "k1", F(0), F(40), F(10), F(0), F(1))
        target = evaluate(c, traj, "T", "r1")
        self.assertEqual(target.coverage, COVERAGE_INSUFFICIENT)
        self.assertIsNone(target.window)
        self.assertIsNone(target.demand)
        self.assertIsNone(target.charge)

    def test_uncovered_time_is_not_zero_filled_or_extended(self):
        traj = Trajectory([seg("A", 10, 20, 5)])
        # Outside the known segment the integral is zero (nothing invented).
        self.assertEqual(traj.integral(F(0), F(10)), F(0))
        self.assertEqual(traj.integral(F(20), F(30)), F(0))
        self.assertFalse(traj.is_covered(F(0), F(10)))
        self.assertEqual(traj.covered_length(F(0), F(30)), F(10))

    def test_overlap_rejected_and_touching_allowed(self):
        with self.assertRaises(SegmentationError):
            Trajectory([seg("A", 0, 10, 1), seg("B", 5, 15, 1)])
        # Touching is fine.
        Trajectory([seg("A", 0, 10, 1), seg("B", 10, 15, 1)])

    def test_duplicate_id_and_bad_length_rejected(self):
        with self.assertRaises(SegmentationError):
            Trajectory([seg("A", 0, 10, 1), seg("A", 10, 15, 1)])
        with self.assertRaises(SegmentationError):
            Trajectory([seg("A", 5, 5, 1)])

    def test_negative_power_rejected(self):
        with self.assertRaises(SegmentationError):
            Trajectory([seg("A", 0, 10, -1)])

    def test_period_charge_formula(self):
        self.assertEqual(period_charge(F(20, 3), F(6), F(3)), F(2))
        self.assertEqual(period_charge(F(5), F(6), F(3)), F(0))
        self.assertEqual(period_charge(F(6), F(6), F(3)), F(0))


class RevisionTests(unittest.TestCase):
    def test_identity_replacement_is_atomic_and_validated(self):
        store = TrajectoryStore()
        store.register_json(material_power())
        rev = store.revise_json("P", {"revision": "p2",
                                      "replace_segment": {"segment_id": "S3",
                                                          "start": 30, "end": 40,
                                                          "power": 9}})
        self.assertEqual(rev.revision, "p2")
        self.assertEqual(store.trajectory("P").integral(F(30), F(40)), F(90))

    def test_invalid_revision_leaves_store_unchanged(self):
        store = TrajectoryStore()
        store.register_json(material_power())
        before = store.current("P")
        # Make S3 overlap S2 by moving its start to 25.
        with self.assertRaises(SegmentationError):
            store.revise_json("P", {"revision": "bad",
                                    "replace_segment": {"segment_id": "S3",
                                                        "start": 25, "end": 40,
                                                        "power": 9}})
        self.assertIs(store.current("P"), before)
        self.assertEqual(store.current("P").revision, "p1")

    def test_multi_segment_batch_all_or_nothing(self):
        store = TrajectoryStore()
        store.register_json(material_power())
        with self.assertRaises(SegmentationError):
            store.revise_json("P", {"revision": "bad", "replace_segments": [
                {"segment_id": "S0", "start": 0, "end": 10, "power": 5},
                {"segment_id": "S3", "start": 25, "end": 40, "power": 9},
            ]})
        self.assertEqual(store.current("P").revision, "p1")

    def test_replace_unknown_segment_rejected(self):
        store = TrajectoryStore()
        store.register_json(material_power())
        with self.assertRaises(RevisionError):
            store.revise_json("P", {"revision": "x",
                                    "replace_segment": {"segment_id": "NOPE",
                                                        "start": 0, "end": 1,
                                                        "power": 1}})

    def test_power_correction_alone_books_nothing(self):
        store = TrajectoryStore()
        store.register_json(material_power())
        c = material_contract()
        ledger = Ledger()
        ledger.register_contract(c)
        ledger.set_current_power("P", "p1")
        ledger.post_target("B1", evaluate(c, store.trajectory("P"), "P", "p1"))
        # Apply the correction (new version current) but do not settle.
        rev = store.revise_json("P", json.loads((MATERIALS / "correction.json").read_text()))
        ledger.set_current_power("P", rev.revision)
        self.assertEqual(ledger.balance("K", (F(0), F(40))), F(0))
        self.assertEqual(ledger.current_head("K", (F(0), F(40)))["settlement_id"], "B1")


class LedgerMaterialScenarioTests(unittest.TestCase):
    """The exact B1/B2/B3 scenario from the material."""

    def setUp(self):
        self.store = TrajectoryStore()
        self.store.register_json(material_power())
        self.contract = material_contract()
        self.ledger = Ledger()
        self.ledger.register_contract(self.contract)
        self.ledger.set_current_power("P", "p1")
        self.period = (F(0), F(40))

    def _target(self):
        rev = self.store.current("P").revision
        return evaluate(self.contract, self.store.trajectory("P"), "P", rev)

    def test_full_scenario_b1_b2_b3(self):
        led, per = self.ledger, self.period
        b1 = led.post_target("B1", self._target())
        self.assertEqual(b1["charge"], {"n": 0, "d": 1})
        self.assertEqual(b1["coverage"], "complete")
        self.assertEqual(led.balance("K", per), F(0))
        b1_snapshot = led.get_bill("B1")

        # p2: correct S3 9; B2 only books +2.
        rev = self.store.revise_json(
            "P", json.loads((MATERIALS / "correction.json").read_text()))
        led.set_current_power("P", rev.revision)
        b2 = led.post_target("B2", self._target(), expected_previous_bill="B1")
        self.assertEqual(b2["difference"], {"n": 2, "d": 1})
        self.assertEqual(b2["predecessor_id"], "B1")
        self.assertEqual(b2["charge"], {"n": 2, "d": 1})
        self.assertEqual(led.balance("K", per), F(2))
        self.assertEqual(led.current_head("K", per)["settlement_id"], "B2")

        # Replaying B2 changes nothing.
        replay = led.post_target("B2", self._target(), expected_previous_bill="B1")
        self.assertEqual(replay, b2)
        self.assertEqual(led.balance("K", per), F(2))
        self.assertEqual(len(led.entries("K", per)), 2)

        # p3: revert S3 to 2; B3 books -2.
        rev3 = self.store.revise_json("P", {"revision": "p3",
                                            "replace_segment": {"segment_id": "S3",
                                                                "start": 30, "end": 40,
                                                                "power": 2}})
        led.set_current_power("P", rev3.revision)
        b3 = led.post_target("B3", self._target(), expected_previous_bill="B2")
        self.assertEqual(b3["difference"], {"n": -2, "d": 1})
        self.assertEqual(b3["charge"], {"n": 0, "d": 1})
        self.assertEqual(led.balance("K", per), F(0))
        self.assertEqual(led.current_head("K", per)["settlement_id"], "B3")

        # Old bills are unchanged and remain readable.
        self.assertEqual(led.get_bill("B1"), b1_snapshot)
        self.assertEqual(led.get_bill("B2")["charge"], {"n": 2, "d": 1})
        self.assertEqual(led.get_bill("B2")["difference"], {"n": 2, "d": 1})

        # Entries: 0, +2, -2 -> sum 0 == head target 0.
        amounts = [e["amount"] for e in led.entries("K", per)]
        self.assertEqual(amounts, [F(0), F(2), F(-2)])
        led.verify("K", per)

        # Replaced relationships are fixed and queryable.
        self.assertEqual(led.successor_of("B1")["settlement_id"], "B2")
        self.assertEqual(led.successor_of("B2")["settlement_id"], "B3")
        self.assertIsNone(led.successor_of("B3"))

    def test_conflicting_bill_id_rejected(self):
        self.ledger.post_target("B1", self._target())
        # Same id, different contract content -> reject.
        other = Contract("OTHER", "o1", F(0), F(40), F(15), F(0), F(1))
        with self.assertRaises(ConflictError):
            self.ledger.post_target("B1", evaluate(other, self.store.trajectory("P"),
                                                   "P", "p1"))

    def test_stale_predecessor_rejected(self):
        led, per = self.ledger, self.period
        led.post_target("B1", self._target())
        rev = self.store.revise_json(
            "P", json.loads((MATERIALS / "correction.json").read_text()))
        led.set_current_power("P", rev.revision)
        led.post_target("B2", self._target(), expected_previous_bill="B1")
        # Another correction claiming B1 (no longer head) must fail.
        rev2 = self.store.revise_json("P", {"revision": "p3",
                                            "replace_segment": {"segment_id": "S3",
                                                                "start": 30, "end": 40,
                                                                "power": 2}})
        led.set_current_power("P", rev2.revision)
        with self.assertRaises(ConflictError):
            led.post_target("B2b", self._target(), expected_previous_bill="B1")
        self.assertEqual(led.balance("K", per), F(2))
        self.assertEqual(led.current_head("K", per)["settlement_id"], "B2")

    def test_first_bill_requires_no_predecessor_and_vice_versa(self):
        led, per = self.ledger, self.period
        with self.assertRaises(ConflictError):
            led.post_target("B1", self._target(), expected_previous_bill="NOPE")
        led.post_target("B1", self._target())
        # A second first-bill for the same period must be told it needs a
        # predecessor.
        rev = self.store.revise_json(
            "P", json.loads((MATERIALS / "correction.json").read_text()))
        led.set_current_power("P", rev.revision)
        with self.assertRaises(ConflictError):
            led.post_target("B2", self._target())

    def test_target_must_match_current_versions(self):
        # Register a newer contract revision, then try to post a stale target.
        led = self.ledger
        stale = self._target()  # contract revision k1
        new_contract = Contract("K", "k2", F(0), F(40), F(15), F(6), F(3))
        led.register_contract(new_contract)
        with self.assertRaises(ConflictError):
            led.post_target("B1", stale)
        self.assertIsNone(led.current_head("K", (F(0), F(40))))

    def test_partial_and_insufficient_never_become_bills(self):
        ledger = Ledger()
        c = Contract("K", "k1", F(0), F(70), F(10), F(0), F(1))
        ledger.register_contract(c)
        ledger.set_current_power("T", "r1")
        traj = Trajectory([seg("A", 0, 20, 1), seg("B", 20, 40, 4),
                           seg("C", 50, 70, 1)])
        partial = evaluate(c, traj, "T", "r1")
        self.assertEqual(partial.coverage, COVERAGE_PARTIAL)
        with self.assertRaises(InsufficientEvidence):
            ledger.post_target("X", partial)
        # Analysis persists but is not a bill / head / entry.
        info = ledger.record_analysis("A1", partial)
        self.assertEqual(info["coverage"], "partial")
        self.assertIsNone(info["charge"])
        self.assertIsNone(ledger.get_bill("A1"))
        self.assertIsNone(ledger.current_head("K", (F(0), F(70))))
        self.assertEqual(ledger.entries("K", (F(0), F(70))), [])

    def test_replay_without_predecessor_hint_returns_original(self):
        led, per = self.ledger, self.period
        led.post_target("B1", self._target())
        rev = self.store.revise_json(
            "P", json.loads((MATERIALS / "correction.json").read_text()))
        led.set_current_power("P", rev.revision)
        b2 = led.post_target("B2", self._target(), expected_previous_bill="B1")
        # Replay the same B2 target but omit the hint -> identical receipt.
        replay = led.post_target("B2", self._target())
        self.assertEqual(replay, b2)
        self.assertEqual(led.balance("K", per), F(2))
        self.assertEqual(len(led.entries("K", per)), 2)

    def test_replay_with_different_content_is_rejected(self):
        led = self.ledger
        led.post_target("B1", self._target())  # charge 0 at p1
        rev = self.store.revise_json(
            "P", json.loads((MATERIALS / "correction.json").read_text()))
        led.set_current_power("P", rev.revision)
        # Reuse B1 for a different (p2, charge 2) product -> conflict.
        with self.assertRaises(ConflictError):
            led.post_target("B1", self._target())
        self.assertEqual(led.balance("K", self.period), F(0))

    def test_zero_difference_keeps_new_head_without_growing_balance(self):
        led, per = self.ledger, self.period
        b1 = led.post_target("B1", self._target())  # charge 0
        # A "correction" that leaves the target at 0 (re-register same power as
        # a new revision id) still advances the head with a zero entry.
        rev = self.store.revise_json("P", {"revision": "p1b",
                                           "replace_segment": {"segment_id": "S3",
                                                               "start": 30, "end": 40,
                                                               "power": 2}})
        led.set_current_power("P", rev.revision)
        b2 = led.post_target("B2", self._target(), expected_previous_bill="B1")
        self.assertEqual(b2["difference"], {"n": 0, "d": 1})
        self.assertEqual(led.balance("K", per), F(0))
        self.assertEqual(led.current_head("K", per)["settlement_id"], "B2")
        self.assertEqual(led.entries("K", per)[-1]["amount"], F(0))

    def test_reopening_a_database_preserves_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "billing.db")
            led = Ledger(path)
            led.register_contract(self.contract)
            led.set_current_power("P", "p1")
            led.post_target("B1", self._target())
            led.close()

            # Reopen with a fresh store/contract/ledger.
            store = TrajectoryStore()
            store.register_json(material_power())
            led2 = Ledger(path)
            led2.register_contract(self.contract)
            led2.set_current_power("P", "p1")
            self.assertEqual(led2.balance("K", self.period), F(0))
            self.assertEqual(led2.get_bill("B1")["charge"], {"n": 0, "d": 1})
            self.assertEqual(led2.current_head("K", self.period)["settlement_id"], "B1")

            # Replaying B1 after reopen is an idempotent no-op.
            target = evaluate(self.contract, store.trajectory("P"), "P", "p1")
            before = led2.entries("K", self.period)
            replay = led2.post_target("B1", target)
            self.assertEqual(replay["settlement_id"], "B1")
            self.assertEqual(led2.entries("K", self.period), before)

            # And a correction still works across the reopen.
            rev = store.revise_json(
                "P", json.loads((MATERIALS / "correction.json").read_text()))
            led2.set_current_power("P", rev.revision)
            b2 = led2.post_target("B2", evaluate(self.contract,
                                                 store.trajectory("P"), "P", rev.revision),
                                  expected_previous_bill="B1")
            self.assertEqual(b2["difference"], {"n": 2, "d": 1})
            led2.close()


class RandomizedExactnessTests(unittest.TestCase):
    """Cross-check the optimized paths against straightforward definitions."""

    def test_prefix_integral_matches_piecewise_sum(self):
        import random

        rng = random.Random(1234)
        for _ in range(200):
            t = F(0)
            segs = []
            for i in range(rng.randint(1, 8)):
                t += F(rng.randint(0, 5), rng.choice([1, 2, 3]))
                length = F(rng.randint(1, 7), rng.choice([1, 2, 3]))
                power = F(rng.randint(0, 9), rng.choice([1, 2, 3]))
                segs.append(seg(f"S{i}", t, t + length, power))
                t += length
            traj = Trajectory(segs)
            for _ in range(20):
                a = F(rng.randint(0, 60), rng.choice([1, 2, 3]))
                b = a + F(rng.randint(0, 20), rng.choice([1, 2, 3]))
                naive = sum(
                    (s.power * (min(b, s.end) - max(a, s.start))
                     for s in segs if max(a, s.start) < min(b, s.end)),
                    F(0),
                )
                self.assertEqual(traj.integral(a, b), naive)
                known, total, _ = traj.coverage(a, b)
                self.assertEqual(total, max(b - a, F(0)))
                self.assertLessEqual(known, total)

    def test_demand_max_beats_all_sampled_starts(self):
        import random

        rng = random.Random(99)
        for _ in range(100):
            t = F(0)
            segs = []
            for i in range(rng.randint(1, 7)):
                length = F(rng.randint(1, 6))
                segs.append(seg(f"S{i}", t, t + length, F(rng.randint(0, 8))))
                t += length
            period_end = t
            w = F(rng.randint(1, max(1, int(period_end))))
            if w > period_end:
                continue
            traj = Trajectory(segs)
            best_i, best_s, _ = traj.demand_max(F(0), period_end, w)
            self.assertEqual(traj.integral(best_s, best_s + w), best_i)
            self.assertGreaterEqual(best_s, F(0))
            self.assertLessEqual(best_s + w, period_end)
            # No sampled start (fine grid across the domain) beats the optimum.
            steps = 40
            for k in range(steps + 1):
                s = period_end * k / steps
                s = min(max(s, F(0)), period_end - w)
                self.assertLessEqual(traj.integral(s, s + w), best_i)


class ScaleTests(unittest.TestCase):
    def test_1000_segments_200_contracts_500_bills(self):
        import random
        import time

        rng = random.Random(7)
        t = F(0)
        segs = []
        for i in range(1000):
            length = F(rng.randint(1, 4))
            segs.append(seg(f"S{i}", t, t + length, F(rng.randint(0, 20))))
            t += length
        traj = Trajectory(segs)
        contract = Contract("K", "k1", F(0), t, F(10), F(5), F(3))

        start = time.perf_counter()
        for _ in range(200):
            target = evaluate(contract, traj, "P", "r1")
        search_elapsed = time.perf_counter() - start
        self.assertEqual(target.coverage, COVERAGE_COMPLETE)

        ledger = Ledger()
        ledger.register_contract(contract)
        ledger.set_current_power("P", "r1")
        start = time.perf_counter()
        # 500 distinct contract/periods, one bill each.
        for i in range(500):
            c = Contract(f"C{i}", "r1", F(0), t, F(10), F(5), F(3))
            ledger.register_contract(c)
            ledger.set_current_power("P", "r1")
            ledger.post_target(f"B{i}", evaluate(c, traj, "P", "r1"))
        bill_elapsed = time.perf_counter() - start

        # Generous ceilings: this is about catching accidental O(n^2) blowups.
        self.assertLess(search_elapsed, 10.0)
        self.assertLess(bill_elapsed, 20.0)
        self.assertEqual(ledger.balance("C0", (F(0), t)), target.charge)


class ConcurrencyTests(unittest.TestCase):
    """Two connections racing to correct the same predecessor head (B2)."""

    def test_only_one_concurrent_correction_wins(self):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "billing.db")
            contract = material_contract()
            period = (F(0), F(40))

            # Build B1 (p1, charge 0) and B2 (p2, charge 2, delta +2).
            s = TrajectoryStore()
            s.register_json(material_power())
            setup = Ledger(path)
            setup.register_contract(contract)
            setup.set_current_power("P", "p1")
            setup.post_target("B1", evaluate(contract, s.trajectory("P"), "P", "p1"))
            s.revise_json("P", json.loads((MATERIALS / "correction.json").read_text()))
            setup.set_current_power("P", "p2")
            setup.post_target("B2", evaluate(contract, s.trajectory("P"), "P", "p2"),
                              expected_previous_bill="B1")
            self.assertEqual(setup.balance("K", period), F(2))

            # p3 reverts S3 to 2 -> target charge 0; both correctors race to
            # book the -2 correction on top of B2.
            s.revise_json("P", {"revision": "p3",
                                "replace_segment": {"segment_id": "S3",
                                                    "start": 30, "end": 40,
                                                    "power": 2}})
            setup.set_current_power("P", "p3")
            setup.close()

            def make_target():
                st = TrajectoryStore()
                data = material_p2_segments()
                for segd in data["segments"]:
                    if segd["segment_id"] == "S3":
                        segd["power"] = 2
                st.register_json({**data, "revision": "p3"})
                return evaluate(contract, st.trajectory("P"), "P", "p3")

            results: dict[str, object] = {}
            barrier = threading.Barrier(2, timeout=10)

            def corrector(name: str):
                led = Ledger(path)
                try:
                    target = make_target()
                    barrier.wait()  # maximize overlap
                    receipt = led.post_target(name, target, expected_previous_bill="B2")
                    results[name] = ("ok", receipt["difference"])
                except ConflictError as exc:
                    results[name] = ("conflict", str(exc))
                finally:
                    led.close()

            threads = [threading.Thread(target=corrector, args=(n,))
                       for n in ("C1", "C2")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            oks = [n for n, r in results.items() if r[0] == "ok"]
            conflicts = [n for n, r in results.items() if r[0] == "conflict"]
            self.assertEqual(len(oks), 1, results)
            self.assertEqual(len(conflicts), 1, results)
            self.assertEqual(results[oks[0]][1], {"n": -2, "d": 1})

            led = Ledger(path)
            # Exactly one -2 was booked, not two: [0, +2, -2] and balance 0.
            amounts = [e["amount"] for e in led.entries("K", period)]
            self.assertEqual(amounts, [F(0), F(2), F(-2)])
            self.assertEqual(led.balance("K", period), F(0))
            self.assertEqual(led.current_head("K", period)["settlement_id"], oks[0])
            led.verify("K", period)

            # The loser re-reads the new head and can only post a zero delta.
            loser = conflicts[0]
            target = make_target()
            retry = led.post_target(loser, target, expected_previous_bill=oks[0])
            self.assertEqual(retry["difference"], {"n": 0, "d": 1})
            self.assertEqual(retry["predecessor_id"], oks[0])
            self.assertEqual(led.balance("K", period), F(0))
            self.assertEqual(led.current_head("K", period)["settlement_id"], loser)
            led.close()


class BillReaderCompatTests(unittest.TestCase):
    def test_bill_document_matches_reader_requirements(self):
        traj = Trajectory([seg("S0", 0, 10, 2), seg("S1", 10, 20, 8),
                           seg("S2", 20, 30, 2), seg("S3", 30, 40, 9)])
        c = material_contract()
        target = evaluate(c, traj, "P", "p2")
        led = Ledger()
        led.register_contract(c)
        led.set_current_power("P", "p2")
        receipt = led.post_target("B2", target)
        doc = bill_document(receipt)
        self.assertEqual(doc["schema"], "demand-bill/v1")
        self.assertEqual(doc["coverage"], "complete")
        for key in ("window", "demand", "charge"):
            self.assertIn(key, doc)
            self.assertIsInstance(doc[key], (dict, list))
        # Fraction objects are {n, d} with positive denominator.
        self.assertEqual(doc["demand"], {"n": 20, "d": 3})
        self.assertEqual(doc["charge"], {"n": 2, "d": 1})
        self.assertEqual(doc["window"], [{"n": 25, "d": 1}, {"n": 40, "d": 1}])
        json.dumps(doc)  # must be JSON-serializable

    def test_insufficient_document_shape(self):
        c = Contract("K", "k1", F(0), F(40), F(10), F(0), F(1))
        traj = Trajectory([seg("A", 0, 5, 1)])
        target = evaluate(c, traj, "T", "r1")
        doc = target.to_json()
        self.assertEqual(doc["coverage"], "insufficient")
        self.assertIsNone(doc["window"])
        self.assertIsNone(doc["demand"])
        self.assertIsNone(doc["charge"])


if __name__ == "__main__":
    unittest.main()
