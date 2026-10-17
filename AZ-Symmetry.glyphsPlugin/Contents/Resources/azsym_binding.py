"""Pairing nodes across a symmetry, and detecting when a pairing goes stale.

Pure module: no Glyphs imports. A layer is represented as ``Paths`` — a list of
paths, each a list of ``(x, y)`` node positions in path order. On-curve and
off-curve nodes are both present and are treated identically, because mirroring
a handle is the same operation as mirroring an on-curve point.

The pairing rules run in the native engine (native/binding.cpp); the docstrings
below are their specification.
"""

from __future__ import annotations

from dataclasses import dataclass

import azsym_native
from azsym_geometry import (
    Point,
    contour_sides,
)

Paths = list[list[Point]]
NodeRef = tuple[int, int]
Pair = tuple[int, int, int, int]


@dataclass(frozen=True)
class Binding:
    """One symmetry's pairing over one layer."""

    pairs: tuple[Pair, ...]
    onaxis: tuple[NodeRef, ...]
    counts: tuple[int, ...]
    unpaired: tuple[NodeRef, ...]


def _offsets(count: int, mode: str, self_paired: bool) -> list[int]:
    """Which index offsets could describe this symmetry.

    A mirror reverses traversal, and ``partner(i) = (s - i) mod n`` is its own
    inverse for every ``s``, so all offsets are candidates. A half-turn PRESERVES
    traversal, and ``partner(i) = (i + s) mod n`` is only an involution when
    ``2s = 0 mod n`` — so a contour can only be half-turn symmetric onto ITSELF
    at ``s = n/2``, and only with an even node count. Between two contours any
    offset is a valid bijection.
    """
    return azsym_native.offsets(count, mode, self_paired)


def _alignment(
    src_points: list[Point],
    dst_points: list[Point],
    centre: Point,
    mode: str,
    shear: float,
) -> tuple[int, float, float, bool] | None:
    """Best alignment of one contour onto another (or onto itself).

    Searching all n offsets finds the correspondence without knowing anything
    about where the nodes happen to be, which is the whole point: the shape does
    not have to be symmetric yet.

    Ranked by the SHARE of partner pairs that straddle the symmetry — a
    distance-free, topological test — with mean residual only breaking ties.
    Returns ``(offset, straddling_share, mean_residual, flipped)``, where
    ``flipped`` means the destination contour was traversed backwards, or None
    if the contours cannot correspond.
    """
    return azsym_native.alignment(
        src_points, dst_points, src_points is dst_points, centre, mode, shear
    )


def _sides(points: list[Point], centre: Point, mode: str, shear: float) -> int | None:
    """+1 or -1 if every node sits on one side, None if the contour crosses."""
    signs = {1 if value > 0 else -1 for value in contour_sides(points, centre, mode, shear)}
    return signs.pop() if len(signs) == 1 else None


def pair_nodes(
    paths: Paths,
    centre: Point,
    mode: str,
    shear: float,
    source_sign: int,
    split: Point | None = None,
) -> Binding:
    """Pair nodes with their symmetric partners by walking the contour.

    Pairing is STRUCTURAL, not positional: partners come from the order nodes
    appear in the contour, not from being near a transformed position. That
    matters because binding is a forcing operation — "copy this side onto that
    one" — so the shape does not have to be symmetric yet. Matching by position
    refuses exactly the shapes this tool exists to fix.

    ``mode`` selects the symmetry and ``source_sign`` picks which side leads.

    ``split`` is a half-turn's guide split (``azsym_geometry.axis_split``,
    decision 7): the guide's line, not the box diagonal, decides which node of
    each pair leads.

    A contour that crosses the axis pairs with itself. A contour lying wholly on
    one side pairs with an unclaimed contour of equal length on the other, so
    two-part marks like a dieresis still bind. Anything left over is reported in
    ``unpaired`` rather than guessed at.

    Under a mirror, nodes that map to themselves are the on-axis ones, recorded
    in ``onaxis`` and clamped onto the axis at bind time — structurally, so a
    node that belongs on the axis while sitting 31 units off it is still found.
    A half-turn onto itself has one fixed index only when the node count is even.

    There is no matching tolerance. An earlier version matched by proximity and
    needed one; it also could not bind the shapes this tool exists to fix.

    ponytail: every contour is treated as closed, which is what glyph outlines
    almost always are. An open contour mirrors as a plain reversal, which the
    offset search still finds; it just also considers wrapped offsets that are
    meaningless for it.
    """
    pairs, onaxis, unpaired = azsym_native.pair_nodes(
        paths, centre, mode, shear, source_sign, split
    )
    return Binding(
        pairs=pairs,
        onaxis=onaxis,
        counts=tuple(len(path) for path in paths),
        unpaired=unpaired,
    )


def mirror_source(
    paths: Paths,
    binding: Binding,
    centre: Point,
    mode: str,
    shear: float,
) -> dict[NodeRef, Point]:
    """One-shot snap at bind time: rewrite the target side from the source side.

    Also clamps on-axis nodes onto the axis. Returns only the nodes that need to
    move.
    """
    return dict(
        azsym_native.sources(False, paths, binding.pairs, binding.onaxis, centre, mode, shear)
    )


def model_centre(paths: Paths, binding: Binding, centre: Point, mode: str, shear: float) -> Point:
    """``centre`` moved onto the mirror's on-axis nodes, unsheared.

    Hugo, 2026-10-10, on an o whose right half was mid-edit: "if i select left
    side, it horizontal should be possible". A handle 40 units out put the
    bounding-box centre 20 units right of the o's top and bottom, so keeping
    the left half still dragged both onto a wrong axis. The on-axis nodes belong
    to the half you keep as much as to the other, so they say where its mirror
    is; the box only says so once the whole outline is symmetric — and after the
    bind it is, about this same line.

    Mirrors only; a half-turn keeps the box centre. Several on-axis nodes that
    disagree meet at their mean, and the switch's purity check sees them move.
    """
    moved = azsym_native.model_centre(paths, binding.onaxis, centre, mode, shear)
    return centre if moved is None else moved


def fitted(paths: Paths, centre: Point, mode: str, shear: float, source_sign: int) -> str:
    """The transform a mirror SWITCH binds with: the mirror, or its turned form.

    Only on an italic (see azsym_geometry.TURNED): an upright mirror is left
    alone, and so is the half-turn, which has nothing to turn into. Of the two,
    the one that moves the outline least wins — the turned form only by a clear
    margin. Measured once, at bind time; the stored binding then keeps it.
    """
    code = azsym_native.fitted(paths, centre, mode, shear, source_sign)
    return mode if code == azsym_native.mode_code(mode) else azsym_native.MODE_NAMES[code]


def meet_source(
    paths: Paths,
    binding: Binding,
    centre: Point,
    mode: str,
    shear: float,
) -> dict[NodeRef, Point]:
    """One-shot snap that moves BOTH partners half-way, instead of one onto the other.

    ``mirror_source`` picks a winner: the source side survives and the other is
    rewritten from it. That is right when the designer drew one half and wants
    the other to follow it. It is wrong when they drew both and the two merely
    disagree — there the losing side's drawing is discarded, and the shape moves
    the whole distance rather than half of it.

    Each node goes to the midpoint of where it is and where its partner says it
    belongs. Every pair this writes lands EXACTLY symmetric — measured at 0.0 to
    twelve decimal places — and the algebra says why: ``partner`` is affine and
    its own inverse, so with ``a' = (a + partner(b))/2`` and
    ``b' = (b + partner(a))/2``,

        partner(a') = (partner(a) + partner(partner(b))) / 2
                    = (partner(a) + b) / 2
                    = b'

    MEASURED across every real outline in the test suite: the furthest a
    node moves is exactly HALF what ``mirror_source`` moves it, on every glyph
    and every mode.

    The ``sign`` its ``Binding`` was paired with makes no difference to the
    result. Swapping source for target swaps the two halves of a midpoint, which
    is the same point — pinned by a test rather than left as an observation.

    A pair already symmetric to within ``close`` is skipped WHOLE, both ends,
    exactly as ``mirror_source`` skips it. Settling each end independently
    against its own tolerance would let a pair one grid step out sit unmoved at
    both ends, which is twice the residual ``mirror_source`` leaves; and writing
    it anyway would hand back a write for every node of an already-symmetric
    outline.

    Because the skip rule is the same one, the residual left behind is the same
    too — MEASURED bit-for-bit equal to ``mirror_source``'s across every outline
    of the test suite (worst case 0.492421 units on ``H`` under a
    vertical mirror, against a ``TOL`` of 0.5). So this is not a looser
    operation than binding, only a cheaper one: exactly as tight, at half the
    movement.
    """
    return dict(
        azsym_native.sources(True, paths, binding.pairs, binding.onaxis, centre, mode, shear)
    )


def to_dict(binding: Binding) -> dict[str, list]:
    """Plain JSON-ish payload for ``layer.userData``."""
    return {
        "pairs": [list(pair) for pair in binding.pairs],
        "onaxis": [list(ref) for ref in binding.onaxis],
        "counts": list(binding.counts),
        "unpaired": [list(ref) for ref in binding.unpaired],
    }


def from_dict(data: object) -> Binding | None:
    """Rebuild a Binding from userData, or None when the payload is unusable.

    Duck-typed on purpose. A reopened Glyphs file returns NSDictionary/NSArray,
    for which ``isinstance(data, dict)`` is False — type-checking here would
    silently drop every binding the moment a file is closed and reopened.
    """
    try:
        pairs = tuple(
            tuple(int(value) for value in pair)
            for pair in data["pairs"]  # type: ignore[index]
        )
        onaxis = tuple(
            tuple(int(value) for value in ref)
            for ref in data["onaxis"]  # type: ignore[index]
        )
        counts = tuple(int(value) for value in data["counts"])  # type: ignore[index]
        unpaired = tuple(
            tuple(int(value) for value in ref)
            for ref in data["unpaired"]  # type: ignore[index]
        )
    except (KeyError, TypeError, ValueError):
        return None

    # Validate shapes: pairs are 4-tuples, onaxis and unpaired are 2-tuples.
    if any(len(pair) != 4 for pair in pairs):
        return None
    if any(len(ref) != 2 for ref in onaxis):
        return None
    if any(len(ref) != 2 for ref in unpaired):
        return None

    # Validate all references are non-negative integers and within counts bounds.
    for src_path, src_node, dst_path, dst_node in pairs:
        if (
            src_path < 0
            or src_node < 0
            or dst_path < 0
            or dst_node < 0
            or src_path >= len(counts)
            or dst_path >= len(counts)
            or src_node >= counts[src_path]
            or dst_node >= counts[dst_path]
        ):
            return None
    for path_index, node_index in onaxis:
        if (
            path_index < 0
            or node_index < 0
            or path_index >= len(counts)
            or node_index >= counts[path_index]
        ):
            return None
    for path_index, node_index in unpaired:
        if (
            path_index < 0
            or node_index < 0
            or path_index >= len(counts)
            or node_index >= counts[path_index]
        ):
            return None

    # The comprehensions above build tuple[int, ...]; the length checks that
    # follow are what make them Pair and NodeRef, which the checker cannot see.
    return Binding(
        pairs=pairs,  # type: ignore[arg-type]
        onaxis=onaxis,  # type: ignore[arg-type]
        counts=counts,
        unpaired=unpaired,  # type: ignore[arg-type]
    )


def stale_paths(binding: Binding, paths: Paths) -> frozenset[int] | None:
    """Which stored path indices no longer match the layer.

    Returns None when the number of paths itself changed — every stored path
    index is then suspect, so the whole binding must stop writing. Otherwise
    returns the set of path indices whose node count changed; an empty set means
    the binding is healthy.
    """
    if len(paths) != len(binding.counts):
        return None
    return frozenset(
        index for index, count in enumerate(binding.counts) if len(paths[index]) != count
    )
