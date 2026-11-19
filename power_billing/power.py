"""In-memory, revisioned power trajectories.

A *trajectory store* holds named traces, each pinned to one current revision.
A revision operation replaces one or more segments **by identity**: a segment
whose ``segment_id`` appears in the replacement set is atomically replaced; the
post-revision trajectory is fully validated (unique ids, positive lengths,
non-overlapping) and only then committed.  Any invalid result leaves the store
exactly as it was -- all-or-nothing at the batch level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .errors import RevisionError
from .interval import Segment, Trajectory, validate_segments
from .numbers import parse_frac


@dataclass(frozen=True, slots=True)
class PowerRevision:
    trace_id: str
    revision: str
    segments: tuple[Segment, ...]

    def trajectory(self) -> Trajectory:
        return Trajectory(self.segments)


class TrajectoryStore:
    """A mutable registry of revisioned trajectories (in memory, by design)."""

    def __init__(self) -> None:
        self._revs: dict[str, PowerRevision] = {}

    # -- loading ---------------------------------------------------------- #
    def register(self, trace_id: str, revision: str, segments: Iterable[Segment]) -> PowerRevision:
        ordered = validate_segments(segments)
        if trace_id in self._revs:
            raise RevisionError(f"trace {trace_id!r} is already registered")
        rev = PowerRevision(trace_id, revision, tuple(ordered))
        self._revs[trace_id] = rev
        return rev

    def register_json(self, obj: dict) -> PowerRevision:
        segs = [
            Segment(
                segment_id=str(s["segment_id"]),
                start=parse_frac(s["start"], "start"),
                end=parse_frac(s["end"], "end"),
                power=parse_frac(s["power"], "power"),
            )
            for s in obj["segments"]
        ]
        return self.register(str(obj["trace_id"]), str(obj["revision"]), segs)

    # -- reading ---------------------------------------------------------- #
    def current(self, trace_id: str) -> PowerRevision:
        try:
            return self._revs[trace_id]
        except KeyError as exc:
            raise RevisionError(f"unknown trace {trace_id!r}") from exc

    def trajectory(self, trace_id: str) -> Trajectory:
        return self.current(trace_id).trajectory()

    # -- revising --------------------------------------------------------- #
    def revise(self, trace_id: str, revision: str,
               replace: Iterable[Segment], *, expected_revision: str | None = None) -> PowerRevision:
        """Atomically replace segments by identity and commit a new revision.

        ``replace`` may contain any number of segments; every one must name an
        existing ``segment_id``.  The resulting trajectory is validated as a
        whole before anything is stored, so an illegal result changes nothing.
        """
        cur = self.current(trace_id)
        if expected_revision is not None and cur.revision != expected_revision:
            raise RevisionError(
                f"trace {trace_id!r} is at revision {cur.revision!r}, "
                f"expected {expected_revision!r}"
            )
        if revision == cur.revision:
            raise RevisionError(f"revision {revision!r} already exists")

        by_id = {s.segment_id: s for s in cur.segments}
        replacements = tuple(replace)
        seen: set[str] = set()
        for seg in replacements:
            if seg.segment_id not in by_id:
                raise RevisionError(
                    f"cannot replace unknown segment {seg.segment_id!r}"
                )
            if seg.segment_id in seen:
                raise RevisionError(
                    f"segment {seg.segment_id!r} provided twice in one revision"
                )
            seen.add(seg.segment_id)

        repl_map = {s.segment_id: s for s in replacements}
        merged: list[Segment] = []
        for seg in cur.segments:
            merged.append(repl_map.get(seg.segment_id, seg))

        # Validate the *whole* candidate before committing.
        ordered = validate_segments(merged)
        new_rev = PowerRevision(trace_id, revision, tuple(ordered))
        # ``validate_segments`` returns a new sorted tuple; only assign on success.
        self._revs[trace_id] = new_rev
        return new_rev

    def revise_json(self, trace_id: str, obj: dict, *,
                    expected_revision: str | None = None) -> PowerRevision:
        node = obj.get("replace_segment", obj.get("replace_segments"))
        if node is None:
            raise RevisionError("revision must contain replace_segment(s)")
        if isinstance(node, dict):
            node = [node]
        segs = [
            Segment(
                segment_id=str(s["segment_id"]),
                start=parse_frac(s["start"], "start"),
                end=parse_frac(s["end"], "end"),
                power=parse_frac(s["power"], "power"),
            )
            for s in node
        ]
        return self.revise(
            trace_id, str(obj["revision"]), segs, expected_revision=expected_revision
        )
