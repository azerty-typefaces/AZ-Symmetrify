"""Symmetry maths for AZ Symmetrify.

Pure functions over ``(x, y)`` tuples. Imports nothing from Glyphs so the whole
module is unit-testable headlessly.

THE MODEL: unshear by the master's italic angle, flip about a centre, reshear.
An italic is a sheared upright, so every symmetry an italic glyph has is an
upright symmetry wearing a shear. Undoing the shear first makes all three flips
plain coordinate negations and removes italics as a special case entirely.

Measured on a real 12-degree italic, mean per-node residual in font units,
comparing the naive upright flip against this model:

    glyph   vertical        horizontal      half-turn
    A        87.0 -> 6.7
    T        83.5 -> 1.3
    H       105.3 -> 2.0    105.5 -> 3.0     1.3 -> 1.4
    x        76.7 -> 6.1
    o                                        0.2 -> 0.2

The half-turn column barely moves because a half-turn is ``-I``, and
``S(-I)S⁻¹ = -I`` for any shear ``S`` — point symmetry is shear-invariant. The
mirrors are not, which is why an upright reflection misses by 80-100 units on an
italic and finds no partners at all.
"""

from __future__ import annotations

import math

Point = tuple[float, float]

# Distance in font units, used for "are these two points the same". Half a unit
# sits below anything a designer can place deliberately on a unit grid, and is
# zoom-independent.
TOL = 0.5

# The three symmetries a glyph can have. Named for what the designer sees, not
# for the maths: VERTICAL means "mirrored left-to-right about a vertical axis",
# which on an italic master is a line leaning at the italic angle.
VERTICAL = "vertical"
HORIZONTAL = "horizontal"
ROTATE = "rotate"
MODES = (VERTICAL, HORIZONTAL, ROTATE)

# The two MIRRORS, kept apart from the half-turn because the three do not
# combine freely. See ``excluded_by``.
MIRRORS = (VERTICAL, HORIZONTAL)

# A mirror switch bound as a HALF-TURN. Hugo, 2026-10-10, on a real 20-degree
# italic: "I don't understand why nodes are moved along the symmetry axis. The
# mirror horizontal should also mirror bottom on top, and top on bottom." A true
# italic round has its left extreme lower than its right one, so it is point-
# symmetric and no sheared mirror fits it: binding /O as one slid its extremes
# 56 units on average, up to 153. ``azsym_binding.fitted`` picks the turned
# form on an italic when it fits better. It is SPLIT, kept, shaded and stored
# like its mirror — the switch pressed is the one that lights — and MOVES like
# the half-turn. Never a store key: see ``switch_of``.
TURNED = {VERTICAL: "vertical-turn", HORIZONTAL: "horizontal-turn"}
# Inverted once: ``switch_of`` and ``moves_as`` run per node, per frame.
_MIRROR_OF = {turned: mirror for mirror, turned in TURNED.items()}


def switch_of(mode: str) -> str:
    """The switch, and store key, a mode belongs to: a turned mirror is its mirror's."""
    return _MIRROR_OF.get(mode, mode)


def moves_as(mode: str) -> str:
    """The transform a mode enforces: a turned mirror moves as the half-turn."""
    return ROTATE if mode in _MIRROR_OF else mode


def excluded_by(mode: str) -> tuple[str, ...]:
    """The symmetries that must come OFF when ``mode`` is switched on.

    Hugo, 2026-08-11: vertical OR/AND horizontal, THEN rotate. So the states a
    layer may be in are nothing, one mirror, BOTH mirrors, or the half-turn —
    never a mirror together with the half-turn, and never all three.

    That is the group, not a preference. A vertical and a horizontal mirror
    about the same centre COMPOSE to the half-turn, so {V, H} already contains
    it: a layer bound to both is bound to the half-turn whether or not the
    switch is lit. Adding rotate to either mirror reaches that same four-group
    by naming two of its three members instead of the other two — the same
    state, described differently, and one the survey has no row for (``COMBOS``
    offers only V + H). Two ways of spelling one state is how a switch row
    stops meaning anything.

    Returns modes to RELEASE, so an unknown name excludes nothing rather than
    raising: this is reached from a menu click, and a typo must not cost the
    designer their bindings.
    """
    mode = switch_of(mode)
    if mode == ROTATE:
        return MIRRORS
    if mode in MIRRORS:
        return (ROTATE,)
    return ()


def shear_factor(italic_angle: float) -> float:
    """Horizontal offset per unit of height, from a master's italic angle.

    Zero for an upright master, which collapses every function below to its
    plain upright form. There is no separate upright code path.
    """
    return math.tan(math.radians(italic_angle))


def unshear(point: Point, shear: float) -> Point:
    """Into the upright frame the italic was sheared from."""
    return (point[0] - shear * point[1], point[1])


def reshear(point: Point, shear: float) -> Point:
    """Back out of the upright frame. Inverse of ``unshear``."""
    return (point[0] + shear * point[1], point[1])


def centre_of(points: list[Point], shear: float) -> Point:
    """The symmetry centre for a set of nodes, IN THE UNSHEARED FRAME.

    The midpoint of the node bounding box, which is the one point every flip
    below can be built around: mirroring about it preserves the bounding box, so
    binding never slides the glyph sideways.

    Deliberately NOT the advance width. Measured on a real italic ``A``, half the
    width was 309.5 while the true symmetry axis sat at 249 — binding to the
    width would have shoved the glyph 60 units. Across ``A H M T V`` a width/2
    axis scored 87-463 units off; this one scores 1.3-8.1.
    """
    if not points:
        return (0.0, 0.0)
    upright = [unshear(point, shear) for point in points]
    xs = [point[0] for point in upright]
    ys = [point[1] for point in upright]
    return ((min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0)


def guide_stands(angle: float) -> bool:
    """Whether a guide at ``angle`` (degrees, 0 along +x) stands: within 45° of
    upright, ends included, so 45° and 135° stand and no angle is both or
    neither. Read off the angle the designer sees, never off an unsheared
    vector: on an italic the two disagree near 45° (final review, 2026-10-15),
    and the mirrors' fit, the half-turn's split and its label must agree."""
    return abs(angle % 180.0 - 90.0) <= 45.0


def guide_centre(
    position: Point, angle: float, mode: str, shear: float, box: Point
) -> Point | None:
    """The centre a selected guide gives ``mode``, IN THE UNSHEARED FRAME.

    Hugo, 2026-10-15, on a tildecomb no automatic axis can bind: "users could
    use a GSGuide, and then Symmetrify use this custom axis". A guide says WHERE
    the symmetry is, never which way it runs. The half-turn turns about the
    guide's position point. A mirror takes that point's x (across a vertical
    line, so the guide has to stand) or its y (across a horizontal one, so it
    has to lie flat), and keeps ``box`` — ``centre_of`` the outline — for the
    coordinate it does not use.

    ``angle`` is in degrees, 0 along +x, as ``GSGuide.angle`` is. A guide
    stands within 45 degrees of upright and lies flat otherwise, so no angle is
    both or neither. None when the guide runs the wrong way for the mirror. A
    turned mirror (``TURNED``) reads its own mirror's rule and turns about the
    centre this returns.
    """
    upright = unshear(position, shear)
    mode = switch_of(mode)
    if mode == ROTATE:
        return upright
    standing = guide_stands(angle)
    if mode == VERTICAL:
        return (upright[0], box[1]) if standing else None
    return (box[0], upright[1]) if not standing else None


def axis_split(angle: float, shear: float) -> Point:
    """The split a half-turn's axis angle gives: the ``extent`` whose "\\"
    diagonal is the guide's line (``_diagonal``), IN THE UNSHEARED FRAME.

    Decision 7 of the guide-axis spec. Hugo, 2026-10-15, on a point-symmetric S
    with a guide through it at about 60°: "I would like to replicate left side".
    The box diagonal put the S's top-left lobe on the generated side. About a
    guide the half-turn splits on the guide's own line instead. Its positive
    side is the right of a standing guide and the top of a flat one (the 45°
    rule ``guide_centre`` uses), so the default sign, -1, keeps the left or the
    bottom: the sides the mirrors keep.

    Every reader of the split takes its DIRECTION from ``_diagonal(extent)``,
    never its size, so this unit vector stands in for the box wherever a
    half-turn is split: ``side``, ``half_plane``, ``axis_direction``, and the
    native pairing.

    Renamed from ``guide_split`` by decision 8: the angle is typed in the
    palette's Diagonal field now, and no guide feeds it.
    """
    radians = math.radians(angle)
    run, rise = unshear((math.cos(radians), math.sin(radians)), shear)
    normal = (rise, -run)  # the right-hand side, walking along the guide
    standing = guide_stands(angle)
    if (normal[0] if standing else normal[1]) < 0.0:
        normal = (-normal[0], -normal[1])
    length = math.hypot(normal[0], normal[1])
    # _diagonal maps an extent (w, h) to the normal (h, w); swapped back here.
    return (normal[1] / length, normal[0] / length)


def split_angle(split: Point, shear: float) -> float:
    """The on-screen angle, in degrees in [0, 180), of the line a half-turn's
    stored ``split`` runs along: the inverse of ``axis_split``.

    Decision 8 of the guide-axis spec: the palette's Diagonal field shows a
    bound half-turn's angle, and the binding keeps only its split. The line
    runs along (-x, y) of the split in the unsheared frame (``axis_split``
    swapped the normal's coordinates), so it is resheared back to the angle
    the designer typed.
    """
    run, rise = reshear((-split[0], split[1]), shear)
    degrees = math.degrees(math.atan2(rise, run)) % 180.0
    # A hair below zero comes back from % as 180.0 exactly.
    return degrees if degrees < 180.0 else 0.0


def extent_of(points: list[Point], shear: float) -> Point:
    """Width and height of the node bounding box, IN THE UNSHEARED FRAME — the
    box ``centre_of`` is the middle of. The half-turn splits corner to corner
    across it (see ``side``)."""
    if not points:
        return (1.0, 1.0)
    upright = [unshear(point, shear) for point in points]
    xs = [point[0] for point in upright]
    ys = [point[1] for point in upright]
    return (max(xs) - min(xs), max(ys) - min(ys))


def _diagonal(extent: Point) -> tuple[Point, Point]:
    """The unit direction from a box's top-left corner to its bottom-right, and
    the unit normal into its top-right half. A box with no size is a square."""
    width, height = extent
    length = math.hypot(width, height)
    if length < 1e-9:
        width, height, length = 1.0, 1.0, math.sqrt(2.0)
    return (width / length, -height / length), (height / length, width / length)


def flip(point: Point, centre: Point, mode: str) -> Point:
    """Negate about ``centre`` in the UNSHEARED frame. An involution."""
    mode = moves_as(mode)
    x, y = point
    if mode == VERTICAL:
        return (2.0 * centre[0] - x, y)
    if mode == HORIZONTAL:
        return (x, 2.0 * centre[1] - y)
    return (2.0 * centre[0] - x, 2.0 * centre[1] - y)


def partner(point: Point, centre: Point, mode: str, shear: float) -> Point:
    """Where ``point``'s symmetric counterpart belongs.

    unshear -> flip -> reshear. Its own inverse, because ``flip`` is.
    """
    return reshear(flip(unshear(point, shear), centre, mode), shear)


def side(
    point: Point, centre: Point, mode: str, shear: float, extent: Point = (1.0, 1.0)
) -> float:
    """Signed half-plane: positive one side of the symmetry, negative the other.

    Which coordinate splits depends on the mode. A half-turn has no mirror
    line — only the centre itself is fixed — so ANY line through the centre
    splits it into the half you draw and the half it generates: a partner's
    offset from the centre is negated whole, so it always lands on the other
    side of every such line.

    The half-turn splits on the "\\" DIAGONAL (Hugo, 2026-10-10: "it should be
    also previewed with diagonal line"), which is what his tile has always
    drawn: bottom-left kept, top-right generated. It split on x before, exactly
    like the vertical mirror, so the two previews were indistinguishable but for
    the outline. Positive is the top-right half. A node ON the diagonal falls
    back to its x-offset, which its partner's negates just the same.

    The diagonal runs corner to corner across the layer's box (Hugo,
    2026-10-10: "diagonal axis should go from topleft to bottomright of the
    bbox of the layer"): ``extent`` is that box's size, from ``extent_of``, and
    the centre is its middle, so the line through it meets both corners. With
    no extent it is the 45-degree line it used to be.

    A turned mirror splits like its mirror (see TURNED).
    """
    mode = switch_of(mode)
    x, y = unshear(point, shear)
    if mode == HORIZONTAL:
        return y - centre[1]
    if mode == VERTICAL:
        return x - centre[0]
    _along, normal = _diagonal(extent)
    across = normal[0] * (x - centre[0]) + normal[1] * (y - centre[1])
    return across if abs(across) > TOL else x - centre[0]


def contour_side(point: Point, centre: Point, mode: str, shear: float) -> float:
    """``side``, as asked when sorting CONTOURS: crossing ones pair with themselves.

    The same as ``side`` for the mirrors, whose axis a self-symmetric contour
    has to cross. Not for the half-turn: the stems of a real 20-degree italic
    ``H`` each cut the "\\" diagonal, so each was paired onto ITSELF and the
    bind threw a stem's far end onto the other stem, 220-400 units. Asked on x
    — the split the half-turn used before the diagonal — a stem lies wholly on
    one side and pairs with its twin. The diagonal still decides which node of
    each pair is kept; only the sorting asks x, falling back to y for a node on
    the centre's vertical, whose partner's y-offset is the one negated.

    ponytail: x is a heuristic too; two bars stacked one above the other
    (``=``) both cross it and self-pair. Ask each contour's own centre against
    the glyph's if a glyph like that ever needs a half-turn.
    """
    if moves_as(mode) != ROTATE:
        return side(point, centre, mode, shear)
    x, y = unshear(point, shear)
    offset = x - centre[0]
    return offset if abs(offset) > TOL else y - centre[1]


def contour_sides(points: list[Point], centre: Point, mode: str, shear: float) -> list[float]:
    """``contour_side`` of every point, with the mode resolved once.

    The same arithmetic in the same order, so the same floats. Pairing asks it
    of every node of every contour, and per point the mode lookups were most of
    the cost.
    """
    cx, cy = centre
    if moves_as(mode) == ROTATE:
        sides = []
        for point in points:
            offset = point[0] - shear * point[1] - cx
            sides.append(offset if abs(offset) > TOL else point[1] - cy)
        return sides
    if mode == VERTICAL:
        return [point[0] - shear * point[1] - cx for point in points]
    if mode == HORIZONTAL:
        return [point[1] - cy for point in points]
    return [contour_side(point, centre, mode, shear) for point in points]


def clamp(point: Point, centre: Point, mode: str, shear: float) -> Point:
    """Put an on-axis point exactly on the axis. The fixed points of ``flip``."""
    mode = moves_as(mode)
    x, y = unshear(point, shear)
    if mode == VERTICAL:
        x = centre[0]
    elif mode == HORIZONTAL:
        y = centre[1]
    else:
        x, y = centre
    return reshear((x, y), shear)


def reverses_traversal(mode: str) -> bool:
    """True when the flip reverses the order a contour is walked in.

    Both mirrors are reflections and reverse it; a half-turn is a rotation and
    preserves it. This is what lets partners be found structurally, by index,
    on a shape that is not symmetric yet.
    """
    return moves_as(mode) != ROTATE


def close(a: Point, b: Point, tol: float = TOL) -> bool:
    """True when two points are within ``tol`` font units of each other."""
    return math.hypot(a[0] - b[0], a[1] - b[1]) <= tol


def side_labels(mode: str, standing: bool | None = None) -> tuple[str, str]:
    """Labels for the positive and negative sides, in that order.

    A half-turn split on a guide (``axis_split``, decision 7) names its halves
    by the guide: its right and left when it stands (``guide_stands``), its top
    and bottom when it lies flat. ``standing`` is None without a guide.
    """
    mode = switch_of(mode)
    if mode == HORIZONTAL:
        return ("Top", "Bottom")
    if mode == ROTATE:
        if standing is None:
            return ("Top Right", "Bottom Left")
        return ("Right", "Left") if standing else ("Top", "Bottom")
    return ("Right", "Left")


def axis_origin(centre: Point, shear: float) -> Point:
    """The centre, back in the layer's own (sheared) coordinates."""
    return reshear(centre, shear)


def half_plane(
    centre: Point,
    mode: str,
    shear: float,
    sign: int,
    reach: float = 4000.0,
    extent: Point = (1.0, 1.0),
) -> list[Point]:
    """Corners of the half of the plane where ``side`` carries ``sign``.

    Used to clip a fill to the half of the glyph a binding DRIVES, so the
    generated side can be shaded without shading the drawn one.

    A half-turn has no axis line, but it still splits the plane: ``side``
    divides it on the "\\" diagonal, so its half is the triangle-ish wedge on
    one side of that line, built in the unsheared frame and put back. Returned
    in layer (sheared) coordinates, ready to stroke or clip with, and always
    wound counterclockwise — see _counterclockwise.
    """
    mode = switch_of(mode)
    if mode == ROTATE:
        # Far along the diagonal both ways, then pushed across it toward the
        # side ``sign`` names. A shear is linear, so the half-plane it maps to
        # is still a half-plane and four resheared corners describe it.
        along, normal = _diagonal(extent)
        upper_left = (centre[0] - reach * along[0], centre[1] - reach * along[1])
        lower_right = (centre[0] + reach * along[0], centre[1] + reach * along[1])
        push = (sign * reach * 2.0 * normal[0], sign * reach * 2.0 * normal[1])
        corners = [
            upper_left,
            lower_right,
            (lower_right[0] + push[0], lower_right[1] + push[1]),
            (upper_left[0] + push[0], upper_left[1] + push[1]),
        ]
        return _counterclockwise([reshear(corner, shear) for corner in corners])
    if mode == HORIZONTAL:
        near = centre[1]
        far = centre[1] + sign * reach
        return _counterclockwise([(-reach, near), (reach, near), (reach, far), (-reach, far)])
    low = reshear((centre[0], -reach), shear)
    high = reshear((centre[0], reach), shear)
    offset = sign * reach * 2.0
    return _counterclockwise([low, high, (high[0] + offset, high[1]), (low[0] + offset, low[1])])


def _clipped_to_ink(points: list[Point], origin: Point, direction: Point) -> tuple[Point, Point]:
    """The stretch of a line through ``origin`` that lies inside the ink's box.

    The diagonal cannot span the height the way an upright axis does: at one
    unit across per unit up, a tall narrow glyph — an ``l`` — would send it far
    out past both sides. Clipped to the box instead, it ends where the ink ends
    in whichever direction runs out first.
    """
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    low, high = -math.inf, math.inf
    for start, step, least, most in (
        (origin[0], direction[0], min(xs), max(xs)),
        (origin[1], direction[1], min(ys), max(ys)),
    ):
        if abs(step) < 1e-12:
            continue
        a, b = (least - start) / step, (most - start) / step
        low, high = max(low, min(a, b)), min(high, max(a, b))
    if not math.isfinite(low) or not math.isfinite(high) or low > high:
        return origin, origin
    return (
        (origin[0] + direction[0] * low, origin[1] + direction[1] * low),
        (origin[0] + direction[0] * high, origin[1] + direction[1] * high),
    )


def _counterclockwise(corners: list[Point]) -> list[Point]:
    """The same corners, wound in positive-area order.

    Written for the case where several symmetries are bound at once and their
    half-planes are clipped together as ONE path. Under the non-zero winding
    rule an overlap only survives if every contour covering it winds the same
    way; mix the two windings and the region where two symmetries agree cancels
    to zero and is punched out as a hole.

    The naive corner order flips winding with ``sign``, so a vertical mirror
    generating right and a horizontal one generating up produce opposite
    windings, and the shared quadrant — which is precisely the part BOTH of them
    generate — would be the one left unshaded.
    """
    area = 0.0
    for index, (x, y) in enumerate(corners):
        next_x, next_y = corners[(index + 1) % len(corners)]
        area += x * next_y - next_x * y
    return corners if area >= 0.0 else corners[::-1]


def axis_direction(mode: str, shear: float, extent: Point = (1.0, 1.0)) -> Point:
    """Direction of the drawn line for this symmetry.

    A vertical line in the unsheared frame leans by the shear once put back;
    a horizontal one is unaffected, because a shear only moves x.

    A half-turn has no mirror axis — it has a centre — but it still SPLITS the
    glyph into the half you draw and the half it generates, on the "\\"
    diagonal ``side`` uses. That is the boundary half_plane() shades along, so
    the drawn line and the shaded region agree by construction. Read it as the
    edge of the generated half, not as a line the shape reflects across: nothing
    reflects across it under a half-turn.

    Per unit of height, like the vertical axis: the diagonal runs one unit left
    for every unit up in the unsheared frame, and the shear adds its lean.
    """
    mode = switch_of(mode)
    if mode == HORIZONTAL:
        return (1.0, 0.0)
    if mode == ROTATE:
        along, _normal = _diagonal(extent)
        rise = -along[1]
        if abs(rise) < 1e-12:
            return (1.0, 0.0)  # a box with no height: its diagonal is its width
        # A guide split (axis_split) may rise to the LEFT, which no box diagonal
        # does; per unit of height the line is the same either way round.
        return (shear - along[0] / rise, 1.0)
    return (shear, 1.0)


def axis_span(
    points: list[Point],
    centre: Point,
    mode: str,
    shear: float,
    split: Point | None = None,
) -> tuple[Point, Point]:
    """The two ends of the drawn axis line, held to the glyph's own extent.

    An unbounded line runs clean across the neighbouring glyphs in the Edit
    view, where it reads as page furniture rather than as something belonging
    to THIS glyph. Both ends come from the layer's own points instead: a
    horizontal axis spans the width of the outline, a vertical or half-turn
    axis spans its height.

    Measured in LAYER space, not the unsheared frame, because the extent wanted
    is the ink's. A sheared axis still spans exactly the height — a shear leaves
    y alone — and its ends lean with it, so it stays inside the glyph rather
    than poking out of the top and bottom corners.

    With no points there is nothing to bound, so both ends collapse onto the
    origin and the line draws as nothing. That is the honest answer: an empty
    layer has no extent to span.

    ``split`` is a half-turn's guide split (``axis_split``) when it was bound
    about one; the line is then drawn along the guide instead of the box
    diagonal.
    """
    origin = axis_origin(centre, shear)
    if not points:
        return origin, origin
    mode = switch_of(mode)
    dx, dy = axis_direction(mode, shear, split if split is not None else extent_of(points, shear))
    if dy == 0.0:
        xs = [x for x, _ in points]
        return (min(xs), origin[1]), (max(xs), origin[1])
    if mode == ROTATE:
        return _clipped_to_ink(points, origin, (dx, dy))
    ys = [y for _, y in points]
    return (
        (origin[0] + dx * (min(ys) - origin[1]), min(ys)),
        (origin[0] + dx * (max(ys) - origin[1]), max(ys)),
    )
