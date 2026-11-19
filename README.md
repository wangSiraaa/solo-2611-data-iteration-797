# Exact interval-power & demand-billing library

Python 3.11, standard library only (`fractions`, `sqlite3`). All arithmetic is
exact rational arithmetic; JSON floats are rejected rather than coerced.

Two independent layers:

| layer | module | responsibility |
|-------|--------|----------------|
| power / demand | `power_billing/interval.py`, `demand.py`, `power.py` | exact interval products, revisioned trajectories, demand search, target-charge products |
| billing ledger | `power_billing/ledger.py` | immutable posted bills, append-only entries, linear correction chain, current head |

The layers are intentionally independent: a *target product* is a pure function
of `(contract, power trajectory)`; the ledger never recomputes demand. A power
correction changes only which target products are valid — **it never books
money.** Money moves only through an explicit `post_target`.

## Run

```bash
python3 -m power_billing.cli materials/735          # replay the material ops
python3 -m unittest discover -s tests -v            # 36 tests
python3 materials/735/bill_reader.py bill.json      # read a demand-bill/v1 doc
```

`cli.py` prints `BILL B1 …`, `POWER corrected … (no amount booked)`, etc. The
receipts it emits are accepted by the supplied `bill_reader.py`
(e.g. `B1 complete 5:20 6 0`).

## Layer 1 — exact interval power

A trajectory is a set of half-open segments `[start, end)` with a stable
`segment_id`, positive length and a non-negative rational power. Segments may
have gaps but never overlap; touching is allowed.

* `Trajectory.integral(a, b)` — exact integral of the **known** power over
  `[a,b)`. Gaps contribute nothing (never zero-filled, never extrapolated); use
  `coverage()` / `is_covered()` to distinguish "unknown" from "zero".
* `Trajectory.coverage(a, b)` — `(known_length, total_length, gaps)`, with the
  maximal uncovered sub-intervals, i.e. the gap provenance.
* `Trajectory.demand_max(a, b, W)` — the maximum average known power over a full
  window of length `W`, the earliest optimal window start, and the window's
  source segments.

### Why a non-breakpoint start is required

For a piecewise-constant left-continuous curve, the window integral
`I(s) = ∫_s^{s+W} P` is continuous and piecewise-linear in `s`, with slope
`P(s+W) − P(s)`. The slope can change only where `s` or `s+W` crosses a power
breakpoint. So `I` is linear between **events**
`{breakpoints} ∪ {breakpoint − W}` and its maximum on each linear piece is at an
endpoint. The candidate set is therefore

```
{events in [a, b−W]} ∪ {a, b−W}
```

(the last two are the feasible-segment endpoints that clip the search to
`[a, b−W]`). We evaluate every candidate exactly and take strict-greater, so
ties resolve to the earliest start. This finds the material p2 optimum
`[25,40)` with demand `20/3`, which a breakpoint-only search (`0/10/20`) misses.

### Complexity and revision strategy (chosen explicitly)

* `integral` is O(log n) via a prefix-integral array + binary search.
* `is_covered` is O(log n) via the sorted interior-gap list.
* `demand_max` is O(E · log n) = O(n log n), where `E ≈ 2n` events, plus O(n)
  for the final source decomposition. It is exact and never uses a fixed grid.
* Candidates depend only on `W`, so repeated queries at the same `W` reuse the
  same event geometry; different `W` recompute shifted events. Revisions rebuild
  the prefix array in O(n). We deliberately optimised for **revising cheaply and
  staying exact** rather than for caching per-`W` slope scans; at the stated
  scale (≤1000 segments) an O(n log n) exact search is comfortably fast (see
  `ScaleTests`, 200 evaluations well under a second).

### Revision

`TrajectoryStore.revise_json(...)` replaces segments **by identity** (every
replacement must name an existing `segment_id`), validates the whole candidate
trajectory (unique ids, positive lengths, non-overlap), and commits only on
success. A batch with any illegal result leaves the store byte-for-byte
unchanged.

## Layer 2 — demand-bill-v1 products

`evaluate(contract, trajectory, trace, revision)` returns a `TargetCharge`:

| `coverage` | meaning | `window` / `demand` | `charge` |
|------------|---------|---------------------|----------|
| `complete` | entire `[a,b)` covered by known segments | best window | `max(0,D−D0)×q` |
| `partial` | gaps in `[a,b)` but some full known window exists | best **known** window | `null` |
| `insufficient` | no fully-known window of length `W` | `null` | `null` |

Only `complete` is bookable. A partial result is explicitly *not* the
full-period maximum and can never become a successful (zero or non-zero) bill —
it may only be stored via `record_analysis`, which creates no bill, entry, or
head. Insufficient evidence likewise never yields a zero-charge success.

The `demand-bill/v1` document (see `bill_document`) preserves
`settlement_id`, `coverage`, `window`, `demand`, `charge` in the required
fraction-object form, and additionally carries `predecessor_id`, `difference`,
`entry_id`, `balance_after`, `sources`, `gaps`, versions and period.

## Layer 3 — the ledger

State per `contract_id` + exact `period`:

* `bills` — immutable rows: target `charge`, booked `delta`, `predecessor_id`,
  `balance_after`, full receipt JSON. One row per `settlement_id`.
* `entries` — append-only borrow/credit entries, exactly one per bill, amount =
  the bill's `delta`.
* `heads` — the current bill for each `contract_id` + `period`.

Rules enforced inside a single `BEGIN IMMEDIATE` transaction:

1. **First bill** — no `expected_previous_bill`; `delta = target`; balance from 0.
2. **Correction** — `expected_previous_bill` must equal the current head; the
   booked amount is `new_target − predecessor_target` (never the full new
   amount); the head flips. Zero difference still advances the head and adds a
   zero entry.
3. **No fork** — `UNIQUE(predecessor_id)` plus the head check means a
   predecessor can have at most one successor; of two concurrent corrections
   from the same head, at most one commits, the other gets `ConflictError` and
   must re-read.
4. **Versions** — the product's contract revision and power revision must equal
   the registered current ones, and the product's period must match the stored
   contract.
5. **Idempotency** — replaying a `settlement_id` with the same target product
   returns the stored receipt and writes nothing; a different product is
   rejected as different content.
6. **Invariant** — after every write, `net(entries) == head.target_charge`;
   `verify()` re-checks it. Any failure rolls back the whole transaction, leaving
   no delta, partial bill, or wrong head.

### Concurrency model and its cost

Serialization is by SQLite's write lock: `BEGIN IMMEDIATE` at the start of each
posting, so all head/predecessor/version checks and the writes see a consistent
snapshot and cannot interleave. The cost is that writers to the same database
serialize; readers continue concurrently (WAL mode for file databases). This
was chosen over optimistic "validate-then-commit with a version recheck" because
it is simpler to prove correct and the workload is write-light. `busy_timeout`
(default 15 s) makes a losing writer wait rather than fail spuriously; a genuine
conflict (stale head) is reported as `ConflictError`.

## Verified material scenario

| step | result |
|------|--------|
| p1, B1 | window `[5,20)`, demand `6`, target `0`, balance `0` |
| p2 (S3→9), B2 `expected_previous_bill=B1` | window `[25,40)`, demand `20/3`, target `2`, **delta +2**, balance `2` |
| replay B2 | identical receipt, balance still `2`, no new entry |
| p3 (S3→2), B3 `expected_previous_bill=B2` | demand `6`, target `0`, **delta −2**, balance `0` |
| B1/B2 after corrections | read back unchanged |
| two corrections from B2 | one commits a single −2, the other conflicts; entries `[0,+2,−2]`, not two extra −2 entries |

Edge cases covered by tests: `W` = whole period, flat-peak ties (earliest
start), head/tail truncation, gaps (partial), no known window (insufficient),
overlap rejection, all-or-nothing batch revision, zero-difference head advance,
idempotent replay, conflicting id reuse, and reopening a file database.

## Requirement → test map

| requirement | test |
|---|---|
| exact interval integral; gaps not zero-filled | `test_prefix_integral_matches_piecewise_sum`, `test_uncovered_time_is_not_zero_filled_or_extended` |
| optimal window may start between measurements | `test_material_p1_demand_and_window_start`, `test_material_p2_demand_and_charge` |
| `W` = whole period | `test_window_equals_whole_period` |
| flat/tie → earliest start | `test_flat_peak_tie_takes_earliest_start`, `test_tie_between_two_plateaus_takes_earliest` |
| head/tail truncation | `test_truncated_head_and_tail` |
| gaps → partial, charge null | `test_gap_marks_partial_and_charge_none` |
| no known window → insufficient | `test_no_known_window_is_insufficient` |
| reject overlap; identity revision atomic | `test_overlap_rejected_and_touching_allowed`, `test_invalid_revision_leaves_store_unchanged`, `test_multi_segment_batch_all_or_nothing` |
| correction alone books nothing | `test_power_correction_alone_books_nothing` |
| full B1/B2/B3 scenario | `test_full_scenario_b1_b2_b3` |
| idempotent replay / conflicting content | `test_replay_without_predecessor_hint_returns_original`, `test_replay_with_different_content_is_rejected`, `test_conflicting_bill_id_rejected` |
| stale predecessor rejected | `test_stale_predecessor_rejected` |
| zero difference advances head, no balance | `test_zero_difference_keeps_new_head_without_growing_balance` |
| two connections, one winner | `test_only_one_concurrent_correction_wins` |
| reopen persisted database | `test_reopening_a_database_preserves_state` |
| scale (1000 seg / 200 contracts / 500 bills) | `test_1000_segments_200_contracts_500_bills` |
