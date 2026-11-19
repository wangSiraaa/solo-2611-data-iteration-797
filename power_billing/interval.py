"""Exact rational time intervals and the linear-window demand search.

Everything is ``fractions.Fraction``.  No floats are involved anywhere.

The central observation behind the demand search is that for a fixed window
length ``W`` and a left-continuous, piecewise-constant power curve ``P``, the
window integral

    I(s) = integral from s to s+W of P

is a continuous, piecewise-linear function of the window start ``s``.  Its
slope is ``P(s+W) - P(s)``; the slope can only change when ``s`` crosses an
*event* (a power breakpoint shifted by ``0`` or by ``W``).  Between consecutive
events the slope is constant, so on each such interval ``I`` is linear, and its
maximum over a closed segment is attained at an endpoint unless the segment is
flat.  Therefore a maximum of ``I`` over the feasible starts is attained at
either an event or a feasible-segment endpoint.  We evaluate every candidate exactly; no grid is
ever used.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from fractions import Fraction
from typing import Iterable, Sequence

from .errors import SegmentationError

ZERO = Fraction(0)


def _xf(node) -> Fraction:
    """Parse a fraction given either as an int/str or as an ``{n,d}`` object."""
    if isinstance(node, dict):
        return Fraction(int(node["n"]), int(node["d"]))
    return Fraction(node)


def frac_to_json(x: Fraction | None) -> dict | None:
    """Canonical JSON encoding: ``{"n": numerator, "d": positive denominator}``."""
    if x is None:
        return None
    x = Fraction(x)
    return {"n": x.numerator, "d": x.denominator}


def frac_from_json(node) -> Fraction | None:
    if node is None:
        return None
    if isinstance(node, dict):
        return Fraction(int(node["n"]), int(node["d"]))
    return Fraction(node)


@dataclass(frozen=True, slots=True)
class Segment:
    segment_id: str
    start: Fraction
    end: Fraction
    power: Fraction

    @property
    def length(self) -> Fraction:
        return self.end - self.start


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def validate_segments(segments: Iterable[Segment]) -> tuple[Segment, ...]:
    """Validate and canonically order a power trajectory.

    Rules enforced (all are contract requirements):

    * ``segment_id`` is a non-empty string and unique within the trajectory;
    * ``start < end`` (strictly positive length, half-open ``[start, end)``);
    * ``power >= 0`` (negative power is out of scope) and a rational;
    * segments do not overlap.  Touching segments (``prev.end == next.start``)
      are allowed and leave no gap; a gap is any positive spacing.

    The result is sorted by start time so all later code can rely on that.
    """
    segs = tuple(segments)
    ids: set[str] = set()
    for s in segs:
        if not isinstance(s.segment_id, str) or not s.segment_id:
            raise SegmentationError("segment_id must be a non-empty string")
        if s.segment_id in ids:
            raise SegmentationError(f"duplicate segment_id {s.segment_id!r}")
        ids.add(s.segment_id)
        if not (s.start < s.end):
            raise SegmentationError(
                f"segment {s.segment_id!r}: start must be strictly before end"
            )
        if s.power < 0:
            raise SegmentationError(
                f"segment {s.segment_id!r}: power must be non-negative"
            )
    ordered = sorted(segs, key=lambda s: s.start)
    for prev, nxt in zip(ordered, ordered[1:]):
        if nxt.start < prev.end:
            raise SegmentationError(
                f"segments {prev.segment_id!r} and {nxt.segment_id!r} overlap"
            )
    return ordered


# --------------------------------------------------------------------------- #
# Trajectory: integration, coverage, gap provenance
# --------------------------------------------------------------------------- #
class Trajectory:
    """An immutable, validated set of non-overlapping power segments."""

    __slots__ = ("segments", "_starts", "_ends", "_prefix", "breaks",
                 "_gaps", "_gap_starts", "_first_start", "_last_end")

    def __init__(self, segments: Iterable[Segment], *, validate: bool = True):
        segs = validate_segments(segments) if validate else tuple(sorted(
            segments, key=lambda s: s.start
        ))
        self.segments: tuple[Segment, ...] = segs
        self._starts = [s.start for s in segs]
        self._ends = [s.end for s in segs]
        # prefix[i] = integral from 0 to _starts[i] of the covered parts.
        # (Segments may have arbitrary absolute times; prefix is relative to 0
        # and only differences are ever used.)
        prefix = [ZERO]
        acc = ZERO
        for s in segs:
            acc += s.power * (s.end - s.start)
            prefix.append(acc)
        self._prefix = prefix
        brk: list[Fraction] = []
        for s in segs:
            brk.append(s.start)
            brk.append(s.end)
        self.breaks: tuple[Fraction, ...] = tuple(sorted(set(brk)))
        # Interior gaps only (uncovered stretches *between* known segments).
        # Time before the first or after the last segment is unknown too, but
        # handled by the boundary check in ``is_covered``.
        gaps = [
            (prev.end, nxt.start)
            for prev, nxt in zip(segs, segs[1:])
            if nxt.start > prev.end
        ]
        self._gaps = gaps
        self._gap_starts = [g[0] for g in gaps]
        self._first_start = segs[0].start if segs else None
        self._last_end = segs[-1].end if segs else None

    # -- interval decomposition ------------------------------------------- #
    def covered_pieces(self, a: Fraction, b: Fraction) -> list[tuple[Fraction, Fraction, Segment | None]]:
        """Split ``[a,b)`` into maximal pieces tagged with the covering segment.

        A piece with ``segment is None`` is a gap.  Pieces are in time order,
        contiguous, and exactly partition ``[a,b)``.
        """
        if not (a < b):
            return []
        segs = self.segments
        lo = bisect_right(self._ends, a)  # first segment with end > a
        hi = bisect_left(self._starts, b)  # first segment with start >= b
        pieces: list[tuple[Fraction, Fraction, Segment | None]] = []
        cursor = a
        for idx in range(lo, hi):
            s = segs[idx]
            if s.start > cursor:
                pieces.append((cursor, s.start, None))
            p0 = max(a, s.start)
            p1 = min(b, s.end)
            if p0 < p1:
                pieces.append((p0, p1, s))
                cursor = p1
            # if p0 >= p1 the segment does not contribute inside [a,b)
        if cursor < b:
            pieces.append((cursor, b, None))
        return pieces

    def covered_length(self, a: Fraction, b: Fraction) -> Fraction:
        return sum(
            (p1 - p0 for p0, p1, seg in self.covered_pieces(a, b) if seg is not None),
            ZERO,
        )

    def is_covered(self, a: Fraction, b: Fraction) -> bool:
        """True iff every point of ``[a,b)`` is covered by a known segment.

        O(log n): the window must lie inside ``[first_start, last_end]`` and
        must not intersect any interior gap.  Because gaps are disjoint and
        sorted, only the last gap starting before ``b`` can possibly reach
        into ``[a,b)``.
        """
        if not (a < b):
            return False
        if self._first_start is None:
            return False
        if a < self._first_start or b > self._last_end:
            return False
        if not self._gaps:
            return True
        idx = bisect_left(self._gap_starts, b) - 1
        if idx >= 0 and self._gaps[idx][1] > a:
            return False
        return True

    def integral(self, a: Fraction, b: Fraction) -> Fraction:
        """Exact integral of known power over ``[a,b)``; gaps contribute 0.

        Because gaps contribute zero the result is only the *known* part; use
        :meth:`coverage` / :meth:`is_covered` to know whether that is the whole
        story.  Nothing is extrapolated outside the known segments.
        """
        if not (a < b):
            return ZERO
        return self._prefix_integral(b) - self._prefix_integral(a)

    def _prefix_integral(self, t: Fraction) -> Fraction:
        """Integral of known power over ``(-inf, t)`` (gaps add nothing)."""
        # Find the last segment that starts before t; everything after it
        # starts at or after t and contributes nothing.
        idx = bisect_left(self._starts, t)  # number of segments with start < t
        if idx == 0:
            return ZERO
        total = self._prefix[idx]  # full contribution of segments 0..idx-1
        last = self.segments[idx - 1]
        if last.end > t:
            # The last included segment runs past t: remove the excess tail.
            total -= last.power * (last.end - t)
        return total

    def coverage(self, a: Fraction, b: Fraction) -> tuple[Fraction, Fraction, list[tuple[Fraction, Fraction]]]:
        """Return ``(known_length, total_length, gaps)`` for ``[a,b)``.

        ``gaps`` lists maximal uncovered ``(start, end)`` intervals in order.
        """
        total = b - a if a < b else ZERO
        known = ZERO
        gaps: list[tuple[Fraction, Fraction]] = []
        for p0, p1, seg in self.covered_pieces(a, b):
            if seg is None:
                gaps.append((p0, p1))
            else:
                known += p1 - p0
        return known, total, gaps

    # -- window demand search --------------------------------------------- #
    def demand_max(
        self, a: Fraction, b: Fraction, w: Fraction
    ) -> tuple[Fraction, Fraction, list[tuple[Fraction, Fraction, Segment]]]:
        """Maximum average known power over a full window of length ``w``.

        Returns ``(best_integral, best_start, sources)`` where ``sources`` is the
        decomposition of ``[best_start, best_start+w)`` into ``(p0, p1, segment)``.
        Only fully known windows are considered; if none exists (or ``w > b-a``)
        a :class:`ValueError` is raised.  Ties are broken by the earliest start.
        """
        if w <= 0:
            raise ValueError("window length must be positive")
        if w > b - a:
            raise ValueError("window length exceeds the settlement period")
        candidates = self._candidate_starts(a, b, w)
        best_i: Fraction | None = None
        best_s: Fraction | None = None
        for s in candidates:
            if not self.is_covered(s, s + w):
                continue
            i = self.integral(s, s + w)
            if best_i is None or i > best_i:
                best_i, best_s = i, s
            # candidates are sorted ascending, so strict ``>`` keeps the
            # earliest start on ties.
        if best_i is None:
            raise ValueError("no fully known window of the requested length")
        sources = [
            (p0, p1, seg)
            for p0, p1, seg in self.covered_pieces(best_s, best_s + w)
            if seg is not None
        ]
        return best_i, best_s, sources

    def _candidate_starts(self, a: Fraction, b: Fraction, w: Fraction) -> list[Fraction]:
        """All starts needed to locate a maximum for a fixed ``w``.

        Event starts are breakpoints ``t`` (breakpoint, ``t - w``) that lie in
        ``[a, b-w]``; add the feasible-segment endpoints ``a`` and ``b-w`` so
        every linear piece's endpoints are evaluated.
        """
        hi = b - w
        cands: set[Fraction] = {a, hi}
        for t in self.breaks:
            if a <= t <= hi:
                cands.add(t)
            shifted = t - w
            if a <= shifted <= hi:
                cands.add(shifted)
        return sorted(cands)


def period_charge(demand: Fraction, included: Fraction, price: Fraction) -> Fraction:
    """Target charge ``max(0, demand - included) * price`` with exact rationals."""
    excess = demand - included
    if excess < 0:
        excess = ZERO
    return excess * price
