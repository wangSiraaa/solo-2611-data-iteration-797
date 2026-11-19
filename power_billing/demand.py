"""Demand contracts and the exact target-charge products they produce.

A *target product* is a pure function of ``(contract, power trajectory)`` for a
fixed settlement period.  It is the only thing the ledger is ever allowed to
book: the ledger never recomputes demand, and a power correction never books
anything by itself -- it only changes which target products are valid.

Terminology used throughout the codebase (see README):

* **coverage == "complete"** -- the whole period ``[a,b)`` is covered by known
  segments; ``coverage == "complete"`` and ``charge`` is ``max(0,D-D0)*q``.
* **coverage == "partial"**  -- the period has gaps but at least one fully-known
  window of length ``W`` exists.  We may report the best *known* window, but the
  bill must be marked partial and its ``charge`` is ``null``; it can never be a
  successful zero (or non-zero) charge.
* **coverage == "insufficient"** -- no fully-known window exists.  ``window`` and
  ``demand`` are ``null`` and ``charge`` is ``null``.  Evidence failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .errors import ContractError, InsufficientEvidence, ValidationError
from .interval import Trajectory, frac_to_json, period_charge
from .numbers import parse_frac

SCHEMA = "demand-bill/v1"

COVERAGE_COMPLETE = "complete"
COVERAGE_PARTIAL = "partial"
COVERAGE_INSUFFICIENT = "insufficient"


def _as_fraction(node: Any, field: str) -> Fraction:
    try:
        return parse_frac(node, field)
    except ValidationError as exc:
        raise ContractError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class Contract:
    contract_id: str
    revision: str
    period_start: Fraction
    period_end: Fraction
    window_length: Fraction
    included_demand: Fraction
    price_per_excess_unit: Fraction

    @property
    def period(self) -> tuple[Fraction, Fraction]:
        return self.period_start, self.period_end

    @classmethod
    def from_json(cls, obj: dict) -> "Contract":
        try:
            a, b = obj["period"]
        except (KeyError, ValueError, TypeError) as exc:
            raise ContractError("period must be a [start, end] pair") from exc
        c = cls(
            contract_id=str(obj["contract_id"]),
            revision=str(obj["revision"]),
            period_start=_as_fraction(a, "period start"),
            period_end=_as_fraction(b, "period end"),
            window_length=_as_fraction(obj["window_length"], "window_length"),
            included_demand=_as_fraction(obj["included_demand"], "included_demand"),
            price_per_excess_unit=_as_fraction(
                obj["price_per_excess_unit"], "price_per_excess_unit"
            ),
        )
        c.validate()
        return c

    def validate(self) -> None:
        if not self.contract_id:
            raise ContractError("contract_id is required")
        if not (self.period_start < self.period_end):
            raise ContractError("period must have positive length")
        if self.window_length <= 0:
            raise ContractError("window_length must be positive")
        if self.period_end - self.period_start < self.window_length:
            raise ContractError("period length must be >= window_length")
        if self.included_demand < 0:
            raise ContractError("included_demand must be non-negative")
        if self.price_per_excess_unit < 0:
            raise ContractError("price_per_excess_unit must be non-negative")

    def version_key(self) -> tuple[str, str]:
        return (self.contract_id, self.revision)

    def to_json(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "revision": self.revision,
            "period": [frac_to_json(self.period_start), frac_to_json(self.period_end)],
            "window_length": frac_to_json(self.window_length),
            "included_demand": frac_to_json(self.included_demand),
            "price_per_excess_unit": frac_to_json(self.price_per_excess_unit),
        }


@dataclass(frozen=True, slots=True)
class TargetCharge:
    """The target product for one ``contract_id`` + ``period``.

    ``charge`` is a :class:`Fraction` only when ``coverage == "complete"``;
    otherwise it is ``None`` and nothing may be booked.
    """

    contract_id: str
    contract_revision: str
    power_trace_id: str
    power_revision: str
    period_start: Fraction
    period_end: Fraction
    window_length: Fraction
    coverage: str
    window: tuple[Fraction, Fraction] | None
    demand: Fraction | None
    charge: Fraction | None
    sources: tuple[tuple[Fraction, Fraction, str], ...] = ()
    gaps: tuple[tuple[Fraction, Fraction], ...] = ()

    @property
    def bookable(self) -> bool:
        return self.coverage == COVERAGE_COMPLETE and self.charge is not None

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "settlement_id": None,  # filled in by the ledger
            "contract_id": self.contract_id,
            "contract_revision": self.contract_revision,
            "power_trace_id": self.power_trace_id,
            "power_revision": self.power_revision,
            "period": [frac_to_json(self.period_start), frac_to_json(self.period_end)],
            "window_length": frac_to_json(self.window_length),
            "coverage": self.coverage,
            "window": None if self.window is None else [frac_to_json(x) for x in self.window],
            "demand": frac_to_json(self.demand),
            "charge": frac_to_json(self.charge),
            "sources": [
                {"start": frac_to_json(p0), "end": frac_to_json(p1), "segment_id": sid}
                for p0, p1, sid in self.sources
            ],
            "gaps": [
                {"start": frac_to_json(p0), "end": frac_to_json(p1)} for p0, p1 in self.gaps
            ],
        }


def evaluate(contract: Contract, trajectory: Trajectory,
             trace_id: str, power_revision: str) -> TargetCharge:
    """Compute the target charge product for a contract against a trajectory."""
    contract.validate()
    a, b = contract.period
    w = contract.window_length
    known, total, gaps = trajectory.coverage(a, b)

    base = dict(
        contract_id=contract.contract_id,
        contract_revision=contract.revision,
        power_trace_id=trace_id,
        power_revision=power_revision,
        period_start=a,
        period_end=b,
        window_length=w,
        gaps=tuple(gaps),
    )

    complete = not gaps
    try:
        best_i, best_s, sources = trajectory.demand_max(a, b, w)
    except ValueError:
        # No fully-known window of length w at all.
        return TargetCharge(
            coverage=COVERAGE_INSUFFICIENT,
            window=None,
            demand=None,
            charge=None,
            sources=(),
            **base,
        )

    demand = best_i / w
    window = (best_s, best_s + w)
    src = tuple((p0, p1, seg.segment_id) for p0, p1, seg in sources)

    if not complete:
        # A known window exists but the whole period is not known: analysis
        # only.  Never a successful charge, never labelled full-period max.
        return TargetCharge(
            coverage=COVERAGE_PARTIAL,
            window=window,
            demand=demand,
            charge=None,
            sources=src,
            **base,
        )

    charge = period_charge(demand, contract.included_demand, contract.price_per_excess_unit)
    return TargetCharge(
        coverage=COVERAGE_COMPLETE,
        window=window,
        demand=demand,
        charge=charge,
        sources=src,
        **base,
    )


def require_bookable(target: TargetCharge) -> Fraction:
    """Return the charge, or raise if the product is not a successful charge."""
    if not target.bookable:
        raise InsufficientEvidence(
            f"target product for {target.contract_id} / "
            f"[{target.period_start},{target.period_end}) is {target.coverage!r}; "
            "it cannot be booked as a successful charge"
        )
    return target.charge  # type: ignore[return-value]
