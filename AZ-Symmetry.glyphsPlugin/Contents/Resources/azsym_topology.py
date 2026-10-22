"""What changed structurally between two frames of the same layer.

Pure module: no Glyphs imports. Knows nothing about bindings or symmetry — it
only answers "what single edit turns the previous outline into this one?", so it
can be tested headlessly and reasoned about on its own.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

from azsym_geometry import Point, close

Paths = list[list[Point]]
NodeTypes = list[list[str]]
Signature = tuple


@dataclass(frozen=True)
class Structure:
    """One frame's outline: where the nodes are, and what kind they are."""

    paths: Paths
    types: NodeTypes


def signature(structure: Structure) -> Signature:
    """A cheap structural fingerprint, deliberately BLIND to position.

    This is the per-frame gate: it runs on every frame of a drag, where
    positions change constantly and structure does not. Including positions
    would make every dragged frame look like a structural edit.

    Node types carry the CONNECTION as well (see ``encode_type``), so toggling a
    node between smooth and sharp does change the signature — which is the only
    way an edit that moves nothing can reach the classifier at all.

    Node types are included in ORDER, not as a multiset, so reversing a contour
    usually changes the signature. One whose type sequence comes out the same —
    an all-line contour reversed or given a new start node, two contours
    swapped — does not change it, and ``renumbered`` is what catches those.
    """
    return tuple(
        (len(path), tuple(node_types))
        for path, node_types in zip(structure.paths, structure.types, strict=True)
    )


UNCHANGED = "unchanged"
INSERTED = "inserted"
DELETED = "deleted"
DELETED_SEVERAL = "deleted-several"
CURVE_SPLIT = "curve-split"
CURVE_JOIN = "curve-join"
SEGMENT_CURVED = "segment-curved"
CONTOUR_ADDED = "contour-added"
CONTOUR_REMOVED = "contour-removed"
TYPE_CHANGED = "type-changed"
RENUMBERED = "renumbered"
UNCLASSIFIED = "unclassified"

# The one node type that is not ON the curve. Everything else — line, curve,
# qcurve — is an on-curve point, and it is on-curve points that a split leaves
# where they were.
OFFCURVE = "offcurve"

# How the two path segments meeting at an on-curve node relate to each other.
# Plain strings, never Glyphs' GSNodeConnection enum: this module is pure, and
# plugin.py is the only file allowed to know that GSBase.h numbers them Sharp=0,
# Smooth=100 and SuperSmooth=102.
SHARP = "sharp"
SMOOTH = "smooth"
SUPERSMOOTH = "supersmooth"
CONNECTIONS = (SHARP, SMOOTH, SUPERSMOOTH)

# What separates a node's type from its connection inside one encoded string.
#
# The connection rides IN the type string rather than in a fourth list on
# Structure. ``signature``, ``classify``, ``_is_split_at`` and
# ``_reversed_types`` all walk types in lockstep with points, and a third
# sequence would have to be threaded through every one of them. Encoded this way
# the whole pipeline carries the connection with no signature change — and a
# smooth/sharp toggle, which moves no node at all and which no amount of
# position comparison can see, becomes an ordinary TYPE_CHANGED.
CONNECTION_MARK = ":"

# What a cubic split adds: a new on-curve node and the handle either side of it.
SPLIT_SIZE = 3

# What turning a LINE segment into a curve adds: two handles and no on-curve
# node at all. Distinct from a split, which adds a node the designer can drag —
# here the segment keeps its two ends and gains the pair that bends it.
HANDLE_PAIR = 2


def encode_type(node_type: str, connection: str) -> str:
    """A node's type and its connection as the single string ``Structure`` holds.

    A SHARP node keeps its bare type, so an outline of sharp nodes encodes
    exactly as it did before connections existed.

    An OFF-CURVE node is always bare, whatever connection it reports. A handle
    has no pair of segments to relate — GSNode.h says the connection describes
    "the relationship between these two segments", and only an on-curve node has
    an incoming and an outgoing one. More practically, ``OFFCURVE`` is compared
    by EQUALITY in ``_is_split_at``, ``_curve_split_index`` and
    ``_reversed_types``, and a suffixed handle would silently fail every one of
    those comparisons — which is curve split and curve join detection gone.
    """
    if node_type == OFFCURVE:
        return OFFCURVE
    if connection == SHARP or connection not in CONNECTIONS:
        return node_type
    return f"{node_type}{CONNECTION_MARK}{connection}"


def decode_type(encoded: str) -> tuple[str, str]:
    """``(type, connection)`` back out of one encoded string.

    An unrecognised suffix decodes as SHARP and is DROPPED rather than passed
    through: the type half is assigned straight onto a GSNode, so a string
    Glyphs cannot read is a worse outcome than a connection this frame gets
    wrong.
    """
    node_type, mark, connection = encoded.partition(CONNECTION_MARK)
    if not mark or connection not in CONNECTIONS:
        return node_type, SHARP
    return node_type, connection


@dataclass(frozen=True)
class Change:
    """One structural edit, or the absence of one.

    ``index`` carries the node index for INSERTED and DELETED. ``node_type``
    carries the inserted node's ENCODED type — its type and its connection, per
    ``encode_type`` — which the mirrored partner must copy.

    For CURVE_SPLIT ``index`` is the NEW ON-CURVE node's index in the current
    node list, and the two handles the split added are at ``index - 1`` and
    ``index + 1`` modulo the contour's length. The two entries beyond those are
    the segment's original handles, which the subdivision MOVED — they are not
    part of the insert, and treating them as new duplicates them.

    For CURVE_JOIN ``index`` is the REMOVED on-curve node's index in the
    PREVIOUS node list — the only frame that still has it — and the same
    neighbourhood applies there: the two entries either side of it went with it,
    and the two beyond those are the merged segment's handles, which the join
    RECOMPUTED rather than removed.
    """

    kind: str
    path: int = -1
    index: int = -1
    node_type: str = ""
    # DELETED_SEVERAL: every node removed, as (path, index) in the PREVIOUS
    # frame, in path order.
    removed: tuple[tuple[int, int], ...] = ()


def renumbered(previous: Structure, current: Structure) -> bool:
    """True when no node moved but nodes now sit at other indices: Make Node
    First, Reverse Contours, a change of contour order. The nodes whose index
    reads a new position hold, between them, exactly the positions and types
    those indices held. A drag puts a node somewhere no node was; a single
    changed index cannot be a renumbering."""
    if [len(path) for path in previous.paths] != [len(path) for path in current.paths]:
        return False
    was: list[tuple[Point, str]] = []
    now: list[tuple[Point, str]] = []
    for before, after, types_before, types_after in zip(
        previous.paths, current.paths, previous.types, current.types, strict=True
    ):
        for index, (old, new) in enumerate(zip(before, after, strict=True)):
            if old != new:
                was.append((old, types_before[index]))
                now.append((new, types_after[index]))
    return len(was) > 1 and sorted(was) == sorted(now)


def _without(points: list[Point], index: int) -> list[Point]:
    return points[:index] + points[index + 1 :]


def _same(left: list[Point], right: list[Point]) -> bool:
    return len(left) == len(right) and all(close(a, b) for a, b in zip(left, right, strict=True))


def _added_contour(previous: Paths, current: Paths) -> int | None:
    """Which index of ``current`` is the contour ``previous`` does not have."""
    for index in range(len(current)):
        rest = current[:index] + current[index + 1 :]
        if len(rest) == len(previous) and all(
            _same(left, right) for left, right in zip(rest, previous, strict=True)
        ):
            return index
    return None


def _insertion_index(previous: list[Point], current: list[Point]) -> int | None:
    """Which index of ``current`` was added, or None if no single one explains it.

    O(n^2), and deliberately so: it runs only when the signature changed, never
    on the frames of a drag.
    """
    for index in range(len(current)):
        if _same(_without(current, index), previous):
            return index
    return None


def _types_without(types: list[str], index: int) -> list[str]:
    return types[:index] + types[index + 1 :]


def _other_paths_retyped(previous: Structure, current: Structure, skip: int) -> bool:
    """True if any contour OTHER than ``skip`` has a different type list.

    An insert/delete only proves node POSITIONS are untouched elsewhere — the
    count comparison that locates it says nothing about node TYPES. Without
    this, a coincident retype on another contour would vanish behind the
    insert/delete instead of forcing UNCLASSIFIED.
    """
    return any(
        previous.types[index] != current.types[index]
        for index in range(len(current.types))
        if index != skip
    )


def split_cluster(index: int, count: int) -> tuple[int, int, int]:
    """The three entries a split added, in path order around the new on-curve one.

    Modular because a contour is a ring: the block can straddle the list's own
    end, which is the same edit written from a different start node.
    """
    return ((index - 1) % count, index, (index + 1) % count)


def kept_indices(count: int, cluster: tuple[int, ...]) -> list[int]:
    """Which index of the CURRENT list each PREVIOUS node is at, by previous order.

    Ascending is the right order even when the cluster straddles the end of the
    list. Removing the block leaves the survivors in the rotation the previous
    frame had, because the block sits between two of them either way round.
    """
    return [index for index in range(count) if index not in cluster]


def _is_split_at(
    before: list[Point],
    after: list[Point],
    before_types: list[str],
    after_types: list[str],
    index: int,
) -> bool:
    """Would removing the block around ``index`` give ``before`` back?

    "Give back" with two exemptions, and they are the whole difficulty: de
    Casteljau recomputes the split segment's ORIGINAL handles, so the two
    entries bracketing the block are allowed to have moved. Everything else —
    every position, every type, on both sides of the block — must be untouched.
    """
    count = len(after)
    cluster = split_cluster(index, count)
    outer = ((index - 2) % count, (index + 2) % count)
    # Five distinct slots: the block, plus a moved handle either side of it.
    if len({*cluster, *outer}) != 5:
        return False
    if after_types[index] == OFFCURVE:
        return False
    if any(after_types[slot] != OFFCURVE for slot in (cluster[0], cluster[2], *outer)):
        return False

    kept = kept_indices(count, cluster)
    if [after_types[slot] for slot in kept] != before_types:
        return False

    opening, closing = kept.index(outer[0]), kept.index(outer[1])
    # The two handles must still bracket ONE gap of the previous contour, and
    # that gap must be a CUBIC — an on-curve node either side of two handles.
    if closing != (opening + 1) % len(before):
        return False
    if before_types[(opening - 1) % len(before)] == OFFCURVE:
        return False
    if before_types[(closing + 1) % len(before)] == OFFCURVE:
        return False

    return all(
        close(after[slot], before[order])
        for order, slot in enumerate(kept)
        if order not in (opening, closing)
    )


def _curve_split_index(
    before: list[Point],
    after: list[Point],
    before_types: list[str],
    after_types: list[str],
) -> int | None:
    """Index in ``after`` of the on-curve node a cubic split added, or None.

    ``_insertion_index`` cannot find this, because it requires every other node
    to be positionally unchanged and two of them are not. What DOES survive a
    subdivision is the on-curve subsequence, so the search runs over the
    on-curve candidates and each is checked in full.

    Every candidate is tried rather than the first that fits, and an ambiguous
    frame is refused: the answer is applied to a designer's outline, and two
    equally good readings of it mean neither can be trusted.
    """
    if len(before) < 4 or len(after) != len(before) + SPLIT_SIZE:
        return None
    on_curve = [index for index, node_type in enumerate(after_types) if node_type != OFFCURVE]
    if len(on_curve) != sum(1 for node_type in before_types if node_type != OFFCURVE) + 1:
        return None
    found = [
        index for index in on_curve if _is_split_at(before, after, before_types, after_types, index)
    ]
    return found[0] if len(found) == 1 else None


def _curved_segment_index(
    before: list[Point],
    after: list[Point],
    before_types: list[str],
    after_types: list[str],
) -> int | None:
    """Index in ``after`` of the FIRST of the two handles that bent a line.

    Turning a straight segment into a curve is what a designer does to a
    rectangle to make it a shape, and Glyphs writes it as +2: two off-curve
    handles between the segment's two existing ends, and the end it ARRIVES at
    retyped from ``line`` to ``curve``. Nothing moves — which is what tells this
    apart from a split, where de Casteljau recomputes the original handles.

    ``_insertion_index`` cannot find it, because that looks for ONE added entry;
    and the lone retype means the ordinary +1/-1 type comparison would refuse it
    even if it could.

    Ambiguity is refused rather than resolved, as everywhere else in this
    module: the answer is applied to a designer's outline, and two equally good
    readings of it mean neither can be trusted.

    The one shape not answered here is a pair that STRADDLES the end of the list
    — the handles at ``count - 1`` and ``0``. Glyphs appends to the closing
    segment rather than wrapping into index 0, so it does not arise from this
    edit; a contour renumbered by something else could still produce it, and it
    falls through to UNCLASSIFIED and bare re-derivation, which is what the whole
    edit did before this existed.
    """
    count = len(after)
    if len(before) < 2 or count != len(before) + HANDLE_PAIR:
        return None

    found = []
    for index in range(count - 1):
        if after_types[index] != OFFCURVE or after_types[index + 1] != OFFCURVE:
            continue
        if not _same(after[:index] + after[index + HANDLE_PAIR :], before):
            continue
        rest_types = after_types[:index] + after_types[index + HANDLE_PAIR :]
        # The node the new curve arrives at, in PREVIOUS terms. ``index`` counts
        # up to len(before) inclusive: one past the last node is the pair
        # appended to the CLOSING segment, whose far end is node 0.
        arrival = index % len(before)
        differing = [
            slot for slot in range(len(before_types)) if rest_types[slot] != before_types[slot]
        ]
        if differing != [arrival]:
            continue
        # Both ends of a segment are on-curve. A handle retyped to a curve would
        # be a contour Glyphs cannot draw.
        if OFFCURVE in (before_types[arrival], rest_types[arrival]):
            continue
        found.append(index)

    return found[0] if len(found) == 1 else None


def _surviving_types_changed(smaller: NodeTypes, larger: NodeTypes, added_index: int) -> bool:
    """True if the contours surviving a contour add/remove changed type.

    Mirrors what ``_added_contour`` already proved for node POSITIONS: pull the
    added contour out of ``larger`` and line the rest up pairwise against
    ``smaller``. If that does not hold for types too, a coincident retype on a
    surviving contour would vanish behind the CONTOUR_ADDED/REMOVED verdict.
    """
    return larger[:added_index] + larger[added_index + 1 :] != smaller


def classify(previous: Structure, current: Structure) -> Change:
    """The single edit that turns ``previous`` into ``current``.

    Returns UNCLASSIFIED rather than guessing when more than one thing changed.
    The caller re-derives the pairing in that case, which is both safe and the
    right answer for the common multi-edit case of pasting a contour. The one
    multi-edit named is a selection of nodes deleted at once (DELETED_SEVERAL),
    and only where no single edit explains the frame.
    """
    change = _single_change(previous, current)
    if change.kind == UNCLASSIFIED:
        return _several_deleted(previous, current) or change
    return change


def _several_deleted(previous: Structure, current: Structure) -> Change | None:
    """On-curve nodes removed and nothing else, two or more: every contour still
    there, each the old one with nodes taken out, its other nodes where and what
    they were. Re-deriving instead pairs an outline the deletes left lopsided,
    and the whole contour stops mirroring (edge-case hunt, 2026-10-22). A
    handle never goes alone: deleting one retypes its segment."""
    if len(previous.paths) != len(current.paths):
        return None
    removed: list[tuple[int, int]] = []
    for path_index, (before, after) in enumerate(zip(previous.paths, current.paths, strict=True)):
        types_before, types_after = previous.types[path_index], current.types[path_index]
        kept = 0
        for index, point in enumerate(before):
            if (
                kept < len(after)
                and close(point, after[kept])
                and types_before[index] == types_after[kept]
            ):
                kept += 1
            elif types_before[index] == OFFCURVE:
                return None
            else:
                removed.append((path_index, index))
        if kept != len(after):
            return None
    if len(removed) < 2:
        return None
    return Change(kind=DELETED_SEVERAL, removed=tuple(removed))


def _single_change(previous: Structure, current: Structure) -> Change:
    """``classify`` for one edit."""
    if len(current.paths) == len(previous.paths) + 1:
        index = _added_contour(previous.paths, current.paths)
        if index is None or _surviving_types_changed(previous.types, current.types, index):
            return Change(kind=UNCLASSIFIED)
        return Change(kind=CONTOUR_ADDED, path=index)
    if len(current.paths) == len(previous.paths) - 1:
        index = _added_contour(current.paths, previous.paths)
        if index is None or _surviving_types_changed(current.types, previous.types, index):
            return Change(kind=UNCLASSIFIED)
        return Change(kind=CONTOUR_REMOVED, path=index)
    if len(previous.paths) != len(current.paths):
        return Change(kind=UNCLASSIFIED)

    changed = [
        index
        for index in range(len(current.paths))
        if len(previous.paths[index]) != len(current.paths[index])
    ]
    if not changed:
        retyped = [
            (path_index, node_index)
            for path_index in range(len(current.types))
            for node_index in range(len(current.types[path_index]))
            if previous.types[path_index][node_index] != current.types[path_index][node_index]
        ]
        if len(retyped) == 1:
            path_index, node_index = retyped[0]
            return Change(
                kind=TYPE_CHANGED,
                path=path_index,
                index=node_index,
                node_type=current.types[path_index][node_index],
            )
        if len(retyped) > 1:
            return Change(kind=UNCLASSIFIED)
        return Change(kind=RENUMBERED if renumbered(previous, current) else UNCHANGED)
    if len(changed) != 1:
        return Change(kind=UNCLASSIFIED)

    path_index = changed[0]
    before, after = previous.paths[path_index], current.paths[path_index]
    delta = len(after) - len(before)

    if delta == 1:
        index = _insertion_index(before, after)
        if index is None:
            return Change(kind=UNCLASSIFIED)
        if _types_without(current.types[path_index], index) != previous.types[path_index]:
            return Change(kind=UNCLASSIFIED)
        if _other_paths_retyped(previous, current, path_index):
            return Change(kind=UNCLASSIFIED)
        return Change(
            kind=INSERTED,
            path=path_index,
            index=index,
            node_type=current.types[path_index][index],
        )

    if delta == -1:
        index = _insertion_index(after, before)
        if index is None:
            return Change(kind=UNCLASSIFIED)
        if _types_without(previous.types[path_index], index) != current.types[path_index]:
            return Change(kind=UNCLASSIFIED)
        if _other_paths_retyped(previous, current, path_index):
            return Change(kind=UNCLASSIFIED)
        return Change(
            kind=DELETED,
            path=path_index,
            index=index,
            node_type=previous.types[path_index][index],
        )

    if delta == HANDLE_PAIR:
        # A line segment pulled into a curve: two handles, no new on-curve node.
        curved = _curved_segment_index(
            before, after, previous.types[path_index], current.types[path_index]
        )
        if curved is None:
            return Change(kind=UNCLASSIFIED)
        if _other_paths_retyped(previous, current, path_index):
            return Change(kind=UNCLASSIFIED)
        # node_type is the ARRIVAL node's new type, not a handle's: the handles
        # are off-curve by definition and carry nothing worth mirroring, while
        # the retype is the half of this edit that no position can express.
        return Change(
            kind=SEGMENT_CURVED,
            path=path_index,
            index=curved,
            node_type=current.types[path_index][(curved + HANDLE_PAIR) % len(after)],
        )

    if delta == SPLIT_SIZE:
        # Splitting a curve is the ordinary way to add a node to a typeface, and
        # it arrives as +3: the new on-curve point and a handle either side.
        split = _curve_split_index(
            before, after, previous.types[path_index], current.types[path_index]
        )
        if split is None:
            return Change(kind=UNCLASSIFIED)
        if _other_paths_retyped(previous, current, path_index):
            return Change(kind=UNCLASSIFIED)
        return Change(
            kind=CURVE_SPLIT,
            path=path_index,
            index=split,
            node_type=current.types[path_index][split],
        )

    if delta == -SPLIT_SIZE:
        # Deleting an on-curve node that sits between two curves is the ordinary
        # way to REMOVE a node from a typeface, and it arrives as -3: the node
        # and the handle either side of it. Since almost every segment in a
        # typeface is a curve, this is what "delete a node" nearly always means.
        #
        # A join from previous to current is exactly a split from current to
        # previous, so the frames are swapped and the split detector answers it.
        # A second detector would duplicate the hardest logic in this module —
        # the de Casteljau handle exemption and the ambiguity refusal — and the
        # two copies would drift.
        #
        # The index that comes back is an index into the argument passed as
        # ``after``, which is the PREVIOUS frame: the node the designer removed,
        # named in the only frame that still has it.
        joined = _curve_split_index(
            after, before, current.types[path_index], previous.types[path_index]
        )
        if joined is None:
            return Change(kind=UNCLASSIFIED)
        if _other_paths_retyped(previous, current, path_index):
            return Change(kind=UNCLASSIFIED)
        return Change(
            kind=CURVE_JOIN,
            path=path_index,
            index=joined,
            node_type=previous.types[path_index][joined],
        )

    return Change(kind=UNCLASSIFIED)


# How far the shorter handle's tip may sit off the line the other one draws
# through a smooth node and still count as aligned. The grid snap alone can put
# a mirrored handle half a unit out.
KINK_TOL = 1.0


def kinked_smooth(structure: Structure, near: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Smooth on-curve nodes at or next to ``near`` whose two sides no longer line up.

    Hugo, 2026-10-10: "automatically switch smooth node to not smooth if
    symmetry break aligned of handles". A reflection maps a straight line to a
    straight line, so every handle pair a bind COPIES stays aligned — but an
    on-axis node's two handles are one kept and its own mirror image, which line
    up only when the kept one is square to the axis. Left smooth, that node
    draws a kink Glyphs believes is a tangent, and Glyphs swings the mirrored
    handle back the moment the kept one is dragged.

    Next to, as well as at: a written HANDLE belongs to the on-curve node beside
    it. A node with a zero-length side has no direction to compare, and is left.
    """
    found: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for path_index, written in near:
        points = structure.paths[path_index]
        types = structure.types[path_index]
        count = len(points)
        for index in ((written - 1) % count, written % count, (written + 1) % count):
            if (path_index, index) in seen:
                continue
            seen.add((path_index, index))
            if types[index] == OFFCURVE or decode_type(types[index])[1] == SHARP:
                continue
            node, before, after = points[index], points[index - 1], points[(index + 1) % count]
            incoming = (node[0] - before[0], node[1] - before[1])
            outgoing = (after[0] - node[0], after[1] - node[1])
            lengths = (math.hypot(*incoming), math.hypot(*outgoing))
            if min(lengths) < 1e-9:
                continue
            cross = incoming[0] * outgoing[1] - incoming[1] * outgoing[0]
            dot = incoming[0] * outgoing[0] + incoming[1] * outgoing[1]
            if dot < 0 or abs(cross) / max(lengths) > KINK_TOL:
                found.append((path_index, index))
    return found
