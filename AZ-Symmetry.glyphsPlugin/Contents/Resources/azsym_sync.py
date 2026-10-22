"""Per-frame symmetry sync.

Pure module: no Glyphs imports. Given the layer's current node positions, the
bound symmetries, and the positions from the previous frame, work out which
nodes the caller should rewrite.

Direction is fixed at bind time. The source side is the one the designer draws;
the other is generated and read-only, rewritten from the source on any frame
where either end moved. Dragging a generated node therefore springs back.

``sync`` only ever moves nodes. ``reconcile`` is the other half: when the
STRUCTURE changes it says which node — or which whole contour — to add or
remove to keep the two halves in step, and hands back a freshly derived pairing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from azsym_binding import Binding, NodeRef, Pair, Paths, pair_nodes, stale_paths
from azsym_geometry import (
    HORIZONTAL,
    ROTATE,
    TOL,
    VERTICAL,
    Point,
    clamp,
    close,
    contour_side,
    moves_as,
    partner,
    reverses_traversal,
    unshear,
)
from azsym_topology import (
    CONTOUR_ADDED,
    CONTOUR_REMOVED,
    CURVE_JOIN,
    CURVE_SPLIT,
    DELETED,
    DELETED_SEVERAL,
    HANDLE_PAIR,
    INSERTED,
    OFFCURVE,
    SEGMENT_CURVED,
    SPLIT_SIZE,
    TYPE_CHANGED,
    UNCHANGED,
    Change,
    Structure,
    decode_type,
    encode_type,
    kept_indices,
    split_cluster,
)

# Rules are re-run until nothing changes. Two perpendicular symmetries settle in
# two passes; the cap only exists so a pathological binding cannot spin forever.
MAX_ITER = 4

# How far a stored pair may be from symmetric ABOUT THE CURRENT AXIS before it
# is refused, in font units. Node counts alone cannot detect a reordered contour
# or a reversed path: both keep the counts intact while every stored index
# points somewhere else. Without this guard, such an edit leaves the binding
# "healthy" and the next one-unit nudge teleports its partner across the glyph.
#
# NOT TOL. The floor here is grid quantization: a written position is snapped to
# the layer grid, losing up to half a grid step per node, so a pair whose two
# ends were both written can sit a full grid step off a true mirror while being
# entirely healthy. At TOL every slanted axis would be permanently blocked.
#
# Expressed as a share of the EM rather than as a raw unit count (Hugo,
# 2026-08-11), so a 2048-upm font gets a tolerance on its own scale instead of
# one sized for a 1000-upm font — the same move ``azsym_scan.safe_units`` makes.
# The grid is still not read: a coarse-grid font would want this taken from
# layer.gridLength, and nothing does that yet.
DEFAULT_UPM = 1000.0
GUARD_TOL_PER_UPM = 0.02


def guard_tol(upm: float = DEFAULT_UPM) -> float:
    """The stale-pair tolerance, in the units of a font of this em size.

    A non-positive or unreadable ``upm`` falls back to ``DEFAULT_UPM``. A
    tolerance of zero would hold every slanted axis permanently red, so a font
    that cannot say how big its em is is given the ordinary rule instead.
    """
    return GUARD_TOL_PER_UPM * (upm if upm and upm > 0 else DEFAULT_UPM)


# The default-em value, which is what ``sync`` falls back to when a caller has
# no font to ask. Derived rather than written out, so the two cannot drift.
GUARD_TOL = guard_tol()


@dataclass(frozen=True)
class Axis:
    """One bound symmetry: its pairing, its centre, which flip it enforces, and
    the shear that centre and flip are expressed in."""

    binding: Binding
    centre: Point
    mode: str = VERTICAL
    shear: float = 0.0
    # Which side the designer draws. The OTHER side is the one this binding
    # generates, and the one the caller shades.
    sign: int = -1
    # A half-turn bound about a selected guide splits on the guide's line
    # (azsym_geometry.axis_split; decision 7 of the guide-axis spec). None is
    # the box's "\" diagonal, which every other binding splits on.
    split: Point | None = None


def split_of(axis: Axis, extent: Point) -> Point:
    """What splits ``axis`` into the half drawn and the half generated: its
    guide's line when it was bound about one, else the box ``extent`` whose
    diagonal a half-turn has always split on. Only a half-turn reads it."""
    return axis.split if axis.split is not None else extent


@dataclass(frozen=True)
class SyncResult:
    writes: dict[NodeRef, Point]
    snapshot: dict[NodeRef, Point]
    stale: bool
    # Indices into the ``axes`` list passed to sync(). The caller draws these
    # red: an axis that has stopped mirroring must never look healthy.
    stale_axes: frozenset[int] = frozenset()
    # Set when the whole outline moved rigidly this frame. The caller must move
    # every axis centre by it — see translated_axes — and store the result.
    translation: Point | None = None
    # {axis index: new centre} for an axis whose whole generated half the
    # designer moved this frame (see _recentred). The caller stores it.
    recentred: dict[int, Point] = field(default_factory=dict)


def _tracked(axes: list[Axis]) -> set[NodeRef]:
    """Every node any axis has an opinion about."""
    refs: set[NodeRef] = set()
    for axis in axes:
        for src_path, src_node, dst_path, dst_node in axis.binding.pairs:
            refs.add((src_path, src_node))
            refs.add((dst_path, dst_node))
        refs.update(axis.binding.onaxis)
    return refs


def sync(
    paths: Paths,
    axes: list[Axis],
    previous: dict[NodeRef, Point] | None,
    tol: float = TOL,
    max_iter: int = MAX_ITER,
    guard_tol: float = GUARD_TOL,
) -> SyncResult:
    """Mirror this frame's edits across every bound symmetry.

    Returns the nodes to rewrite, the snapshot to keep for the next frame,
    whether any binding has gone stale, and which axes are the stale ones. A
    binding whose node counts no longer match writes nothing at all — stored
    node indices no longer describe the layer, so acting on them would corrupt
    the outline.

    Individual pairs are checked too: one that was not already symmetric LAST
    frame is skipped and its axis flagged stale. That is the only defence
    against a contour being reordered or a path reversed — both leave the node
    counts intact while making every stored index point at the wrong node.
    """
    count_stale = {index for index, axis in enumerate(axes) if axis_is_stale(axis, paths)}

    positions: dict[NodeRef, Point] = {}
    for ref in _tracked(axes):
        path_index, node_index = ref
        if path_index < len(paths) and node_index < len(paths[path_index]):
            positions[ref] = paths[path_index][node_index]

    if count_stale:
        return SyncResult(
            writes={},
            snapshot=positions,
            stale=True,
            stale_axes=frozenset(range(len(axes))),
        )

    if previous is None:
        return SyncResult(writes={}, snapshot=positions, stale=False)

    # Before anything is mirrored: the whole outline may simply have MOVED.
    moved_by = _translation(previous, positions, tol)
    if moved_by is not None:
        return SyncResult(writes={}, snapshot=positions, stale=False, translation=moved_by)

    # Judged against LAST frame, so about the axis as it was.
    stale_axes: set[int] = set()
    symmetric: list[tuple[int, Pair]] = []
    for index, axis in enumerate(axes):
        for pair in axis.binding.pairs:
            if _was_symmetric(previous, pair, axis, guard_tol):
                symmetric.append((index, pair))
            else:
                stale_axes.add(index)

    user_moved = {
        ref
        for ref, point in positions.items()
        if ref in previous and not close(previous[ref], point, tol)
    }
    derived: set[NodeRef] = set()

    # Mirrored and clamped about the axis as it is NOW, which only differs from
    # the one above where the designer moved a whole generated half.
    recentred: dict[int, Point] = {}
    moved_axes = list(axes)
    for index, axis in enumerate(axes):
        if index in stale_axes:
            continue
        centre = _recentred(axis, previous, positions, user_moved, tol)
        if centre is not None:
            recentred[index] = centre
            moved_axes[index] = replace(axis, centre=centre)
    trusted = [(moved_axes[index], pair) for index, pair in symmetric]

    for _ in range(max_iter):
        changed = False
        for axis, (src_path, src_node, dst_path, dst_node) in trusted:
            first, second = (src_path, src_node), (dst_path, dst_node)
            source = _choose_source(first, second, user_moved, derived)
            if source is None:
                continue
            target = second if source == first else first
            wanted = partner(positions[source], axis.centre, axis.mode, axis.shear)
            if not close(positions[target], wanted, tol):
                positions[target] = wanted
                derived.add(target)
                changed = True
        if not changed:
            break

    for index, axis in enumerate(axes):
        for ref in axis.binding.onaxis:
            # A re-centred axis takes its on-axis nodes along, selected or not.
            if ref not in user_moved and ref not in derived and index not in recentred:
                continue
            # Same guard, one node wide: an on-axis node only stays on-axis
            # while the pairing still describes the outline.
            if ref not in previous or not close(
                previous[ref],
                clamp(previous[ref], axis.centre, axis.mode, axis.shear),
                guard_tol,
            ):
                stale_axes.add(index)
                continue
            now = moved_axes[index]
            clamped = clamp(positions[ref], now.centre, now.mode, now.shear)
            if not close(positions[ref], clamped, tol):
                positions[ref] = clamped
                derived.add(ref)

    writes = {ref: positions[ref] for ref in derived}
    return SyncResult(
        writes=writes,
        snapshot=positions,
        stale=bool(stale_axes),
        stale_axes=frozenset(stale_axes),
        recentred=recentred,
    )


def _recentred(
    axis: Axis,
    previous: dict[NodeRef, Point],
    positions: dict[NodeRef, Point],
    user_moved: set[NodeRef],
    tol: float,
) -> Point | None:
    """Where ``axis``'s centre goes when the designer moved its WHOLE generated
    half as one block, or None.

    Hugo, 2026-10-10, on a /D bound top-to-bottom: "I would like to re align top
    part with Cap height" — select the top half and shift it down, and it sprang
    back, the generated half being read-only. Moving all of it together is not
    an edit to undo: it says where the generated half belongs. So the axis goes
    HALFWAY (his pick from three, rendered on that /D): the generated half lands
    where it was put, the kept half stays — the D stays on its baseline — and
    the on-axis nodes follow the axis (``sync`` clamps them, selected or not).

    Every generated node, one delta, no kept node moved: moving part of the half
    still springs back, and a move of everything is ``_translation``'s. A mirror
    takes only the delta ACROSS its axis, in the unsheared frame where centres
    live, and only when that is the larger part of it — a half pushed along the
    axis cannot be a symmetric shape, and an italic's upright nudge of a
    VERTICAL mirror's half must not slide the axis sideways. A half-turn takes
    the whole delta.
    """
    kept = [(src_path, src_node) for src_path, src_node, _dp, _dn in axis.binding.pairs]
    generated = [(dst_path, dst_node) for _sp, _sn, dst_path, dst_node in axis.binding.pairs]
    if not generated or any(ref in user_moved for ref in kept):
        return None
    delta: Point | None = None
    for ref in generated:
        if ref not in user_moved:
            return None
        moved = (positions[ref][0] - previous[ref][0], positions[ref][1] - previous[ref][1])
        if delta is None:
            delta = moved
        elif not close(moved, delta, tol):
            return None
    if delta is None:
        return None
    dx, dy = unshear(delta, axis.shear)
    x, y = axis.centre
    mode = moves_as(axis.mode)
    if mode == VERTICAL:
        return (x + dx / 2.0, y) if abs(dx) > abs(dy) else None
    if mode == HORIZONTAL:
        return (x, y + dy / 2.0) if abs(dy) > abs(dx) else None
    return (x + dx / 2.0, y + dy / 2.0)


def _translation(
    previous: dict[NodeRef, Point],
    positions: dict[NodeRef, Point],
    tol: float,
) -> Point | None:
    """The delta EVERY tracked node moved by, or None if they did not all share one.

    A stored centre is an absolute position, and the outline it describes is
    not: changing the left sidebearing translates every path in the layer while
    the centre stays where it was. The mirror is then taken about a line that is
    no longer in the middle of the shape, and the generated half is written back
    the other way by twice the shift — the drawn half follows the sidebearing,
    the mirrored half moves against it, and the glyph folds up. Selecting
    everything and nudging it with the arrow keys does the same thing.

    So a frame where every tracked node shares one delta is not an edit to
    mirror, it is the shape moving: the caller carries the axis along with it
    and nothing is written.

    Cheap to tell apart from a real edit. Dragging one side moves the source
    nodes and leaves the generated ones where they are; only a rigid move of
    the whole outline gives every node the same delta. Selecting BOTH halves and
    moving them is a rigid move, and the axis should travel with that too.

    Two nodes minimum: one node moving is a plain edit, and there is nothing to
    agree with it. Refuses a snapshot that does not cover exactly this frame's
    tracked set, because a delta cannot be read off a node that was not there.
    """
    if len(positions) < 2 or len(positions) != len(previous):
        return None
    delta: Point | None = None
    for ref, point in positions.items():
        if ref not in previous:
            return None
        was = previous[ref]
        moved = (point[0] - was[0], point[1] - was[1])
        if delta is None:
            delta = moved
        elif not close(moved, delta, tol):
            return None
    if delta is None or close(delta, (0.0, 0.0), tol):
        return None
    return delta


def translated_axes(axes: list[Axis], delta: Point) -> list[Axis]:
    """Every axis with its centre carried along by a LAYER-space ``delta``.

    A centre is held in the unsheared frame, so the delta is unsheared before it
    is added. ``unshear`` is linear, which is what lets it be applied to a
    difference of two points rather than to a point.
    """

    def moved(centre: Point, shear: float) -> Point:
        dx, dy = unshear(delta, shear)
        return (centre[0] + dx, centre[1] + dy)

    return [replace(axis, centre=moved(axis.centre, axis.shear)) for axis in axes]


def _was_symmetric(
    previous: dict[NodeRef, Point],
    pair: Pair,
    axis: Axis,
    guard_tol: float,
) -> bool:
    """True when this pair was still symmetric last frame.

    Every flip is an involution, so checking one end against the other covers
    both directions. A pair either end of which is missing from ``previous`` is
    refused: unknown is not the same as fine.
    """
    src_path, src_node, dst_path, dst_node = pair
    src, dst = (src_path, src_node), (dst_path, dst_node)
    if src not in previous or dst not in previous:
        return False
    wanted = partner(previous[src], axis.centre, axis.mode, axis.shear)
    return close(wanted, previous[dst], guard_tol)


def _choose_source(
    first: NodeRef,
    second: NodeRef,
    user_moved: set[NodeRef],
    derived: set[NodeRef],
) -> NodeRef | None:
    """Which end of a pair leads this frame, or None to leave the pair alone.

    ``first`` is the stored SOURCE end — pair_nodes orients every pair so the
    source side leads — and ``second`` is the generated one.

    The generated end is READ-ONLY. Drag it and it is rewritten from the source
    on the same frame, so it springs back: that is what makes the mirrored half
    of the outline behave as not-editable, without fighting Glyphs' selection
    model by deselecting nodes under the user's cursor.

    The one case where the generated end may lead is when ANOTHER axis derived
    it this frame. Two composed symmetries share nodes — a corner is generated
    by one and the source for the other — and refusing to propagate there would
    stop one corner from driving the other three.
    """
    if first in user_moved or second in user_moved:
        return first

    first_derived, second_derived = first in derived, second in derived
    if first_derived and not second_derived:
        return first
    if second_derived and not first_derived:
        return second
    return None


def stranded_handles(axis: Axis, structure: Structure, selected: set[Point]) -> list[NodeRef]:
    """Generated handles a selected node drags along but cannot move.

    Hugo, 2026-10-22, on an O bound as a half-turn split at a typed angle: "if I
    move the node, the top handle will stay in place". Glyphs drags a node's
    handles with it, and a generated handle is rewritten from its source on the
    next frame. When that source is neither the node nor its other handle, it
    stays put, so the handle springs back and the curve kinks. A node on a
    mirror's axis is fine: its far handle's source is its near one.

    ``selected`` holds positions; a selected node that is itself generated
    springs back whole and is not asked about.
    """
    if not selected:
        return []
    source_of = {
        (dst_path, dst_node): (src_path, src_node)
        for src_path, src_node, dst_path, dst_node in axis.binding.pairs
    }
    stranded: list[NodeRef] = []
    for path_index, (points, types) in enumerate(
        zip(structure.paths, structure.types, strict=True)
    ):
        count = len(points)
        for index, point in enumerate(points):
            node = (path_index, index)
            if types[index] == OFFCURVE or point not in selected or node in source_of:
                continue
            handles = [
                (path_index, neighbour)
                for neighbour in ((index - 1) % count, (index + 1) % count)
                if types[neighbour] == OFFCURVE
            ]
            moving = {node, *handles}
            stranded.extend(
                handle
                for handle in handles
                if handle in source_of
                and source_of[handle] not in moving
                and handle not in stranded
            )
    return stranded


def axis_label(stale: bool, unpaired: int) -> str | None:
    """Text to draw beside a bound axis, or None when it has nothing to say.

    A binding with unpaired nodes is not mirroring those nodes, and a user must
    never be shown a healthy-looking axis that is quietly doing nothing. A
    binding with NO pairs at all is the extreme of that: it looks bound and
    writes nothing whatsoever.
    """
    if stale:
        return "re-bind"
    if unpaired == 1:
        return "1 node unpaired"
    if unpaired > 1:
        return f"{unpaired} nodes unpaired"
    return None


def next_snapshot(
    result: SyncResult,
    actual: dict[NodeRef, Point],
) -> dict[NodeRef, Point]:
    """The snapshot to keep for the next frame, given what the layer ACTUALLY
    stored — which is not what this frame asked for.

    Writing a node position goes through the host's grid: ask for
    ``(30.3, 40.7)`` on a unit grid and the node reads back ``(30.0, 41.0)``.

    Store what sync INTENDED and the next frame reads the mirrored node as
    user-moved, so the plugin rewrites it again — every frame, for as long as
    the binding exists. The positions do settle (the grid that causes the error
    also absorbs the correction), so the symptom is not a teleport but endless
    churn: a permanently dirty document and per-frame work that should have
    stopped. Storing what the layer actually kept ends it on frame two.

    ``actual`` only has to cover the nodes that were written; everything else in
    the snapshot was read straight from the layer and is already true.
    """
    merged = dict(result.snapshot)
    merged.update(actual)
    return merged


def axis_is_stale(axis: Axis, paths: Paths) -> bool:
    """True when this axis's stored indices no longer describe the layer.

    Public because plugin.py calls it per axis to colour the drawn line.
    """
    changed = stale_paths(axis.binding, paths)
    return changed is None or bool(changed)


def out_of_step(axis: Axis, paths: Paths, tol: float = GUARD_TOL) -> int:
    """How many of this axis's pairs do NOT mirror each other RIGHT NOW.

    ``sync`` asks the same question of the PREVIOUS frame, because mid-drag the
    half being dragged is legitimately out of step for one frame. This asks it
    of the outline as it stands, which is the only form of the question that
    means anything when a document has just been opened and there is no previous
    frame to compare against.

    That is the whole gap this fills. A binding whose pairs stopped mirroring —
    because the file was edited somewhere without the plugin — is not
    ``axis_is_stale``: the node counts are untouched, so the stored indices
    still describe the layer perfectly well. It stops mirroring on the SECOND
    frame, when ``_was_symmetric`` refuses the pair. Before that nobody has
    asked, and the designer has no way to know the symmetry went dormant unless
    they happen to open the glyph and see the axis drawn red.

    Assumes the binding is not structurally stale; check ``axis_is_stale``
    first. A pair whose nodes are missing from the layer counts as out of step,
    which is the safe way round — unknown is not the same as fine.
    """
    positions: dict[NodeRef, Point] = {}
    for ref in _tracked([axis]):
        path_index, node_index = ref
        if path_index < len(paths) and node_index < len(paths[path_index]):
            positions[ref] = paths[path_index][node_index]
    return sum(1 for pair in axis.binding.pairs if not _was_symmetric(positions, pair, axis, tol))


INSERT_NODE = "insert-node"
DELETE_NODE = "delete-node"
# Splitting a curve MOVES the segment's two original handles as well as adding
# three entries, so the mirrored half needs its matching two handles moved or it
# is no longer the mirror of what the designer drew. That is a write, not an
# insert, and it has to travel with the inserts to be applied in step with them.
MOVE_NODE = "move-node"
# A WHOLE contour, not a node. Drawing a new path that is not symmetric in
# itself needs a mirrored twin of the entire thing, and deleting one needs the
# twin taken back out; neither is expressible as a node index.
ADD_CONTOUR = "add-contour"
REMOVE_CONTOUR = "remove-contour"
# A node's encoded TYPE, and nothing else. Toggling a node between smooth and
# sharp moves nothing at all, so it can reach the generated half only as an edit
# of its own: there is no position for the position sync to carry it in.
RETYPE_NODE = "retype-node"

# How many nodes a contour needs before a twin is built for it. Two nodes
# enclose nothing and one is the first click of the Draw tool, which arrives as
# a whole new contour on the very frame it is created — twin that and the twin
# is stranded at one node for good, because every LATER click arrives as an
# insert and _mirror_insert cannot place anything into a contour that short.
# The designer is then left with a stray one-node contour AND no mirroring.
MIN_TWIN_NODES = 3


@dataclass(frozen=True)
class StructuralEdit:
    """One node — or one whole contour — to add to, remove from, or move.

    ``index`` is an index into the CURRENT outline — the one the caller is
    about to modify — not into the binding's stored indices, and it is read
    AFTER every edit before it in the same ``ReconcileResult`` has been applied.

    ADD_CONTOUR and REMOVE_CONTOUR name a whole contour through ``path`` and
    leave ``index`` unused. ADD_CONTOUR carries the twin's nodes in ``points``
    and ``node_types``, which are the same length and in path order, and its
    ``path`` is the slot the appended contour will occupy — a new contour always
    goes on the END, which is where ``layer.shapes.append`` puts it.

    RETYPE_NODE names an EXISTING node and carries its whole new encoded type in
    ``node_type``; ``position`` is unused, because a retype moves nothing.

    ``node_type`` is always the ENCODED string — type and connection together,
    per ``azsym_topology.encode_type`` — never the bare type.
    """

    kind: str
    path: int
    index: int
    position: Point = (0.0, 0.0)
    node_type: str = ""
    points: tuple[Point, ...] = ()
    node_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconcileResult:
    """The axes to keep, and the edits that make the outline match them.

    ``edits`` are ORDERED and must be applied in the order given, one after the
    other. Each ``index`` names the outline as it reads once every edit BEFORE
    it in the tuple has been applied — not the outline the caller started with.

    A curve split emits three inserts at CONSECUTIVE ascending indices, so
    applying them in order lays the new nodes down in path order; applying them
    against the original indexing instead reverses the two handles, which is a
    curve that doubles back. The moves that follow are numbered past those
    inserts. ``_with_edits`` applies them exactly this way and is the reference
    for what the caller must do to the real path.

    A whole contour is the one edit whose ``path`` is not a node container.
    ADD_CONTOUR is APPENDED, so its ``path`` names the slot one past the end;
    REMOVE_CONTOUR deletes the contour at ``path``. Both are answered before
    ``path`` is ever used to look a contour's nodes up.
    """

    axes: tuple[Axis, ...]
    edits: tuple[StructuralEdit, ...]


def _partner_of(binding: Binding, ref: NodeRef) -> NodeRef | None:
    """The other end of ``ref``'s pair, whichever end ``ref`` is."""
    for src_path, src_node, dst_path, dst_node in binding.pairs:
        if (src_path, src_node) == ref:
            return (dst_path, dst_node)
        if (dst_path, dst_node) == ref:
            return (src_path, src_node)
    return None


def _image_of(binding: Binding, ref: NodeRef) -> NodeRef | None:
    """Where ``ref`` lands under the symmetry, INCLUDING when that is itself.

    An on-axis node is its own image. It lives in ``onaxis`` rather than in
    ``pairs``, so a pairs-only lookup reports it as having no partner — and a
    node on the axis is precisely where a symmetric outline crosses it, which
    makes the two segments meeting there ordinary places to insert into, not
    exotic ones.

    Deliberately NOT used for deletes: answering "itself" there would hand back
    the index of the very node the user has already removed.
    """
    paired = _partner_of(binding, ref)
    if paired is not None:
        return paired
    return ref if ref in binding.onaxis else None


def _rederived(paths: Paths, axes: list[Axis]) -> tuple[Axis, ...]:
    """Every axis re-paired against ``paths``, WITHOUT touching the outline.

    Binding snaps the shape; re-deriving must not. A routine Correct Path
    Direction pass would otherwise force-snap every glyph in the font.
    """
    return tuple(
        Axis(
            binding=pair_nodes(paths, axis.centre, axis.mode, axis.shear, axis.sign, axis.split),
            centre=axis.centre,
            mode=axis.mode,
            shear=axis.shear,
            sign=axis.sign,
            split=axis.split,
        )
        for axis in axes
    )


def _with_edits(paths: Paths, edits: tuple[StructuralEdit, ...]) -> Paths:
    """``paths`` as the caller's outline will read once ``edits`` are applied.

    Re-deriving against the outline as it stands NOW would pair the user's new
    node against whatever happens to fall opposite it, and would hand back a
    binding whose node counts do not match the outline the caller ends up with
    — which every later frame reads as stale.

    SEQUENTIAL, in the order the edits arrive, each index read against the
    result of the ones before it. That is the contract stated on
    ReconcileResult, and this function is what the caller's own applier has to
    agree with: read the indices against the ORIGINAL outline instead and a
    curve split's three inserts land back-to-front.

    An unrecognised kind is skipped rather than guessed at. This function only
    predicts; predicting the wrong thing would hand back a binding built for an
    outline nobody produces.
    """
    if not edits:
        return paths
    amended = [list(path) for path in paths]
    for edit in edits:
        # Both contour kinds are answered before ``edit.path`` is used as a node
        # container: ADD_CONTOUR names the slot the new contour will OCCUPY,
        # which is one past the end of the list it is read against.
        if edit.kind == ADD_CONTOUR:
            amended.append(list(edit.points))
            continue
        if edit.kind == REMOVE_CONTOUR:
            if edit.path < len(amended):
                del amended[edit.path]
            continue
        if edit.kind == RETYPE_NODE:
            # A retype is not a move: ``paths`` carries POSITIONS only, so there
            # is nothing here for it to change. Named explicitly rather than
            # left to the skip-the-unknown rule, because it is a kind this
            # module knows about and silence would read as an oversight.
            continue
        nodes = amended[edit.path]
        if edit.kind == DELETE_NODE:
            if edit.index < len(nodes):
                del nodes[edit.index]
        elif edit.kind == MOVE_NODE:
            if edit.index < len(nodes):
                nodes[edit.index] = edit.position
        elif edit.kind == INSERT_NODE:
            nodes.insert(edit.index, edit.position)
    return amended


def _current_index(index: int, inserted_at: int) -> int:
    """A PREVIOUS-frame node index read in CURRENT-frame terms.

    The user's node is already in the outline, so every node from the insertion
    point onwards has moved up by one.
    """
    return index + 1 if index >= inserted_at else index


def _shared_gap_index(
    axis: Axis,
    previous: Structure,
    structure: Structure,
    change: Change,
    opening: int,
) -> int:
    """Where the mirror goes when it lands in the very gap the user drew into.

    That happens whenever the gap straddles the symmetry: the flip maps the
    segment onto itself, back to front. Walking it from its opening node to its
    closing one, the mirror sits as far from the closing node as the new node
    sits from the opening one — so the new node comes FIRST exactly when it
    shares a side with the opening node.
    """
    inserted = structure.paths[change.path][change.index]
    new_side = contour_side(inserted, axis.centre, axis.mode, axis.shear)
    opener = previous.paths[change.path][opening]
    opening_side = contour_side(opener, axis.centre, axis.mode, axis.shear)
    if new_side * opening_side > 0:
        return change.index + 1
    # The mirror comes first. At index 0 that would renumber the whole contour
    # and move its start node; appending names the same ring position.
    if change.index == 0:
        return len(structure.paths[change.path])
    return change.index


def _mirror_insert(
    axis: Axis,
    previous: Structure,
    structure: Structure,
    change: Change,
) -> StructuralEdit | None:
    """Where the partner of a newly inserted node belongs, in CURRENT indices.

    The new node sits in the gap between two existing ones. Its mirror belongs
    in the gap between THOSE TWO NODES' IMAGES, or the mirrored contour crosses
    itself. Which of the two images the insertion point counts from is not the
    lower-numbered one: it is whichever the walk reaches first, and a symmetry
    that reverses the walk swaps the two.

    Returns None when the neighbours have no images, when those images are not
    the two ends of one gap, or when the new node sits on the axis and is its
    own partner: the caller then simply re-derives, which leaves the new node
    unpaired rather than guessing.
    """
    count = len(previous.paths[change.path])
    if count < 2:
        return None

    inserted = structure.paths[change.path][change.index]
    mirrored = partner(inserted, axis.centre, axis.mode, axis.shear)
    if close(inserted, mirrored):
        # A node ON the axis is its own partner. "Mirroring" it would stack a
        # second node on the same coordinates.
        return None

    # The gap the user drew into, named by the PREVIOUS nodes either side of it.
    # The one before kept its index; the one after was at change.index before
    # being pushed up. change.index == count means the closing gap, appended to.
    opening = (change.index - 1) % count
    closing = change.index % count
    from_opening = _image_of(axis.binding, (change.path, opening))
    from_closing = _image_of(axis.binding, (change.path, closing))
    if from_opening is None or from_closing is None:
        return None
    if from_opening[0] != from_closing[0]:
        return None  # the two ends mirror onto different contours, so there is no gap

    target_path = from_opening[0]
    target_count = len(previous.paths[target_path])
    if target_count < 2:
        return None

    # Which end of the image gap the walk reaches first. Read off the pairing
    # rather than from the mode: a half-turn preserves the walk and a mirror
    # reverses it, but pair_nodes will also pair two contours that were drawn
    # running the SAME way round, and that reverses the reversal.
    preserved = from_closing[1] == (from_opening[1] + 1) % target_count
    reversed_walk = from_opening[1] == (from_closing[1] + 1) % target_count
    if not preserved and not reversed_walk:
        # The two images are not the two ends of ONE gap, so the stored pairing
        # does not describe this edit and a guess would be worse than nothing.
        return None
    if preserved and reversed_walk:
        # A two-node contour, where either ordering is adjacent. Fall back to
        # what this symmetry does to a walk.
        preserved = not reverses_traversal(axis.mode)
    start = from_opening if preserved else from_closing

    if target_path == change.path and {from_opening[1], from_closing[1]} == {opening, closing}:
        if preserved:
            # The gap maps onto itself the same way round, so every point on it
            # is its own image and there is no second node to place.
            return None
        index = _shared_gap_index(axis, previous, structure, change, opening)
    elif target_path == change.path:
        index = _current_index(start[1], change.index) + 1
    else:
        # A different contour is untouched by the user's insert, so its stored
        # indices are already current. One past the gap's opening node — never
        # wrapped to 0, which would renumber that whole contour.
        index = start[1] + 1

    return StructuralEdit(
        kind=INSERT_NODE,
        path=target_path,
        index=index,
        position=mirrored,
        node_type=change.node_type,
    )


def _in_current(index: int, kept: list[int], same_contour: bool) -> int:
    """A PREVIOUS index read in CURRENT terms, given what the user's edit added.

    ``kept`` is the whole translation for the edited contour, so it covers a
    block that straddles the end of the list as naturally as one in the middle.
    A contour the user did not touch needs no translation at all.
    """
    return kept[index] if same_contour else index


def _mirror_curve_split(
    axis: Axis,
    previous: Structure,
    structure: Structure,
    change: Change,
) -> tuple[StructuralEdit, ...]:
    """The mirror of a curve split: three nodes to insert, two handles to move.

    Glyphs subdivides the segment, so what the designer's half now holds is
    ``A, Q0, R0, S, R1, Q2, D`` where ``Q0`` and ``Q2`` are the ORIGINAL handles
    in recomputed positions. The mirrored half has to become the mirror of all
    of that: inserting only the three new entries leaves the mirrored curve with
    the pre-split handles and it bulges away from the shape it is supposed to
    copy — which is why two moves travel with the inserts.

    The three go into the gap between the IMAGES of the two handles bracketing
    the split, in the order that gap is walked. Under a mirror the walk reverses
    and the block goes in back-to-front; the two handles are not
    interchangeable, and swapping them is a curve that doubles back.

    Returns an empty tuple whenever the answer cannot be trusted — the caller
    then re-derives, which leaves the new nodes unpaired rather than guessing.
    """
    before, after = previous.paths[change.path], structure.paths[change.path]
    count = len(after)
    if len(before) < 4 or count != len(before) + SPLIT_SIZE:
        return ()
    cluster = split_cluster(change.index, count)
    outer = ((change.index - 2) % count, (change.index + 2) % count)
    if len({*cluster, *outer}) != 5:
        return ()
    kept = kept_indices(count, cluster)
    if len(kept) != len(before):
        return ()

    # The gap the split landed in, named by the PREVIOUS nodes bracketing it:
    # the segment's two handles, in their pre-subdivision positions.
    opening, closing = kept.index(outer[0]), kept.index(outer[1])
    if closing != (opening + 1) % len(before):
        return ()

    from_opening = _image_of(axis.binding, (change.path, opening))
    from_closing = _image_of(axis.binding, (change.path, closing))
    if from_opening is None or from_closing is None:
        return ()
    if from_opening[0] != from_closing[0]:
        return ()  # the two ends mirror onto different contours, so there is no gap
    target_path = from_opening[0]
    same_contour = target_path == change.path
    if same_contour and any(
        not 0 <= image[1] < len(kept) for image in (from_opening, from_closing)
    ):
        return ()  # a stored index the previous frame cannot even be read for
    if same_contour and {from_opening[1], from_closing[1]} & {opening, closing}:
        # The segment maps onto ITSELF. Its mirror is a SECOND split of the same
        # curve — three more entries with every handle recomputed again — not
        # three nodes dropped into it. Nothing here can produce that, and an
        # insert would leave a curve that does not pass through its own nodes.
        return ()

    target_count = len(previous.paths[target_path])
    if target_count < 4:
        return ()  # too short to hold the mirror of a cubic segment
    preserved = from_closing[1] == (from_opening[1] + 1) % target_count
    reversed_walk = from_opening[1] == (from_closing[1] + 1) % target_count
    if preserved == reversed_walk:
        # The two images are not the two ends of ONE gap, so the stored pairing
        # does not describe this edit and a guess would be worse than nothing.
        return ()
    start = from_opening if preserved else from_closing

    block = [(after[slot], structure.types[change.path][slot]) for slot in cluster]
    if not preserved:
        # Walking the image gap from its own opening node meets the images of
        # the three in the opposite order to the one they were drawn in.
        block.reverse()
    base = _in_current(start[1], kept, same_contour) + 1
    edits = [
        StructuralEdit(
            kind=INSERT_NODE,
            path=target_path,
            index=base + offset,
            position=partner(point, axis.centre, axis.mode, axis.shear),
            node_type=node_type,
        )
        for offset, (point, node_type) in enumerate(block)
    ]

    for image, moved in ((from_opening, outer[0]), (from_closing, outer[1])):
        index = _in_current(image[1], kept, same_contour)
        if not 0 <= index < len(structure.paths[target_path]):
            return ()
        wanted = partner(after[moved], axis.centre, axis.mode, axis.shear)
        if close(structure.paths[target_path][index], wanted):
            continue
        edits.append(
            StructuralEdit(
                kind=MOVE_NODE,
                path=target_path,
                # Read after the inserts above, per the ReconcileResult contract.
                index=index + len(cluster) if index >= base else index,
                position=wanted,
            )
        )
    return tuple(edits)


def _after_insertion(index: int, inserted_at: int, same_contour: bool) -> int:
    """A PREVIOUS index read in CURRENT terms, once a handle pair went in.

    The pair sits between two nodes, so every node from the insertion point on
    has moved up by two. A contour the user did not touch needs no translation.
    """
    if not same_contour:
        return index
    return index + HANDLE_PAIR if index >= inserted_at else index


def _mirror_curved_segment(
    axis: Axis,
    previous: Structure,
    structure: Structure,
    change: Change,
) -> tuple[StructuralEdit, ...]:
    """The mirror of a line pulled into a curve: two handles, and one retype.

    The retype is the half that cannot be left out. A node's type names the
    segment ARRIVING at it, so mirroring the handles alone gives the generated
    side two off-curve nodes sitting inside a segment still declared straight —
    an outline Glyphs draws as a line with two stray points on it.

    Which node it lands on is not the partner of the one the designer retyped.
    A mirror REVERSES the walk, so the image segment runs from the image of the
    far end back to the image of the near one, and the node it arrives at is the
    image of the node the designer's segment LEFT. Getting this backwards
    straightens the wrong segment and bends one that was never touched — which
    is why ``_mirror_type_changed`` refuses a bare type change outright and
    leaves the case to this function.

    Returns an empty tuple whenever the answer cannot be trusted; the caller
    then re-derives, which leaves the new handles unpaired rather than guessing.
    """
    before, after = previous.paths[change.path], structure.paths[change.path]
    count = len(before)
    if count < 2 or len(after) != count + HANDLE_PAIR:
        return ()
    first = change.index
    if not 0 <= first < len(after) - 1:
        return ()

    # The segment the designer curved, named by the PREVIOUS nodes at its ends.
    arrival = first % count
    leaves = (arrival - 1) % count
    from_leaves = _image_of(axis.binding, (change.path, leaves))
    from_arrival = _image_of(axis.binding, (change.path, arrival))
    if from_leaves is None or from_arrival is None:
        return ()
    if from_leaves[0] != from_arrival[0]:
        return ()  # the two ends mirror onto different contours, so there is no gap

    target_path = from_leaves[0]
    same_contour = target_path == change.path
    if same_contour and {from_leaves[1], from_arrival[1]} == {leaves, arrival}:
        # The segment maps onto ITSELF — it crosses the axis. Its mirror is the
        # same two handles reflected onto each other, which is a constraint on
        # the pair the designer just made, not a second pair to insert.
        return ()

    target_count = len(previous.paths[target_path])
    if target_count < 2:
        return ()
    preserved = from_arrival[1] == (from_leaves[1] + 1) % target_count
    reversed_walk = from_leaves[1] == (from_arrival[1] + 1) % target_count
    if preserved == reversed_walk:
        # The two images are not the two ends of ONE gap, so the stored pairing
        # does not describe this edit and a guess would be worse than nothing.
        return ()

    # Whichever image the walk meets first opens the gap the handles go into;
    # the other is the node the mirrored segment ARRIVES at, and the one to
    # retype. Under a mirror that is the image of the node the user's segment
    # left, not the image of the one they retyped.
    start, ends_at = (from_leaves, from_arrival) if preserved else (from_arrival, from_leaves)

    handles = [after[first], after[first + 1]]
    if not preserved:
        # Walked from its own opening node, the image gap meets the two handles
        # in the opposite order to the one they were drawn in. Swapping them is
        # a curve that doubles back through the glyph.
        handles.reverse()

    base = _after_insertion(start[1], first, same_contour) + 1
    if not 0 <= base <= len(structure.paths[target_path]):
        return ()
    edits: list[StructuralEdit] = [
        StructuralEdit(
            kind=INSERT_NODE,
            path=target_path,
            index=base + offset,
            position=partner(point, axis.centre, axis.mode, axis.shear),
            node_type=OFFCURVE,
        )
        for offset, point in enumerate(handles)
    ]

    target = _after_insertion(ends_at[1], first, same_contour)
    if not 0 <= target < len(structure.types[target_path]):
        return ()
    if decode_type(structure.types[target_path][target])[0] == OFFCURVE:
        # The pairing points at a handle. Retyping one to a curve would be a
        # contour Glyphs cannot draw.
        return ()
    edits.append(
        StructuralEdit(
            kind=RETYPE_NODE,
            path=target_path,
            # Read after the inserts above, per the ReconcileResult contract.
            index=target + HANDLE_PAIR if target >= base else target,
            node_type=change.node_type,
        )
    )
    return tuple(edits)


def _after_removal(index: int, cluster: tuple[int, ...]) -> int:
    """A PREVIOUS index read in CURRENT terms, once ``cluster`` has been removed.

    Every removed member that sat before it pulls it down by one. The same
    answer ``kept_indices(count, cluster).index(index)`` gives for an index the
    removal kept, and total where that would raise.
    """
    return index - sum(1 for slot in cluster if slot < index)


def _mirror_join(
    axis: Axis,
    previous: Structure,
    structure: Structure,
    change: Change,
) -> tuple[StructuralEdit, ...]:
    """The mirror of a curve join: three nodes to delete, two handles to move.

    The exact inverse of ``_mirror_curve_split``, and it has the same two parts
    for the same reason. Merging two cubics back into one does not just drop the
    node and its two handles: it RECOMPUTES the merged segment's two remaining
    handles, because each of them was describing half a curve. Delete the three
    on the mirrored side and stop there and the mirrored half keeps its
    pre-merge handles — which is not the mirror of what the designer now has, so
    the pair guard refuses those pairs on the very next frame and the axis goes
    permanently stale while writing nothing. That failure was measured on Task
    7's split (writes=0, stale=True, 34.8 units off); position sync does not
    catch up, because the guard is what stops it trying.

    Every index on the way in is a PREVIOUS-frame index — the user's delete has
    already happened, so the frame the binding describes is the one before it —
    and every index on the way out is a CURRENT-frame one, read under the
    sequential contract on ReconcileResult.

    Returns an empty tuple whenever the answer cannot be trusted. The caller
    then re-derives, which leaves the outline unmirrored; deleting the wrong
    node in a designer's outline is the worse outcome by a distance, so this
    refuses freely.
    """
    before, after = previous.paths[change.path], structure.paths[change.path]
    count = len(before)
    if len(after) < 4 or count != len(after) + SPLIT_SIZE:
        return ()
    cluster = split_cluster(change.index, count)
    outer = ((change.index - 2) % count, (change.index + 2) % count)
    # Five distinct slots: the block that went, plus a recomputed handle either
    # side of it. A contour too short for that cannot have been joined.
    if len({*cluster, *outer}) != 5:
        return ()

    # PAIRS only, never _image_of. An on-axis node is its own image, and reading
    # that as "delete the partner" would delete a node the user has already
    # taken out — or rather, whatever has since slid into its index.
    images: list[NodeRef] = []
    for slot in cluster:
        image = _partner_of(axis.binding, (change.path, slot))
        if image is None:
            return ()
        images.append(image)

    target_path = images[0][0]
    if any(image[0] != target_path for image in images):
        return ()  # the three mirror onto different contours, so there is no block
    if not 0 <= target_path < len(structure.paths):
        # The binding describes the PREVIOUS frame, which may hold a contour the
        # current one no longer does. _addressable only vouches for the contour
        # the user edited, not for the one their edit mirrors onto.
        return ()
    if len({image[1] for image in images}) != SPLIT_SIZE:
        return ()
    same_contour = target_path == change.path
    if same_contour and any(image[1] in cluster for image in images):
        # The merged segment maps onto ITSELF, which is what a segment crossing
        # the symmetry does. Its mirror is the very block that has already gone.
        return ()

    grown = len(structure.paths[target_path])
    deletes = sorted(
        (_after_removal(image[1], cluster) if same_contour else image[1]) for image in images
    )
    if any(not 0 <= index < grown for index in deletes):
        return ()

    edits = [
        # DESCENDING, so each later (smaller) index is still valid once the
        # earlier deletes have landed. That is the ReconcileResult contract:
        # ascending, the second delete removes whatever slid into the free slot.
        StructuralEdit(kind=DELETE_NODE, path=target_path, index=index)
        for index in reversed(deletes)
    ]

    for handle in outer:
        image = _image_of(axis.binding, (change.path, handle))
        if image is None or image[0] != target_path:
            return ()
        if same_contour and image[1] in {*cluster, *outer}:
            # Either the handle is its own image (it sits on the axis) or the
            # merged segment maps onto itself. Moving it would drag a node on
            # the half the designer draws.
            return ()
        moved = _after_removal(handle, cluster)
        index = _after_removal(image[1], cluster) if same_contour else image[1]
        if not 0 <= moved < len(after) or not 0 <= index < grown:
            return ()
        wanted = partner(after[moved], axis.centre, axis.mode, axis.shear)
        if close(structure.paths[target_path][index], wanted):
            continue
        edits.append(
            StructuralEdit(
                kind=MOVE_NODE,
                path=target_path,
                # Read after the deletes above, per the ReconcileResult contract.
                index=index - sum(1 for slot in deletes if slot < index),
                position=wanted,
            )
        )
    return tuple(edits)


def _mirror_deletes(
    axes: list[Axis], previous: Structure, change: Change
) -> tuple[StructuralEdit, ...]:
    """The nodes to delete to match those the designer removed: their partners
    under every axis, and those partners' in turn, so with both mirrors on a
    node takes its two images and the one diagonally opposite. In CURRENT
    indices, highest first, so no delete moves one still to come.

    Pairs only: an on-axis node is its own image, and reading that as "delete
    the partner" would delete whatever has since slid into the freed index. An
    axis that no longer describes even the PREVIOUS outline names nodes that
    moved long ago, and is left out.
    """
    removed = set(change.removed) or {(change.path, change.index)}
    gone = set(removed)
    live = [axis for axis in axes if not axis_is_stale(axis, previous.paths)]
    grew = True
    while grew:
        grew = False
        for axis in live:
            for ref in list(gone):
                opposite = _partner_of(axis.binding, ref)
                if opposite is not None and opposite not in gone:
                    gone.add(opposite)
                    grew = True
    return tuple(
        # The designer's deletes already happened: anything after one on the
        # same contour has moved down by one.
        StructuralEdit(
            kind=DELETE_NODE,
            path=path,
            index=index - sum(1 for at, node in removed if at == path and node < index),
        )
        for path, index in sorted(gone - removed, reverse=True)
    )


def _mirror_type_changed(
    axis: Axis,
    previous: Structure,
    structure: Structure,
    change: Change,
) -> StructuralEdit | None:
    """Carry a node's new CONNECTION across to its partner.

    The one edit that moves nothing. A designer toggling a corner from sharp to
    smooth expects the corner opposite to follow, and no amount of position
    comparison can see that edit — which is why the connection is encoded into
    the type string, where the signature gate does see it.

    Only the connection travels. If the bare TYPE changed as well — a line
    turned into a curve in place, say — this refuses outright, because a type
    names the segment ARRIVING at a node and a symmetry that reverses the walk
    re-points it onto the partner's OTHER neighbour. Working out which is a
    different problem from this one, and guessing it wrong hands the designer a
    contour whose curve and line have swapped places.

    ``_partner_of`` and never ``_image_of``: an on-axis node is its own image,
    so an image lookup would emit an edit that retypes the very node the user
    has just retyped — a no-op that would still stop the next axis from being
    asked.
    """
    was_type, _was_connection = decode_type(previous.types[change.path][change.index])
    now_type, now_connection = decode_type(change.node_type)
    if was_type != now_type:
        return None

    opposite = _partner_of(axis.binding, (change.path, change.index))
    if opposite is None:
        return None
    target_path, target_index = opposite
    if not 0 <= target_path < len(structure.types):
        return None
    if not 0 <= target_index < len(structure.types[target_path]):
        return None

    target_type, target_connection = decode_type(structure.types[target_path][target_index])
    if target_type == OFFCURVE:
        # A handle encodes bare and always decodes SHARP, so the equality below
        # can never fire for one and the edit built from it would re-encode the
        # string that is already there. That no-op still consumes the change —
        # reconcile breaks out of the axis loop on any edit at all, and the
        # caller repaints for it — so it is refused here, as this module refuses
        # anything it cannot usefully place.
        return None
    if target_connection == now_connection:
        return None  # already in step; nothing to place
    return StructuralEdit(
        kind=RETYPE_NODE,
        path=target_path,
        index=target_index,
        # The partner keeps its OWN type and takes only the connection. Under a
        # mirror the two need not name the same segment, and this edit has no
        # business deciding that they do.
        node_type=encode_type(target_type, now_connection),
    )


def _one_sided(points: list[Point], axis: Axis) -> bool:
    """True when every node of a contour sits on the same side of the symmetry.

    The same test ``pair_nodes`` sorts contours by, and the reason a twin is
    only ever built for a one-sided contour: ``pair_nodes`` pairs a CROSSING
    contour with itself and never with another one, so a twin of a crossing
    contour would arrive permanently unpaired — an outline that looks mirrored
    and mirrors nothing.
    """
    signs = {contour_side(point, axis.centre, axis.mode, axis.shear) > 0 for point in points}
    return len(signs) == 1


def _reversed_types(node_types: tuple[str, ...]) -> tuple[str, ...]:
    """A contour's node types as they read once the contour is WALKED BACKWARDS.

    A node type names the segment ARRIVING at that node, so reversing the walk
    re-points every one of them: the segment arriving at a node from the other
    direction is the one that used to LEAVE it, and that one is named by the
    next on-curve node along. Reversing the list as it stands instead leaves a
    curve node with no handles in front of it and hands the designer a contour
    Glyphs cannot draw.

    The CONNECTION does not re-point. It is not a property of either segment but
    of the node between them — GSNode.h: "the relationship between these two
    segments" — and the relationship "these two meet at the same angle" is
    unchanged by swapping which of them is the incoming one. So the connection
    travels with its own node while the type is replaced by the next on-curve
    node's, and the two halves of the encoded string have to be taken apart to
    do that. Move the whole string and every corner of a twinned contour takes
    its NEIGHBOUR's smoothness — silently, in an outline that otherwise looks
    right.

    Off-curve nodes stay off-curve — a handle is a handle whichever way the walk
    runs — and a contour with no on-curve node at all comes back unchanged
    rather than raising, since there is nothing to re-point.
    """
    count = len(node_types)
    decoded = [decode_type(encoded) for encoded in node_types]
    arriving: list[str] = []
    for index in range(count):
        node_type, connection = decoded[index]
        if node_type == OFFCURVE:
            arriving.append(OFFCURVE)
            continue
        step = 1
        while step < count and decoded[(index + step) % count][0] == OFFCURVE:
            step += 1
        arriving.append(encode_type(decoded[(index + step) % count][0], connection))
    return tuple(reversed(arriving))


def _pairs_contour(axis: Axis, paths: Paths, path_index: int) -> bool:
    """True when re-derivation ALONE pairs every node of one contour.

    The spec's preferred branch for a new contour, and what the plugin already
    did before this existed: a contour that is symmetric about the axis in its
    own right — or that lands opposite one the layer already had — is bound by
    the fresh pairing with nothing added to the outline. Only a contour this
    refuses needs a twin built for it.
    """
    binding = pair_nodes(paths, axis.centre, axis.mode, axis.shear, axis.sign, axis.split)
    return all(ref[0] != path_index for ref in binding.unpaired)


def _mirror_contour_added(
    axis: Axis,
    structure: Structure,
    change: Change,
) -> StructuralEdit | None:
    """The mirrored twin of a whole contour the designer just drew.

    Every node mirrored, and then — under a MIRROR — the walk reversed, because
    a reflection reverses contour winding and winding is what tells Glyphs fill
    from counter. A twin laid down in its source's own order is an inside-out
    contour that punches a hole where the ink should be. A half-turn is a
    rotation and preserves the walk, so it is read off ``reverses_traversal``
    rather than assumed.

    Returns None when the contour is too short to be a contour at all, when it
    crosses the symmetry, or when it is its own mirror. The caller then
    re-derives, which leaves the new contour unmirrored rather than stacking a
    second one on top of it.

    The length guard is the one that matters in practice: a path drawn with the
    Draw tool arrives as a ONE-node contour on the frame of its first click, and
    the tick runs on every frame. See MIN_TWIN_NODES — a partial path under
    construction must not be twinned at all.
    """
    points = structure.paths[change.path]
    node_types = structure.types[change.path]
    if len(points) < MIN_TWIN_NODES or len(node_types) != len(points):
        return None
    if not _one_sided(points, axis):
        return None

    mirrored = [partner(point, axis.centre, axis.mode, axis.shear) for point in points]
    if all(close(point, image) for point, image in zip(points, mirrored, strict=True)):
        # The contour lies ON the symmetry and is already its own twin.
        return None

    twin_types = tuple(node_types)
    if reverses_traversal(axis.mode):
        mirrored.reverse()
        twin_types = _reversed_types(twin_types)

    return StructuralEdit(
        kind=ADD_CONTOUR,
        # Appended, so it names the slot one past the end of the current list.
        path=len(structure.paths),
        index=0,
        points=tuple(mirrored),
        node_types=twin_types,
    )


def _mirror_contour_removed(
    axis: Axis,
    previous: Structure,
    structure: Structure,
    change: Change,
) -> StructuralEdit | None:
    """Which contour to remove to match one the designer deleted.

    The twin is the contour the removed one's nodes all mapped ONTO. PAIRS
    only, never ``_image_of``: an on-axis node is its own image, and a contour
    with on-axis nodes is one that crosses the symmetry and was paired with
    ITSELF — it had no twin, so there is nothing to take out.

    Returns None when the images scatter across more than one contour, when they
    land back on the removed contour itself, or when the twin's translated index
    is not one the current frame holds. The caller then re-derives, which leaves
    the orphaned twin in place; deleting a contour the designer still wants is
    the worse outcome by a distance.
    """
    count = len(previous.paths[change.path])
    if count == 0:
        return None

    targets: set[int] = set()
    for node_index in range(count):
        image = _partner_of(axis.binding, (change.path, node_index))
        if image is None:
            return None
        targets.add(image[0])
    if len(targets) != 1:
        return None
    target = targets.pop()
    if target == change.path:
        return None  # self-paired: the contour WAS its own twin

    # The user's removal already happened, so every contour after it has moved
    # down by one.
    index = target - 1 if target > change.path else target
    if not 0 <= index < len(structure.paths):
        return None
    return StructuralEdit(kind=REMOVE_CONTOUR, path=index, index=0)


def _addressable(previous: Structure, structure: Structure, change: Change) -> bool:
    """True when ``change`` names a node that both frames can actually be read for.

    classify never produces anything else, but a structural edit is applied to a
    designer's outline: an out-of-range index here would insert or delete in the
    wrong place rather than raise.

    The two contour kinds are checked against ONE frame each, not against both:
    a contour added is in the current frame only, a contour removed in the
    previous one only, and the min() below would refuse every one of them.
    """
    if change.kind == CONTOUR_ADDED:
        return 0 <= change.path < len(structure.paths)
    if change.kind == CONTOUR_REMOVED:
        return 0 <= change.path < len(previous.paths)
    if change.kind == DELETED_SEVERAL:
        return all(
            0 <= path < len(previous.paths) and 0 <= index < len(previous.paths[path])
            for path, index in change.removed
        )
    if not 0 <= change.path < min(len(previous.paths), len(structure.paths)):
        return False
    if change.kind in (INSERTED, CURVE_SPLIT, SEGMENT_CURVED):
        return 0 <= change.index < len(structure.paths[change.path])
    return 0 <= change.index < len(previous.paths[change.path])


# The changes that can be mirrored onto the generated half. Everything else
# falls through to bare re-derivation, which is safe and often right.
MIRRORED_KINDS = (
    INSERTED,
    DELETED,
    DELETED_SEVERAL,
    CURVE_SPLIT,
    CURVE_JOIN,
    CONTOUR_ADDED,
    CONTOUR_REMOVED,
    TYPE_CHANGED,
    SEGMENT_CURVED,
)


def _mirrored(
    axis: Axis,
    previous: Structure,
    structure: Structure,
    change: Change,
) -> tuple[StructuralEdit, ...]:
    """What ONE axis wants done about ``change``, or an empty tuple.

    Empty means two different things and the caller treats them the same: this
    axis refuses to place the change, or this axis needs nothing placed. A new
    contour that re-derivation already pairs is the second — the spec's
    preferred branch — and it reaches the next axis exactly as a refusal does.
    """
    if change.kind == CURVE_SPLIT:
        return _mirror_curve_split(axis, previous, structure, change)
    if change.kind == SEGMENT_CURVED:
        return _mirror_curved_segment(axis, previous, structure, change)
    if change.kind == CURVE_JOIN:
        return _mirror_join(axis, previous, structure, change)
    if change.kind == CONTOUR_ADDED:
        if _pairs_contour(axis, structure.paths, change.path):
            return ()
        edit = _mirror_contour_added(axis, structure, change)
    elif change.kind == CONTOUR_REMOVED:
        edit = _mirror_contour_removed(axis, previous, structure, change)
    elif change.kind == INSERTED:
        edit = _mirror_insert(axis, previous, structure, change)
    elif change.kind == TYPE_CHANGED:
        edit = _mirror_type_changed(axis, previous, structure, change)
    else:
        # An unrecognised kind is skipped, never guessed at — the same rule
        # _with_edits follows, for the same reason.
        return ()
    return () if edit is None else (edit,)


def _composed(first: Axis, second: Axis) -> Axis | None:
    """The half-turn two upright mirrors compose to, its pairing read through
    both, or None for any other two. A node's image in the fourth quarter is
    its partner's partner."""
    if {first.mode, second.mode} != {VERTICAL, HORIZONTAL} or first.shear or second.shear:
        return None
    vertical, horizontal = (first, second) if first.mode == VERTICAL else (second, first)
    refs = {
        (path, node) for pair in horizontal.binding.pairs for path, node in (pair[:2], pair[2:])
    }
    refs.update(horizontal.binding.onaxis)
    pairs: list[Pair] = []
    onaxis: list[NodeRef] = []
    for ref in sorted(refs):
        across = _image_of(horizontal.binding, ref)
        image = _image_of(vertical.binding, across) if across is not None else None
        if image == ref:
            onaxis.append(ref)
        elif image is not None and ref < image:
            pairs.append((*ref, *image))
    binding = Binding(
        pairs=tuple(pairs), onaxis=tuple(onaxis), counts=vertical.binding.counts, unpaired=()
    )
    centre = (vertical.centre[0], horizontal.centre[1])
    return Axis(binding=binding, centre=centre, mode=ROTATE, sign=vertical.sign)


def _merged(
    paths: Paths, groups: list[tuple[StructuralEdit, ...]]
) -> tuple[StructuralEdit, ...] | None:
    """Edit tuples each written against ``paths`` alone, as one tuple in the
    ReconcileResult order, or None where they collide: two put nodes into one
    gap, or set one node two ways.

    Each tuple is replayed on its own copy, existing nodes named by their index
    in ``paths`` and new ones by identity, so a new run is known by the
    existing node it follows. The runs then go in after their nodes, the moves
    and retypes are made, and the deletes, last and highest first. Whole
    contours come after: those removed, highest first, then those appended.
    """
    runs: dict[NodeRef, list[list]] = {}
    removed_contours: set[int] = set()
    added_contours: list[StructuralEdit] = []
    moves: dict[NodeRef, Point] = {}
    retypes: dict[NodeRef, str] = {}
    deletes: set[NodeRef] = set()
    for group in groups:
        lists: list[list] = [list(range(len(path))) for path in paths]
        for edit in group:
            if edit.kind == ADD_CONTOUR:
                added_contours.append(edit)
                continue
            if edit.kind == REMOVE_CONTOUR:
                removed_contours.add(edit.path)
                continue
            if edit.kind not in (INSERT_NODE, DELETE_NODE, MOVE_NODE, RETYPE_NODE):
                return None
            if not 0 <= edit.path < len(lists):
                return None
            nodes = lists[edit.path]
            if edit.kind == INSERT_NODE:
                if not 0 <= edit.index <= len(nodes):
                    return None
                nodes.insert(edit.index, [edit.position, edit.node_type])
                continue
            if not 0 <= edit.index < len(nodes):
                return None
            token = nodes[edit.index]
            if edit.kind == DELETE_NODE:
                del nodes[edit.index]
                if isinstance(token, int):
                    deletes.add((edit.path, token))
            elif edit.kind == MOVE_NODE and isinstance(token, list):
                token[0] = edit.position
            elif edit.kind == RETYPE_NODE and isinstance(token, list):
                token[1] = edit.node_type
            elif edit.kind == MOVE_NODE:
                if moves.setdefault((edit.path, token), edit.position) != edit.position:
                    return None
            elif retypes.setdefault((edit.path, token), edit.node_type) != edit.node_type:
                return None
        for path, nodes in enumerate(lists):
            after = -1
            run: list[list] = []
            for token in [*nodes, len(paths[path])]:
                if isinstance(token, list):
                    run.append(token)
                    continue
                if run:
                    if (path, after) in runs:
                        return None
                    runs[(path, after)] = run
                    run = []
                after = token

    final: list[list] = []
    for path, nodes in enumerate(paths):
        sequence: list = list(runs.get((path, -1), []))
        for index in range(len(nodes)):
            sequence.append(index)
            sequence.extend(runs.get((path, index), []))
        final.append(sequence)
    inserts, settings, removals = [], [], []
    for path, sequence in enumerate(final):
        for index, token in enumerate(sequence):
            if isinstance(token, list):
                inserts.append(
                    StructuralEdit(
                        kind=INSERT_NODE,
                        path=path,
                        index=index,
                        position=token[0],
                        node_type=token[1],
                    )
                )
                continue
            ref = (path, token)
            if ref in moves:
                settings.append(
                    StructuralEdit(kind=MOVE_NODE, path=path, index=index, position=moves[ref])
                )
            if ref in retypes:
                settings.append(
                    StructuralEdit(kind=RETYPE_NODE, path=path, index=index, node_type=retypes[ref])
                )
            if ref in deletes:
                removals.append(StructuralEdit(kind=DELETE_NODE, path=path, index=index))
    remaining = len(paths) - len(removed_contours)
    return (
        *inserts,
        *settings,
        *reversed(removals),
        *(
            StructuralEdit(kind=REMOVE_CONTOUR, path=path, index=-1)
            for path in sorted(removed_contours, reverse=True)
        ),
        *(replace(edit, path=remaining + k) for k, edit in enumerate(added_contours)),
    )


def _composed_edits(
    axes: list[Axis], previous: Structure, structure: Structure, change: Change
) -> tuple[StructuralEdit, ...] | None:
    """With both mirrors on, ``change`` mirrored across each and through the
    half-turn they compose to, so it reaches every quarter (Hugo, 2026-10-22:
    "Adding a node with both mirrors should also add them on other axis").
    None to leave it to the first axis that can place it: a node on an axis
    has fewer than three images, and one of the three refuses."""
    live = [axis for axis in axes if not axis_is_stale(axis, previous.paths)]
    if len(live) != 2:
        return None
    turn = _composed(*live)
    if turn is None:
        return None
    groups = [_mirrored(axis, previous, structure, change) for axis in (*live, turn)]
    if not all(groups):
        return None
    return _merged(structure.paths, groups)


def reconcile(
    structure: Structure,
    previous: Structure,
    axes: list[Axis],
    change: Change,
) -> ReconcileResult:
    """Bring every binding back in step with a structurally changed outline.

    Returns the structural edits the caller must apply to the layer, and the
    axes to keep — already paired against the outline those edits produce, so
    the caller applies them, re-reads the paths, and runs ``sync``.

    An insert, a delete, a curve split, a curve join, a contour drawn, a contour
    deleted or a node's connection toggled produces edits; every other change
    re-derives. That is not a shortcut — once the mirrored nodes are in place the
    outline is symmetric again, so a fresh pairing is exactly right, and it costs
    one O(n^2) pass on a structural edit rather than on every frame.

    A NEW contour that re-derivation already pairs — because it is symmetric in
    itself, or lands opposite one the layer already had — produces no edits at
    all. That is the spec's preferred branch: a twin built for it would stack a
    second contour on top of one that is already bound.

    The edits come back ORDERED, and stay that way: see ReconcileResult.
    """
    if change.kind == UNCHANGED:
        return ReconcileResult(axes=tuple(axes), edits=())

    edits: list[StructuralEdit] = []
    placeable = change.kind in MIRRORED_KINDS and _addressable(previous, structure, change)
    if placeable and change.kind in (DELETED, DELETED_SEVERAL):
        edits.extend(_mirror_deletes(axes, previous, change))
    elif placeable and (composed := _composed_edits(axes, previous, structure, change)):
        edits.extend(composed)
    elif placeable:
        # One change mirrored, by the first axis that can place it.
        for axis in axes:
            # Every index below is read out of this binding. If it no longer
            # describes even the PREVIOUS outline, it names nodes that moved
            # long ago, and acting on it would edit the wrong ones.
            if axis_is_stale(axis, previous.paths):
                continue
            mirrored = _mirrored(axis, previous, structure, change)
            if mirrored:
                edits.extend(mirrored)
                break

    return ReconcileResult(
        axes=_rederived(_with_edits(structure.paths, tuple(edits)), axes),
        edits=tuple(edits),
    )
