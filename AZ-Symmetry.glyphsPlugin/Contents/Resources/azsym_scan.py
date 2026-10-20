"""Which layers could take a symmetry, and which could not.

Binding is a FORCING operation — it overwrites one half of the outline from the
other, whether or not the shape was symmetric to begin with. On a glyph that
already has the symmetry that snap moves almost nothing; on one that does not it
throws the outline hundreds of units. This module measures that snap WITHOUT
performing it, so a whole file can be surveyed before anything is touched.

Pure: imports nothing from Glyphs, so the whole thing is unit-testable
headlessly. ``plugin.py`` walks the font and hands each layer's paths in.

THE MEASURE: the furthest a single node would be shoved by the bind snap, as a
share of the outline's own diagonal. Furthest rather than mean, because a mean
over a hundred nodes hides the one corner that flies off; and a share rather
than raw units, because the same 25 units is a shrug on a capital and a
catastrophe on a period. The diagonal is the shape's OWN extent, never the
advance width — the same measurement the axis itself comes from.

WHY THE PAIRING TEST COMES FIRST. Displacement alone is not evidence. A layer
where nothing pairs writes nothing, so it scores a perfect zero while having
none of the symmetry at all. MEASURED on real italic outlines
(in the test suite): ``n`` under a horizontal mirror and ``T`` under a
horizontal mirror both leave EVERY node unpaired and both score 0.0 units of
displacement — better than ``H``, which genuinely has the symmetry and scores
2.5. Rank on displacement alone and the three worst candidates in the font come
out on top. So a mode is only ever measured once every node has a partner.

CALIBRATION, on the same real outlines, furthest node movement as a share of
the outline diagonal:

    genuinely symmetric         wrong symmetry, but fully paired
    o rotate      0.26%         o vertical      7.59%
    H vertical    0.26%         o horizontal    7.59%
    H rotate      0.41%         T rotate       69.26%
    H horizontal  0.43%         A rotate       76.06%
    T vertical    0.55%
    A vertical    2.62%

Those two clusters are separated by the gap from 2.62% to 7.59%, and that gap is
the boundary between HAVING a symmetry and not having it. It is not the same
question as whether binding is SAFE, and the module now draws both lines:

The scan now measures how far the OUTLINE moves, not the furthest node
(issue #3): never more, and less wherever a handle moved, since a curve moves by
at most 3/4 of its handle. The lines below were kept, re-checked on a real
test font (2026-10-20): at the same 5% the window lists 18 more layers, 11 of
them real symmetries and 7 loose italic mirrors, and 48 rows (dots, rings,
zeros) rise to Safe.

``SAFE_SHARE`` (0.5%) is the safe line — the snap moves nothing the designer
would see, so binding is a tidy-up rather than an edit. ``NEAR_SHARE`` (5%) is
the measured have-it line above. Between them sits a glyph that genuinely has
the symmetry but was drawn loose: on these outlines that is ``A`` vertical at
2.62% and ``T`` vertical at 0.55%, both real symmetries that binding would
visibly tighten. Those are reported, with their numbers, as a judgement for the
designer rather than a verdict from the plugin.

``safe_units(upm)`` caps the same thing in absolute font units, and both
conditions must hold. A share alone is blind to scale in one direction: 0.5% of
a 3000-unit ornament is 15 units, which is a visible shove no matter how large
the glyph is.

That cap is now a share of the font's EM (``SAFE_UNITS_PER_UPM``, read through
``safe_units``) rather than a fixed unit count, so a 2048-upm font is judged on
its own scale — the same move ``azsym_sync.guard_tol`` makes. At the 0.05 this
ships with, the cap is 50 units on a 1000-upm font and only bites above a
10000-unit diagonal, so on every ordinary glyph ``SAFE_SHARE`` is the binding
constraint and the cap is a backstop against outsized artwork rather than a
second opinion on letters.

``SAFE_SHARE`` and ``NEAR_SHARE`` are calibrated on five glyphs from one
12-degree italic. Everything else in this module is a count, not a judgement.

Two ways to make an outline symmetric live here, and they are not variants of
one thing. ``proposal`` FORCES: one half is the source and the other is
overwritten from it, which is what binding does and needs a side to keep.
``settlement`` MEETS: both halves move half-way and neither is discarded, which
is what Symmetrise does and has no side at all.

The measuring rules run in the native engine (native/scan.cpp); the docstrings
below are their specification.
"""

from __future__ import annotations

from dataclasses import dataclass

import azsym_binding
import azsym_native
from azsym_binding import Paths
from azsym_geometry import MODES, Point, centre_of

# Per point, whether it is an off-curve handle. Given, the scan measures how far
# the drawn outline moves; left out, how far the furthest point moves.
OffCurve = list[list[bool]]

# Symmetries the SURVEY proposes as a pair rather than one at a time.
#
# EMPTY (Hugo, 2026-09-10: "In Analyze, don't propose Both symmetrify"). The
# scan now offers one symmetry per row and never a combination.
#
# Composing two mirrors is still entirely supported — it is what the contextual
# menu's "Both Mirrors" switch binds, and what the Browse pane's "Both" filter
# lists. What stopped is the survey VOLUNTEERING it: binding both is a claim
# about the drawing that the designer makes, not one a heuristic should put in
# front of them for a whole file at once.
#
# Restoring it is ``COMBOS = ((VERTICAL, HORIZONTAL),)`` plus putting those two
# names back on the azsym_geometry import above, which ruff removed once nothing
# referenced them.
#
# The pair machinery below is deliberately left in place rather than torn out —
# ``measure_combo`` still measures a pair correctly, and restoring the proposal
# is putting the tuple back. There is no live caller for it while this is empty.
#
# Only V+H was ever a candidate: a vertical and a horizontal mirror about the
# same centre already compose to the half-turn, so V+H is the whole four-group,
# and V+R or H+R would be the same state reached by naming two of its three
# members instead of the other two.
COMBOS: tuple[tuple[str, ...], ...] = ()

# How a combination is named in a row. Spaced for reading, since it is what the
# designer sees in the Symmetry column.
COMBO_JOIN = " + "


def combo_name(modes: tuple[str, ...]) -> str:
    return COMBO_JOIN.join(modes)


def combo_modes(name: str) -> tuple[str, ...]:
    """The individual symmetries a row's name refers to. One for a plain mode."""
    return tuple(name.split(COMBO_JOIN))


def symmetry_rank(name: str) -> tuple[int, int]:
    """Where a symmetry sits in the designer's order of preference.

    Combinations first, then ``MODES`` order — so "vertical + horizontal" beats
    "vertical", which beats "horizontal", which beats "rotate". Hugo's ranking,
    and it is about what is worth BINDING rather than about what measured well:
    the pair is the strongest claim a shape can make, and a half-turn is the
    weakest thing a Latin glyph is likely to genuinely have, so a rotate row
    that happens to measure a hair tighter than a vertical one should still not
    outrank it.

    Derived from ``MODES`` rather than written out, so adding a mode or a
    combination cannot leave a row with no rank — an unknown name sorts last
    instead of raising, because a survey window is the wrong place to discover
    a typo.
    """
    modes = combo_modes(name)
    first = modes[0] if modes else ""
    return (-len(modes), MODES.index(first) if first in MODES else len(MODES))


def bound_name(bound: tuple[str, ...]) -> str:
    """The one symmetry a layer's already-bound modes amount to.

    Named in ``MODES`` order so it reads as the same string ``COMBOS`` would
    give the same set — which is what lets it be RANKED against a candidate.

    Empty for a layer with nothing bound, and callers guard on that rather
    than rank it. Ranking the empty string happens to be harmless today —
    ``symmetry_rank("")`` treats it as one unknown mode, which sorts LAST, so
    it suppresses nothing — but that is an accident of how an unknown name is
    handled and not a decision anyone made. The failure it would become is
    silent and total: every candidate on every unbound layer banded away, and
    a survey window that lists nothing at all.
    """
    return combo_name(tuple(mode for mode in MODES if mode in bound))


# Share of the outline's own diagonal a bind snap may move the outline and still
# count as safe. Half a percent: on a 1000-unit glyph that is five units, which
# is the point below which a designer would not see the snap happen.
# The rule lives in native/kernel.h; change both.
SAFE_SHARE = 0.005

# Units per em to judge against when a font cannot be asked for its own. Glyphs'
# own default, and the value every number in this module was calibrated at.
DEFAULT_UPM = 1000.0

# The same limit in absolute font units, and BOTH have to hold. A share alone
# scales with the glyph, so on a very large outline half a percent is still a
# visible shove; this is what stops that.
#
# Expressed as a share of the EM rather than as a raw unit count (Hugo,
# 2026-08-11), so a 2048-upm font is judged on its own scale instead of on a
# 1000-upm one — see ``safe_units``. The same move ``azsym_sync.guard_tol``
# makes for its own tolerance.
# The rule lives in native/kernel.h; change both.
SAFE_UNITS_PER_UPM = 0.05


def safe_units(upm: float = DEFAULT_UPM) -> float:
    """The absolute cap, in the units of a font of this em size.

    A non-positive or unreadable ``upm`` falls back to ``DEFAULT_UPM`` rather
    than yielding a cap of zero: a font that cannot say how big its em is should
    be judged by the ordinary rule, not have every candidate refused.
    """
    return azsym_native.safe_units(upm)


# Where a symmetry stops being one at all, as opposed to being one drawn loosely.
# This is the MEASURED boundary: on this repo's real outlines the symmetries the
# glyphs genuinely have top out at 2.62% and the fully-paired wrong ones start at
# 7.59%. Between SAFE_SHARE and here is "you have this symmetry, and binding
# would tighten it" — which is a judgement for the designer, so it is reported
# with its number rather than decided.
NEAR_SHARE = 0.05

# Furthest a proposal may move the outline and still be worth LISTING at all (Hugo,
# 2026-08-11). Above it the row is dropped from the window: a snap that shoves a
# node this far is not a tidy-up whatever percentage it works out at, and rows
# nobody would press Apply on are what buries the ones they would.
#
# A share of the em, like every other absolute limit here, so a 2048-upm file is
# not judged on a 1000-upm scale. 0.04 is forty font units at 1000 upm, which is
# the number this was asked for in.
#
# It is a CEILING ON THE ROW, not a band. NEAR_SHARE is relative to the glyph and
# answers "does this shape have this symmetry"; this is absolute and answers "is
# the answer worth reading". They catch different things: 5% of a big display
# glyph can be 100 units, and 40 units is a visible shove however large the
# drawing.
MOVE_CEILING_PER_UPM = 0.04


def move_ceiling(upm: float = DEFAULT_UPM) -> float:
    """The listing ceiling, in the units of a font of this em size.

    Falls back to ``DEFAULT_UPM`` for the reason ``safe_units`` does — a ceiling
    of zero would empty the window rather than tidy it.
    """
    return MOVE_CEILING_PER_UPM * (upm if upm and upm > 0 else DEFAULT_UPM)


# How much further a combination may move an outline than one of its own flips
# would before it stops being worth proposing — see ``_outmoved_by_a_single``.
#
# Half the precision the Moves column shows, which is two decimals. A pair that
# is worse by less than this rounds to the SAME printed number as the flip it
# lost to, so refusing it there would be a row changing its mind over a
# difference the designer cannot see in the column they are reading.
#
# Not a judgement about how much movement is acceptable — SAFE_SHARE and
# NEAR_SHARE do that, and they still band the row afterwards. This only decides
# which of two measurements counts as the same measurement.
COMBO_SLACK = 0.005

# Node ceiling per layer. Pairing searches every offset against every node, so
# it is quadratic, and a survey is a few thousand layers in a row with the
# application frozen behind a sheet — the tail decides how long the designer
# waits, not the average.
#
# TWO sets of measurements live here, and they disagree by a factor of five for
# a reason worth keeping. The original ones were taken on outlines that do not
# pair — 6.7 ms at 100 nodes, 27 ms at 200, 105 ms at 400, 165 ms at 500 — and
# an outline that pairs nothing is the CHEAP case: ``measure`` returns on the
# unpaired branch without taking the second source side, and ``scan_layer``
# skips the pair simulation outright. Re-measured on a fully symmetric single
# contour, where every mode pairs and nothing is skipped: 93 ms at 100 nodes,
# 167 ms at 120, 300 ms at 140 and 2.68 s at 500. A ceiling has to be set
# against the second set.
#
# 120 keeps the worst single layer near a sixth of a second. It is a TOTAL node
# count and the cost is per contour, so it is conservative for a glyph drawn as
# many small contours — which is the safe direction: the cost of refusing one is
# a row missing from a list, against seconds of a frozen application.
#
# A typical Latin glyph is nowhere near it. Measured on this repo's own
# fixtures: ``T`` 8 nodes, ``A`` 11, ``H`` 12, ``n`` 21, ``o`` 24.
#
# A layer that hits the ceiling is REPORTED as crowded rather than dropped
# quietly — a scan that silently skipped the hardest glyphs in the file would
# read as having cleared them — and the contextual menu still binds it by hand.
NODE_CEILING = 120

# What the scan concluded about one mode on one layer.
SAFE = "safe"  # every node paired, and the snap barely moves the shape
RESHAPES = "reshapes"  # every node paired, but the snap would visibly move it
UNPAIRED = "unpaired"  # a node had no partner: this mode does not describe it
VERDICTS = (SAFE, RESHAPES, UNPAIRED)


@dataclass(frozen=True)
class Candidate:
    """What one symmetry would do to one layer, measured but not performed."""

    mode: str
    verdict: str
    shift: float
    share: float
    unpaired: int
    nodes: int

    @property
    def safe(self) -> bool:
        return self.verdict == SAFE


# Why a layer was not measured at all. Distinct from a Candidate's verdict:
# these are properties of the layer, not of any one symmetry.
EMPTY = "empty"  # no drawn paths to measure
CROWDED = "crowded"  # more nodes than NODE_CEILING
# The native engine's skip codes, in order: code i is SKIPS[i].
SKIPS = (EMPTY, CROWDED)


@dataclass(frozen=True)
class LayerScan:
    """Every symmetry's verdict for one layer, plus why it was skipped."""

    candidates: tuple[Candidate, ...]
    skipped: str | None
    nodes: int
    reach: float

    @property
    def safe_modes(self) -> tuple[str, ...]:
        """The symmetries this layer could take, best first."""
        return tuple(c.mode for c in self.candidates if c.safe)


def reach_of(points: list[Point]) -> float:
    """The diagonal of the outline's node bounding box, in LAYER coordinates.

    Sheared, deliberately. This is the yardstick a displacement is judged
    against, and what the designer sees is the ink as drawn — the same reason
    ``axis_span`` bounds the drawn axis in layer space rather than in the
    unsheared frame.

    Zero for an empty layer, and for the degenerate layer whose nodes all sit on
    one point. Callers must not divide by it blind; ``_share`` does not.
    """
    return azsym_native.reach_of(points)


def buried_nodes(paths: Paths) -> set[tuple[int, int]]:
    """Nodes that sit inside ink another contour already fills.

    Hugo, 2026-08-11. Designers draw with overlaps and remove them at export, so
    a stem can be a rectangle buried in a bowl and a serif a box buried in a
    stem. Nothing about the buried part is visible in the filled glyph — and the
    scan measured node displacement, so a buried contour that was drawn
    asymmetrically drove the whole number. MEASURED on this repo's ``H`` with
    two little boxes hidden inside its stems: 2.55 units and Safe becomes 251.64
    units and Reshapes, over ink nobody can see, while the visible outline is
    identical in both.

    SAME-WINDING only, which is what separates an overlap from a counter. A
    counter is inside its outer contour too, but it runs the other way round and
    is CARVING the shape — move one of its nodes and the hole changes, so it is
    every bit as visible as the outside. An overlap runs the same way as the ink
    that swallows it and adds nothing.

    So a node is buried when the other contours already wind around it in the
    same direction its own contour runs. On the ``o``: the counter's nodes sit
    at winding +1 from the outer contour while the counter itself runs -1, the
    signs disagree, and nothing is hidden.

    Approximate in one way worth stating: off-curve points are treated as
    vertices, so the polygon tested is the control polygon rather than the true
    curve. Every other measurement in this module treats nodes as positions the
    same way, and a curve never strays far enough from its own handles for the
    answer to flip on anything but a node already grazing an edge.
    """
    return azsym_native.buried_nodes(paths)


def measure(
    paths: Paths,
    centre: Point,
    mode: str,
    shear: float,
    upm: float = DEFAULT_UPM,
    offcurve: OffCurve | None = None,
) -> Candidate:
    """What binding ``mode`` to this outline would cost, without binding it.

    The cost is how far the drawn OUTLINE would move: per segment, the furthest
    the curve goes (issue #3, schriftgestalt: "It seems the curves confuse the
    classifier?"). A handle moved by d moves its curve by at most 3/4 d, so
    quoting the handle's own move put a dot with handles a few units uneven in
    the yellow. ``offcurve`` says which points are handles; without it the
    furthest point is quoted, as the Python kernel the engine was ported from
    did, and the parity corpus still checks that.

    BOTH source sides are measured and the WORSE is quoted, because which half
    the designer keeps is decided by the selection at bind time and the scan
    cannot know it. Quoting the worse is what makes "safe" mean safe either way.

    It would be cheaper to measure one side and call it both, and the algebra
    almost supports that: ``flip`` is an involution and an isometry, so
    ``|a - partner(b)| = |partner(a) - b|`` in the UNSHEARED frame. But
    ``partner`` is ``reshear . flip . unshear``, and a shear is not an isometry
    — it stretches the two residuals by different amounts once they are back in
    layer space. MEASURED on this repo's 12-degree italic: identical on every
    upright master, and up to a factor of 1.037 apart on the italic (``H``
    horizontal, 4.18 units keeping the bottom against 4.03 keeping the top).
    Small, and on these outlines it never straddles the threshold — but it is
    not zero, and a scanner that rounded it to zero would be asserting an
    identity that is false.
    """
    return _candidate(azsym_native.measure(paths, centre, mode, shear, upm, offcurve), mode)


def is_safe(shift: float, share: float, upm: float = DEFAULT_UPM) -> bool:
    """Both limits, and BOTH have to hold.

    Relative alone is blind to scale: half a percent of a 3000-unit ornament is
    15 font units, which is a visible shove however large the glyph. Absolute
    alone is blind the other way: a few units is nothing on a capital and a
    catastrophe on a period. Neither is the answer on its own.

    ``upm`` scales the absolute half. It defaults so that the pure module can be
    exercised without a font in hand; every call from the plugin passes the
    font's own.
    """
    return azsym_native.is_safe(shift, share, upm)


def proposal(
    paths: Paths,
    modes: tuple[str, ...],
    shear: float,
    sign: int | tuple[int, ...],
    centre: Point | None = None,
    split: Point | None = None,
) -> tuple[Paths, int]:
    """The outline a bind would leave, and how many nodes it could not pair.

    PUBLIC because the scan window's preview pane draws exactly this, and the
    Apply button writes exactly this. Sharing one function is what stops the
    preview drifting from what applying does — a promise no pair of tests can
    make as well as a single call site can.

    Bind each symmetry in turn, exactly as the designer would. Faithful to
    ``_bind`` in two ways that both matter. The second snap runs on the outline
    the FIRST one already changed, not on the original — so the cost of the pair
    is not the cost of either alone. And the centre is recomputed from the
    current nodes at every step, because that is what ``_bind`` does: MEASURED
    on this repo's ``H``, binding the horizontal mirror first moves the centre
    1.27 units before the vertical one is derived.

    ``sign`` is ONE side for every mode, or one PER mode. A batch keeps the same
    side throughout; a bind from the palette asks the selection about each mirror
    separately — nodes in the bottom left answer left for the vertical mirror and
    bottom for the horizontal one — so a pair it previews needs both answers.

    ``centre`` is a selected guide's (``azsym_geometry.guide_centre``), for the
    palette's hover over ONE switch. It runs exactly the calls ``_bind_layer``
    makes about a guide — ``fitted``, ``pair_nodes``, ``mirror_source`` — so the
    ghost and the click cannot disagree. A guide never reaches a pair: Analyze
    does not read guides and the palette has no pair switch. ``split`` is a
    typed Diagonal angle's split for the half-turn (``azsym_geometry.axis_split``),
    passed beside ``centre`` or, for a typed angle with no guide, alone.

    Never mutates ``paths``.
    """
    signs = (sign,) * len(modes) if isinstance(sign, int) else tuple(sign)
    if len(signs) != len(modes):  # what zip(modes, signs, strict=True) raised
        longer = "shorter" if len(signs) < len(modes) else "longer"
        raise ValueError(f"zip() argument 2 is {longer} than argument 1")
    if centre is None and split is not None:
        # A typed Diagonal angle with no guide (decision 8). Native ``reshape``
        # knows no split, so this takes the bind's own calls below, about the
        # bind's own centre: ``model_centre`` keeps a half-turn's box centre, and
        # only a half-turn has a split, so ``centre_of`` is where the click pairs.
        centre = centre_of([point for path in paths for point in path], shear)
    if centre is not None:
        if len(modes) != 1:
            raise ValueError("a guide centre previews one mode")
        moved_as = azsym_binding.fitted(paths, centre, modes[0], shear, signs[0])
        binding = azsym_binding.pair_nodes(paths, centre, moved_as, shear, signs[0], split)
        writes = azsym_binding.mirror_source(paths, binding, centre, moved_as, shear)
        amended = [list(path) for path in paths]
        for (path_index, node_index), point in writes.items():
            amended[path_index][node_index] = point
        return amended, len(binding.unpaired)
    xy, written, unpaired = azsym_native.reshape(False, paths, modes, signs, shear)
    return _amended(paths, xy, written), unpaired


def settlement(paths: Paths, modes: tuple[str, ...], shear: float) -> tuple[Paths, int]:
    """The outline a SYMMETRISE would leave, and how many nodes it could not pair.

    The counterpart to ``proposal``, and PUBLIC for the same reason: the scan
    window's Symmetrise button writes exactly this, so preview and write cannot
    drift apart.

    NO ``sign``, and that is the whole point of the operation. Binding has to
    know which half survives; meeting in the middle has no source side, so there
    is no half to choose and none discarded. ``proposal`` measures both sides and
    quotes the worse; there is only one answer here.

    ``pair_nodes`` still needs a sign to build a ``Binding`` with, and ``-1`` is
    passed arbitrarily: ``meet_source`` gives the same writes either way, because
    swapping source for target swaps the two halves of a midpoint. Pinned by
    ``test_the_side_a_settlement_pairs_from_makes_no_difference``.

    Everything else follows ``proposal`` exactly, and for the same reasons: each
    mode runs on the outline the previous one already settled, and the centre is
    re-derived at every step from the nodes as they now stand.

    Never mutates ``paths``.
    """
    xy, written, unpaired = azsym_native.reshape(True, paths, modes, (), shear)
    return _amended(paths, xy, written), unpaired


def measure_combo(
    paths: Paths,
    modes: tuple[str, ...],
    shear: float,
    upm: float = DEFAULT_UPM,
    offcurve: OffCurve | None = None,
) -> Candidate:
    """What binding several symmetries at once would cost, without binding them.

    Simulated rather than inferred. The obvious shortcut — take the worse of the
    two single-mode costs — understates it: MEASURED on this repo's ``H``, the
    pair costs 4.74 units where the worse single costs 4.18, because the second
    snap works on an outline the first has already moved.

    ORDER IS MEASURED, not assumed away. Binding the vertical mirror first costs
    ``H`` 4.03-4.18 units and binding the horizontal first costs 4.74 — a factor
    of 1.18 apart — and the designer chooses the order by which switch they
    click first. Every order and both source sides are measured and the worst is
    quoted, so "safe" means safe however they go about it.

    One order failing to pair condemns the whole pair, even if another order
    pairs cleanly. That is deliberate: the report has no way to say "bind the
    horizontal one first", so a row offering a pair that only works one way
    round would be an instruction the designer cannot read.
    """
    return _candidate(
        azsym_native.measure_combo(paths, modes, shear, upm, offcurve), combo_name(modes)
    )


def scan_layer(
    paths: Paths,
    shear: float,
    ceiling: int = NODE_CEILING,
    upm: float = DEFAULT_UPM,
    offcurve: OffCurve | None = None,
) -> LayerScan:
    """Measure all three symmetries against one layer's outline.

    Candidates come back ranked by how little the snap would move the shape, so
    the first one is the symmetry this layer most plausibly has. That ranking is
    only ever applied to fully-paired modes — an unpaired mode has no meaningful
    displacement to rank by, so it sorts last on its verdict rather than on its
    zero.

    The centre is derived exactly as binding derives it, from the layer's own
    nodes. Scanning against a different centre than the bind would use would
    make the report a description of some other operation.
    """
    return scan_from_native(azsym_native.scan_layer(paths, shear, ceiling, upm, COMBOS, offcurve))


# --------------------------------------------------------------- a whole file


# Most rows the WORTH-A-LOOK band prints before summarising the rest. A whole
# file yields far more near-misses than anyone reads, and a report that scrolls
# for pages before reaching the answer has buried it. Nothing is dropped
# silently — the count that did not fit is always printed.
#
# The SAFE band is deliberately not capped. It is the answer to the question the
# designer asked, and a truncated answer is the one thing this report cannot
# afford to give: a file with 300 symmetric glyphs would report 40 and read as
# though that were all of them.
MAX_ROWS = 40


@dataclass(frozen=True)
class LayerFinding:
    """One scanned layer, named the way the designer knows it.

    ``bound`` names the symmetries this layer is ALREADY bound to. Those are
    neither candidates nor problems — they are done — so they are counted in the
    tally and kept out of both listed bands, along with everything they already
    outrank. A survey that offered the designer a symmetry they switched on last
    week reads as not having looked. See ``band_of``.
    """

    glyph: str
    master: str
    scan: LayerScan
    components: bool = False
    bound: tuple[str, ...] = ()
    # The master's id, for selecting it again from a listed row. Empty when the
    # layer could not be asked — a row that cannot re-find its master still
    # lists, it just cannot switch to it.
    master_id: str = ""
    # Where this master sits in the font. Masters are ordered by weight and
    # width, not alphabetically, so sorting rows on the NAME interleaves Black
    # with Bold and drops Hairline into the middle — see ``sort_rows``.
    master_index: int = 0


@dataclass(frozen=True)
class FontScan:
    """Every layer walked in one file, measured or accounted for.

    ``unreadable`` counts layers whose outline could not be read at all. Those
    are neither safe nor unsafe — they are unknown, and a scan that folded them
    into either number would be claiming something it did not measure.
    """

    findings: tuple[LayerFinding, ...]
    glyphs: int = 0
    unreadable: int = 0
    # The em the absolute Safe cap was judged against, carried so the report can
    # print the number it actually used rather than a constant that no longer
    # describes this file. See ``safe_units``.
    upm: float = DEFAULT_UPM

    @property
    def measured(self) -> tuple[LayerFinding, ...]:
        return tuple(f for f in self.findings if f.scan.skipped is None)


# What a row is filed under, for the window's filter and the text report's
# bands. Ordered best-first: this is also the sort key, so "Safe" always floats
# to the top however the underlying numbers land.
BAND_SAFE = "Safe"
BAND_NEAR = "Worth a look"
BAND_RESHAPES = "Reshapes"
BAND_UNPAIRED = "No pairing"
BAND_BOUND = "Bound"
BANDS = (BAND_SAFE, BAND_NEAR, BAND_RESHAPES, BAND_UNPAIRED, BAND_BOUND)

# What the WINDOW lists (Hugo, 2026-08-11). A survey is a to-do list, and the
# other three bands are not things to do: "Reshapes" and "No pairing" say this
# glyph does not have the symmetry, and "Bound" says it already does. Banding
# still computes all five — the counts in the summary line depend on it, and a
# row has to be classified before it can be left out.
LISTED_BANDS = (BAND_SAFE, BAND_NEAR)


@dataclass(frozen=True)
class Row:
    """One (layer, symmetry) pair, ready to be listed.

    Carries ``master_id`` alongside the master's NAME because the name is for
    the designer to read and the id is what selects the master again — two
    masters can share a name, and nothing stops a name being edited between the
    scan and the double-click.
    """

    glyph: str
    master: str
    master_id: str
    symmetry: str
    shift: float
    share: float
    band: str
    components: bool
    master_index: int = 0
    # Every master this row stands for. One id on an ordinary row; all of them
    # on a merged "All Masters" row, which is what Apply has to bind and what
    # a single ``master_id`` cannot carry.
    master_ids: tuple[str, ...] = ()

    @property
    def masters(self) -> tuple[str, ...]:
        """The master ids to act on. Never empty for a row built by this module."""
        return self.master_ids or (self.master_id,)

    @property
    def merged(self) -> bool:
        return len(self.masters) > 1


# What a merged row calls itself in the Master column.
ALL_MASTERS = "All Masters"


def master_counts(scan: FontScan) -> dict[str, int]:
    """How many master layers each glyph has, MEASURED or not.

    Skipped layers are counted. A glyph whose Bold was too crowded to measure
    has not had every master offered this symmetry, so it must not collapse into
    a row claiming all of them — the whole point of the label is that there is
    nothing left to check.
    """
    counts: dict[str, int] = {}
    for finding in scan.findings:
        counts[finding.glyph] = counts.get(finding.glyph, 0) + 1
    return counts


def merge_rows(rows: list[Row], counts: dict[str, int]) -> list[Row]:
    """Collapse a glyph's per-master rows into one when they all agree.

    All of them, on the same symmetry AND in the same band. Band equality is the
    part worth insisting on: a glyph that is Safe in Regular and Reshapes in Bold
    has exactly one master worth looking at, and a single row labelled Safe would
    hide it.

    The merged row carries the WORST master's numbers. A survey exists to be
    trusted, and an average would let one master's large snap disappear behind
    three small ones.

    Single-master fonts never merge: "All Masters" would be a longer way of
    writing the one master's name.
    """
    groups: dict[tuple[str, str, str], list[Row]] = {}
    order: list[tuple[str, str, str]] = []
    for row in rows:
        key = (row.glyph, row.symmetry, row.band)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    merged: list[Row] = []
    for key in order:
        group = groups[key]
        total = counts.get(key[0], 0)
        if len(group) < 2 or total < 2 or len(group) != total:
            merged.extend(group)
            continue
        worst = max(group, key=lambda row: row.share)
        merged.append(
            Row(
                glyph=worst.glyph,
                master=ALL_MASTERS,
                master_id=group[0].master_id,
                symmetry=worst.symmetry,
                shift=max(row.shift for row in group),
                share=worst.share,
                band=worst.band,
                components=any(row.components for row in group),
                master_index=min(row.master_index for row in group),
                master_ids=tuple(row.master_id for row in group),
            )
        )
    return merged


def best_per_glyph(rows: list[Row], counts: dict[str, int]) -> list[Row]:
    """One row per GLYPH: its best symmetry, on every master that can take it.

    Hugo, 2026-09-10 — propose a symmetry at glyph level and not per layer. A
    table with a row per master asks the same question six times, and "is this
    glyph symmetric?" is a claim about the drawing rather than about one
    interpolation of it.

    ``best_per_layer`` has already reduced each layer to a single proposal, so
    the pick here is only between the symmetries a glyph's masters DISAGREE on:
    ``symmetry_rank`` again, strongest claim wins.

    EVERY MASTER OR NOTHING (Hugo, 2026-09-10). A glyph whose masters do not all
    offer the winning symmetry is not proposed at all. Binding the ones that
    disagree would reshape them, and binding only the rest leaves one glyph
    symmetric in four masters and free in two — an interpolation that does not
    hold, which is worse than not offering it. A master that could not be
    measured counts against coverage for the same reason: nothing was learned
    about it, so nothing can be claimed for it.

    The row carries the WORST of what it stands for: the worst band, the
    largest shift, the largest share. A survey exists to be trusted, and
    averaging would let one master's big snap hide behind five small ones. It
    is also why the band cannot be the best master's: a glyph that is Safe in
    five masters and Reshapes in the sixth is not a glyph you bind without
    looking.

    Rows arriving here may ALREADY stand for several masters — ``report_rows``
    merges before the window ever sees them — so coverage is counted in
    ``masters`` and never in rows.
    """
    groups: dict[str, list[Row]] = {}
    order: list[str] = []
    for row in rows:
        if row.glyph not in groups:
            groups[row.glyph] = []
            order.append(row.glyph)
        groups[row.glyph].append(row)

    reduced: list[Row] = []
    for glyph in order:
        group = groups[glyph]
        pick = min(group, key=lambda row: symmetry_rank(row.symmetry))
        kept = [row for row in group if row.symmetry == pick.symmetry]
        ids: list[str] = []
        for row in kept:
            ids.extend(master_id for master_id in row.masters if master_id not in ids)
        worst = max(kept, key=lambda row: BANDS.index(row.band))
        total = counts.get(glyph, len(ids))
        if len(ids) < total:
            continue
        reduced.append(
            Row(
                glyph=glyph,
                # Every row covers every master by now, so the label is not a
                # count. A single-master font still says the master's NAME:
                # "All Masters" would be a longer way of writing it.
                master=kept[0].master if total <= 1 else ALL_MASTERS,
                master_id=ids[0] if ids else kept[0].master_id,
                symmetry=pick.symmetry,
                shift=max(row.shift for row in kept),
                share=max(row.share for row in kept),
                band=worst.band,
                components=any(row.components for row in kept),
                master_index=min(row.master_index for row in kept),
                master_ids=tuple(ids),
            )
        )
    return reduced


def band_of(finding: LayerFinding, candidate: Candidate) -> str:
    """Which band this candidate belongs in.

    ``Bound`` wins over everything, and it is decided by RANK rather than by
    membership (Hugo, 2026-08-11): a candidate is filed away as done when what
    the layer already carries ranks at or above it. Membership alone —
    "is every mode of this candidate switched on?" — left a layer bound to the
    pair still being offered ``rotate``, which is the half-turn that pair
    already IS.

    Rank is the right relation because ``symmetry_rank`` orders by strength:
    the pair outranks either mirror, and either mirror outranks the half-turn.
    So it suppresses exactly the proposals that would reach a state the layer
    is already in, or a weaker one:

        bound         suppressed                       still offered
        V + H         everything
        vertical      vertical, horizontal, rotate     V + H
        horizontal    horizontal, rotate               V + H, vertical
        rotate        rotate                           V + H, vertical, horizontal

    Suppressing ``horizontal`` on a vertically-bound layer costs the designer
    nothing: binding it there reaches the same state as the ``V + H`` row,
    which is still listed. What it removes is the same decision offered twice.

    An UPGRADE is never suppressed — ``V + H`` outranks every single mode — so
    a layer bound to one mirror still gets offered the pair.
    """
    if finding.bound and symmetry_rank(bound_name(finding.bound)) <= symmetry_rank(candidate.mode):
        return BAND_BOUND
    if candidate.verdict == UNPAIRED:
        return BAND_UNPAIRED
    if candidate.safe:
        return BAND_SAFE
    if candidate.share <= NEAR_SHARE:
        return BAND_NEAR
    return BAND_RESHAPES


def window_rows(scan: FontScan) -> list[Row]:
    """Exactly what the scan window lists.

    The whole policy in one place, in the one order that works: band filter,
    then one symmetry per layer, then the move ceiling, then merge the masters
    that agree, then sort. Merging LAST because it can only see agreement once
    each layer has been reduced to a single proposal.
    """
    return window_order(listed(report_rows(scan)), master_counts(scan), scan.upm)


def window_order(rows: list[Row], counts: dict[str, int], upm: float = DEFAULT_UPM) -> list[Row]:
    """The last steps of ``window_rows``, for re-applying after a press.

    The window re-measures the layers it just bound or settled and splices the
    fresh rows back in; they have to go through the same reduction, or one
    applied glyph quietly expands back into four rows and three masters.

    The move ceiling runs AFTER ``best_per_layer`` and not before it, which is
    the difference between hiding a layer and re-ranking it. A layer is shown
    the one symmetry its rank picks; if THAT proposal is past the ceiling, the
    layer has nothing worth listing and goes. Dropping the expensive rows first
    would instead let a lower-ranked, cheaper symmetry take the slot — cost
    deciding which symmetry is proposed, which this module refuses everywhere
    else (see ``best_per_layer``: a single flip never loses its rank to a
    tighter measurement, and the combination veto is the one narrow exception).

    Nothing is measured twice to do it: the ceiling reads the ``shift`` already
    on the row, which is the number the Moves column prints.
    """
    picked = [row for row in best_per_layer(rows) if row.shift <= move_ceiling(upm)]
    reduced = best_per_glyph(picked, counts)
    sort_by_cost(reduced)
    return reduced


def report_rows(scan: FontScan) -> list[Row]:
    """Every measured (layer, symmetry) pair, banded and ranked.

    Deliberately wider than ``report_lines``: a scrollable, filterable window
    can afford to hold the pairs the text report only counts, and being able to
    ask "why was THIS glyph not offered anything?" is most of what a designer
    wants from a survey. The text report cannot do that without burying its own
    answer, so the two differ on purpose.

    Safe first, then by which symmetry is worth binding, then by how little the
    snap would disturb the outline, then by name — so the top of the list is
    always the most plausible thing to bind.
    """
    rows = [
        Row(
            glyph=finding.glyph,
            master=finding.master,
            master_id=finding.master_id,
            symmetry=candidate.mode,
            shift=candidate.shift,
            share=candidate.share,
            band=band_of(finding, candidate),
            components=finding.components,
            master_index=finding.master_index,
            master_ids=(finding.master_id,),
        )
        for finding in scan.measured
        for candidate in finding.scan.candidates
    ]
    rows = merge_rows(rows, master_counts(scan))
    sort_rows(rows)
    return rows


def sort_by_cost(rows: list[Row]) -> None:
    """The order the WINDOW lists rows in: least movement first, in place.

    Hugo, 2026-09-10 — sort on the Outline changes column. That column draws
    how little a row would move, so ascending shift puts the fullest bars at the
    top and the table reads as a ranking: everything worth binding without
    thinking about it, then everything worth a look, then the rest.

    It also retires the band as an ordering. Safe rows are the small shifts, so
    they come first by arithmetic rather than by a rule — which is what makes
    one un-split list legible in the first place.

    The glyph name breaks ties, so two rows that measure identically do not swap
    places between one scan and the next.
    """
    rows.sort(key=lambda row: (row.shift, row.glyph, symmetry_rank(row.symmetry)))


def sort_rows(rows: list[Row]) -> None:
    """Put rows in the order the window lists them, in place.

    Band, then which symmetry is worth binding, then GLYPH NAME. Alphabetical
    and not by how little the snap disturbs the outline (Hugo, 2026-08-11):
    ``share`` has no column of its own any more, so ordering by it left a list
    whose sequence the designer could not see the reason for. A name is
    scannable — you can find the glyph you were thinking of.

    A function rather than an inline ``sort`` because the window RE-SORTS after
    an apply, when it splices freshly re-measured rows back into the survey it
    is already showing. Two copies of this key drifted apart once already; one
    of them living here is what stops the list silently re-ordering itself the
    moment Apply is pressed.
    """
    rows.sort(
        key=lambda row: (
            row.glyph,
            row.master_index,
            row.master,
            symmetry_rank(row.symmetry),
        )
    )


def _outmoved_by_a_single(row: Row, layer_rows: list[Row]) -> Row | None:
    """The member flip a combination cannot justify itself against, if any.

    Hugo, 2026-08-11: a pair that moves the outline further than one of its own
    flips would is not a discovery about the shape, it is a worse deal offered
    under a stronger name. MEASURED on this repo's own outlines — ``H`` snaps
    149.00 units under either mirror alone and 149.05 under both, and ``o``
    76.66 against 77.20 — because the second snap works on an outline the first
    has already moved.

    So the pair is kept only while it costs no more than a single flip does,
    which in practice means the layer really is symmetric both ways: a true
    rectangle measures 0.00 under each mirror AND under the pair, and ties.

    The replacement is the CHEAPEST member rather than the highest-ranked one.
    Rank is what decides between symmetries that are each worth binding — see
    ``best_per_layer`` — but this is a veto ON COST, and answering it with a
    flip that moves the outline even further than the pair did would be the
    same trade the veto just refused.

    ``COMBO_SLACK`` because a difference the Moves column cannot show is not a
    difference the designer can act on, and the stronger claim should not lose
    to a rounding error.
    """
    members = set(combo_modes(row.symmetry))
    if len(members) < 2:
        return None
    singles = [other for other in layer_rows if other.symmetry in members]
    if not singles:
        # Nothing cheaper is on offer: the members were measured, but they did
        # not survive the band filter, so there is no single flip to fall back
        # to and the pair stands on its own.
        return None
    cheapest = min(singles, key=lambda other: (other.shift, symmetry_rank(other.symmetry)))
    return cheapest if cheapest.shift < row.shift - COMBO_SLACK else None


def best_per_layer(rows: list[Row]) -> list[Row]:
    """One row per layer: the best symmetry it could take, and only that one.

    Hugo, 2026-08-11 — never propose more than one symmetry for a layer. A
    survey offering four rows for the same glyph is offering the same decision
    four times, and binding two of them is the same act as binding the pair.

    "Best" is ``symmetry_rank``: vertical + horizontal, then vertical, then
    horizontal, then rotate. Rank ALONE, not the band — among rows that are
    already worth doing, the strongest claim about the shape wins even if a
    weaker symmetry happens to measure tighter.

    With ONE exception, which is a veto and not a re-ranking: a combination has
    to cost no more than its own flips do, or it loses to the cheapest of them.
    See ``_outmoved_by_a_single``. Nothing else in this order is decided on
    cost, and a single flip never loses its rank to a tighter measurement.

    Run AFTER the band filter and BEFORE the master merge: the pick is per
    layer, and masters can only collapse once each has been reduced to the one
    symmetry it is offering.
    """
    by_layer: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        by_layer.setdefault((row.glyph, row.master_id), []).append(row)

    best = []
    for layer_rows in by_layer.values():
        pick = min(layer_rows, key=lambda row: symmetry_rank(row.symmetry))
        beaten_by = _outmoved_by_a_single(pick, layer_rows)
        best.append(pick if beaten_by is None else beaten_by)
    return best


def listed(rows: list[Row]) -> list[Row]:
    """Only the rows the WINDOW shows: the two bands that are things to do.

    Applied by the window rather than by ``report_rows``, which stays the whole
    model — every measured pair, banded. The text report and the banding tests
    both depend on the other three still being produced, and "why was this glyph
    not offered anything?" is answerable only while they exist.
    """
    return [row for row in rows if row.band in LISTED_BANDS]


def band_counts(rows: list[Row]) -> dict[str, int]:
    """How many rows each band holds, including the bands with none.

    Every band is present so a filter built from this cannot silently lose a
    button when a particular scan happens to have nothing in that band.
    """
    # Annotated, because ``dict.fromkeys`` over a tuple of string literals
    # infers a Literal-keyed dict that nothing else in the module can index.
    #
    # Over LISTED_BANDS and not BANDS: these counts label the window's filter,
    # and a button for a band the window never lists would always read (0).
    counts: dict[str, int] = dict.fromkeys(LISTED_BANDS, 0)
    for row in rows:
        if row.band in counts:
            counts[row.band] += 1
    return counts


def summary_line(scan: FontScan, rows: list[Row]) -> str:
    """One line naming what was measured and what came of it.

    A row is not a layer, in either direction, and the count has to survive
    both. ``report_rows`` gives one layer FOUR rows — one per symmetry it was
    offered — so rows are de-duplicated; and a merged row is one row standing
    for every master, so it is expanded over ``masters`` first. Counting the
    rows themselves got the second one wrong on every multi-master font whose
    masters agree: "3 layer(s) across 1 glyph(s) — ... over 1 measured
    layer(s)", a sentence at odds with its own opening clause.

    ``master_id`` and not ``master``: two masters may share a NAME, which would
    quietly count them as one.
    """
    counts = band_counts(rows)
    layers = len({(row.glyph, master_id) for row in rows for master_id in row.masters})
    return (
        f"{len(scan.measured)} layer(s) across {scan.glyphs} glyph(s) — "
        f"{counts[BAND_SAFE]} safe, {counts[BAND_NEAR]} worth a look, "
        f"over {layers} measured layer(s)."
    )


def _rows(findings: tuple[LayerFinding, ...], keep) -> list[tuple[LayerFinding, Candidate]]:
    """Every (layer, mode) pair matching ``keep``, closest to symmetric first.

    A mode already bound on that layer never appears, whichever band asked.
    """
    rows = [
        (finding, candidate)
        for finding in findings
        for candidate in finding.scan.candidates
        if band_of(finding, candidate) != BAND_BOUND and keep(candidate)
    ]
    rows.sort(key=lambda row: (row[1].share, row[0].glyph, row[0].master))
    return rows


# Wide enough for the longest combination name, so a pair does not push the
# numbers out of line with the single-mode rows above it.
SYMMETRY_WIDTH = max(len(combo_name(pair)) for pair in COMBOS) if COMBOS else 11


def _row_text(finding: LayerFinding, candidate: Candidate) -> str:
    mark = "   components" if finding.components else ""
    return (
        f"  {finding.glyph:<18} {finding.master:<14}"
        f" {candidate.mode:<{SYMMETRY_WIDTH}}"
        f" {candidate.shift:8.1f}u {candidate.share * 100:6.2f}%{mark}"
    )


def _band(
    title: str,
    rows: list[tuple[LayerFinding, Candidate]],
    limit: int | None = None,
) -> list[str]:
    """One titled block of rows, truncated loudly rather than quietly.

    ``limit`` of None prints every row, which is what the safe band wants.
    """
    if not rows:
        return []
    shown = rows if limit is None else rows[:limit]
    lines = [title, ""]
    lines.extend(_row_text(finding, candidate) for finding, candidate in shown)
    if len(shown) < len(rows):
        lines.append(f"  ... and {len(rows) - len(shown)} more, not shown.")
    lines.append("")
    return lines


def report_lines(scan: FontScan) -> list[str]:
    """The scan as the designer reads it in the Macro window.

    Safe candidates are listed in full, because they are the answer to the
    question. Near-misses are listed too, because a shape drawn 6% off symmetry
    is a judgement call the plugin should not make silently on the designer's
    behalf — the measurement is printed so it can be made. Everything else is
    counted, not listed: a whole file's worth of "this glyph is not symmetric"
    would bury both.
    """
    measured = scan.measured
    safe = _rows(measured, lambda c: c.safe)
    near = _rows(
        measured,
        lambda c: c.verdict == RESHAPES and c.share <= NEAR_SHARE,
    )
    layers_with_safe = {(f.glyph, f.master) for f, _ in safe}

    lines = [
        f"[AZ Symmetrify] Symmetry scan: {len(measured)} layer(s) measured"
        f" across {scan.glyphs} glyph(s).",
        "",
        "Nothing has been bound. Binding stays a per-layer decision — this is"
        " the survey, not the edit.",
        "",
    ]

    if safe:
        lines.extend(
            _band(
                f"SAFE TO BIND — {len(safe)} symmetry/layer pair(s) over"
                f" {len(layers_with_safe)} layer(s). Every node pairs and the snap"
                f" moves no node further than {SAFE_SHARE * 100:.1f}% of the"
                f" outline's own size, or {safe_units(scan.upm):.0f} font units"
                " — whichever is the smaller:",
                safe,
            )
        )
    else:
        lines.extend(["SAFE TO BIND — nothing. No layer takes a symmetry cleanly.", ""])

    lines.extend(
        _band(
            f"WORTH A LOOK — {len(near)} pair(s) between {SAFE_SHARE * 100:.1f}% and"
            f" {NEAR_SHARE * 100:.0f}%. Fully paired, so the symmetry IS there;"
            " it was drawn loosely enough that the snap would visibly tighten it,"
            " which may be what you want or may not:",
            near,
            limit=MAX_ROWS,
        )
    )

    lines.extend(_tally(scan, measured, safe, near))
    return lines


def _tally(
    scan: FontScan,
    measured: tuple[LayerFinding, ...],
    safe: list,
    near: list,
) -> list[str]:
    """What the two listed bands leave unaccounted for.

    Printed as counts with their reasons. A scan whose bottom line did not add
    up to the layers it walked would leave the designer wondering which glyphs
    it had quietly given up on.
    """
    free = [
        candidate
        for finding in measured
        for candidate in finding.scan.candidates
        if band_of(finding, candidate) != BAND_BOUND
    ]
    reshapes = sum(1 for candidate in free if candidate.verdict == RESHAPES)
    unpaired = sum(1 for candidate in free if candidate.verdict == UNPAIRED)
    # What was banded away, and NOT ``sum(len(finding.bound))``. The sentence
    # below claims a number of pairs left out of the lists, and the modes a
    # layer has switched on has never been that number — a layer bound to the
    # vertical mirror alone now takes four pairs out of the lists, not one.
    already = sum(len(finding.scan.candidates) for finding in measured) - len(free)
    crowded = [f for f in scan.findings if f.scan.skipped == CROWDED]
    empty = sum(1 for f in scan.findings if f.scan.skipped == EMPTY)

    lines = ["EVERYTHING ELSE:", ""]
    if already:
        lines.append(
            f"  {already} symmetry/layer pair(s) are bound already and were left"
            " out of the lists above."
        )
    lines.append(
        f"  {reshapes - len(near)} pair(s) pair fully but would reshape the glyph"
        f" by more than {NEAR_SHARE * 100:.0f}%."
    )
    lines.append(
        f"  {unpaired} pair(s) left a node without a partner, so that symmetry"
        " does not describe the outline at all."
    )
    if empty:
        lines.append(f"  {empty} layer(s) hold no paths.")
    if crowded:
        names = ", ".join(f"{f.glyph} {f.master}" for f in crowded[:5])
        tail = "" if len(crowded) <= 5 else f", and {len(crowded) - 5} more"
        lines.append(
            f"  {len(crowded)} layer(s) exceed {NODE_CEILING} nodes and were NOT"
            f" measured ({names}{tail}). Pairing is quadratic in node count;"
            " scan those one at a time."
        )
    if scan.unreadable:
        lines.append(f"  {scan.unreadable} layer(s) could not be read and were not measured.")
    if any(f.components for f, _ in safe):
        lines.append(
            "  Rows marked '+ components' hold components as well as paths."
            " Mirroring only ever touches paths, so the components stay put."
        )
    lines.append("")
    return lines


def _candidate(raw: azsym_native.RawCandidate, mode: str | None = None) -> Candidate:
    """A native candidate as the ``Candidate`` Python built: the mode the caller
    asked about when it is given, else the mode or combination the code names."""
    if mode is None:
        if raw.mode >= azsym_native.COMBO_BASE:
            mode = combo_name(COMBOS[raw.mode - azsym_native.COMBO_BASE])
        else:
            mode = azsym_native.MODE_NAMES[raw.mode]
    return Candidate(
        mode=mode,
        verdict=VERDICTS[raw.verdict],
        shift=raw.shift,
        share=raw.share,
        unpaired=raw.unpaired,
        nodes=raw.nodes,
    )


def _amended(paths: Paths, xy: list[float], written: list[bool]) -> Paths:
    """``[list(path) for path in paths]`` with every node a write moved replaced
    by a new tuple — what ``_with`` built. A node no write touched is still the
    caller's own object, exactly as it was."""
    amended = [list(path) for path in paths]
    at = 0
    for path in amended:
        for index in range(len(path)):
            if written[at]:
                path[index] = (xy[2 * at], xy[2 * at + 1])
            at += 1
    return amended


def scan_from_native(raw: tuple) -> LayerScan:
    """A ``LayerScan`` from what the native engine reports for one layer —
    ``azsym_native.scan_layer``'s result, or one layer of the native survey."""
    candidates, skipped, nodes, reach = raw
    return LayerScan(
        candidates=tuple(_candidate(candidate) for candidate in candidates),
        skipped=None if skipped < 0 else SKIPS[skipped],
        nodes=nodes,
        reach=reach,
    )
