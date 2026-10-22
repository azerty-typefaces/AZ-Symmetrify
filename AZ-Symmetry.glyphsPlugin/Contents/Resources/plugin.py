"""AZ Symmetrify — keep a layer symmetric while you draw.

Flip a switch in the AZ Symmetrify palette and the axis places itself: the
centre of the layer's nodes, measured in the frame the italic was sheared from.
There is no guide to position, because there is no useful freedom in where the
axis goes — a glyph is symmetric about its own centre or it is not symmetric.

That switch row is the entire interface for binding, and it lives in a palette
beside the Edit view (``AZSymmetryPalette``). Everything font-wide lives in one window,
opened from ``Window > AZ Symmetrify``, with two panes behind a toolbar: Browse
draws every layer that already carries a symmetry, and Analyze lists, once per
GLYPH, what the strongest symmetry its masters agree on would cost. Switching to Analyze does
NOT survey — the pane opens disabled, with the scan behind a button in the
middle of its own table. Neither pane writes anything on its own; binding in
bulk is a button in Analyze, and it goes through the same code the switch row
does.

This file is the only one that touches Glyphs objects. It translates GSPath and
GSNode into plain ``(x, y)`` tuples, hands them to the pure azsym_* modules, and
writes the results back. Keep logic out of here so it stays testable.

Why a general plugin and not a reporter: a reporter only draws — and therefore
only ticks — while its View menu item is checked, so unchecking it silently
stopped mirroring. ``Glyphs.addCallback(..., DRAWFOREGROUND)`` registers a plain
function with no visibility flag to gate it, so the tick cannot be switched off
by accident. Verified against a live Glyphs 4.0.1.
"""

from __future__ import annotations

import contextlib
import functools
import math
import os
import sys
import time
import traceback
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import replace
from typing import Any

import objc
from AppKit import (
    NSAffineTransform,
    NSAlert,
    NSApplication,
    NSAttributedString,
    NSBezierPath,
    NSButton,
    NSColor,
    # Foundation, re-exported by AppKit in PyObjC. With NSRunLoop below, these
    # give the scan's progress sheet one turn of the run loop to appear in —
    # see ``_ScanProgress``.
    NSDate,
    NSEvent,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSGraphicsContext,
    NSImage,
    NSMenuItem,
    NSNotificationCenter,
    NSPoint,
    NSRunLoop,
    # The run-loop mode set a menu's own tracking mode belongs to. Without it a
    # timer stops dead while any menu is open — see ``_repeating_timer``.
    NSRunLoopCommonModes,
    NSSegmentedControl,
    NSSegmentStyleRounded,
    NSSegmentSwitchTrackingSelectAny,
    NSTextField,
    NSTimer,
    NSTrackingArea,
    NSView,
    # NSAlert.h: NSAlertStyleWarning = 0, under its older spelling — the one
    # every macOS this plugin runs on has.
    NSWarningAlertStyle,
)
from GlyphsApp import (
    DOCUMENTOPENED,
    DRAWFOREGROUND,
    GSSHARP,
    GSSMOOTH,
    UPDATEINTERFACE,
    WINDOW_MENU,
    Glyphs,
    GSGuide,
    GSNode,
    GSPath,
)
from GlyphsApp.plugins import GeneralPlugin, PalettePlugin

try:
    # Glyphs' own name for "a document went away", which is when the per-layer
    # caches stop describing anything. Guarded rather than imported flat, for
    # the reason the block below gives: a host without the name must lose the
    # cache sweep, not the plugin.
    from GlyphsApp import DOCUMENTCLOSED
except ImportError:
    DOCUMENTCLOSED = None

try:
    # Glyphs 4 only: GSBase.h declares GSNodeConnectionSuperSmooth = 102, and
    # Glyphs 3's GlyphsApp module has no name for it at all. Guarded rather than
    # imported flat, because an ImportError in this block takes the whole plugin
    # down before ``start`` ever runs.
    from GlyphsApp import GSSUPERSMOOTH
except ImportError:
    GSSUPERSMOOTH = 102

try:
    # NSCell.h: NSControlSizeSmall = 1. Guarded and named for the same reason
    # the segment-tracking enum is: the enum runs Regular=0, Small=1, Mini=2,
    # Large=3, and a hand-numbered 2 here would silently give MINI — a control
    # that looks deliberately shrunk rather than merely small.
    from AppKit import NSControlSizeSmall as SMALL_CONTROL
except ImportError:  # pragma: no cover - present on every supported macOS
    SMALL_CONTROL = 1


try:
    # vanilla ships with Glyphs through the Plugin Manager rather than with the
    # application, so it is present on almost every install and guaranteed on
    # none. Guarded for the same reason as GSSUPERSMOOTH above: a bare import
    # that raises here takes the whole plugin down before ``start`` runs, and
    # losing live mirroring because a REPORT could not draw would be a
    # catastrophic trade. Without it the scan prints to the Macro window exactly
    # as it did before the window existed — see ``_show_scan``.
    import vanilla as _vanilla
except Exception:  # noqa: BLE001 - any failure here means "no window", not "crash"
    _vanilla = None

# Rebound through a loosely typed name on purpose. The checker narrows the
# except branch above to None and then reports every control constructor as an
# attribute of None; the real check is the import guard, which runs once at
# import time, and ``_show_scan`` tests for None before touching this at all.
vanilla: Any = _vanilla

try:
    # The Analyze table, and everything that draws a cell in it. ONE guard for
    # the whole family: they arrived in vanilla together, so a host that has any
    # of them has all of them, and a per-name guard would be four branches
    # standing in for one fact.
    #
    # There is no half-measure fallback here, unlike the cells this replaces. A
    # host without List2 falls through to the printed report, which is where the
    # whole feature falls back anyway.
    from vanilla import CheckBoxList2Cell as _CheckBoxList2Cell
    from vanilla import ImageList2Cell as _ImageList2Cell
    from vanilla import LevelIndicatorList2Cell as _LevelIndicatorList2Cell
    from vanilla import List2 as _List2
except ImportError:  # pragma: no cover - older vanilla
    _CheckBoxList2Cell = None
    _ImageList2Cell = None
    _LevelIndicatorList2Cell = None
    _List2 = None

# Rebound through loosely typed names, for the reason ``vanilla`` below is: the
# checker narrows the except branch to None and then reports every use as
# calling None, while the real check is the import guard — which runs once, at
# import time, and which ``_build_analyze`` tests for before building anything.
CheckBoxList2Cell: Any = _CheckBoxList2Cell
ImageList2Cell: Any = _ImageList2Cell
LevelIndicatorList2Cell: Any = _LevelIndicatorList2Cell
List2: Any = _List2

try:
    # What pushes the progress sheet's redraw to the screen while the main
    # thread is blocked — see ``_flush_drawing``. From Quartz because that is
    # where PyObjC puts the QuartzCore bindings; guarded because a host without
    # them should lose the flush and keep the scan.
    from Quartz import CATransaction
except ImportError:  # pragma: no cover - no QuartzCore binding
    CATransaction = None

try:
    # LOAD-BEARING, not defensive: a bare ``import vanilla`` does not
    # populate ``vanilla.dialogs`` — the fork's own __init__.py never imports
    # it — so without this explicit submodule import, ``_confirm_risky``'s
    # ``vanilla.dialogs.askYesNo(...)`` raises AttributeError the first time
    # Apply reaches past Safe. Guarded for the same reason as ``_vanilla``
    # above: an older or stripped vanilla missing
    # this submodule must cost the confirmation dialog, not the plugin.
    import vanilla.dialogs
except ImportError:  # pragma: no cover - vanilla is optional
    pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import azsym_binding  # noqa: E402
import azsym_geometry  # noqa: E402
import azsym_native  # noqa: E402
import azsym_scan  # noqa: E402
import azsym_sync  # noqa: E402
import azsym_topology  # noqa: E402

# The plugin is called "AZ Symmetrify" (Hugo, 2026-08-11) but this key is NOT
# renamed with it. It is where every binding on every layer is stored, in files
# already on disk: changing it would not migrate them, it would orphan them —
# every bound glyph would come back unbound and the old entries would sit in
# userData forever with nothing left that reads them. The bundle identifier
# stays put for the same reason.
KEY = "com.hugojourdan.AZSymmetry"

# Where the layer's OWN right metrics key is parked while ours is in place.
#
# A SEPARATE top-level userData key, deliberately not a field inside the KEY
# store: ``_bound_modes`` reads every top-level name in that store as the name
# of a bound symmetry, so a sibling entry there would report as a fourth mode
# and ``_release``'s ``store or None`` would never go falsy — releasing the last
# real symmetry would leave the layer looking bound forever.
METRICS_KEY_STORE = KEY + ".rightMetricsKey"

# Which bound symmetries actually EARNED the key, as a list of mode names.
#
# ``remaining`` alone cannot answer that. The key is refused on an unpaired
# binding, on an italic master and on a layer holding components, so a keyed
# mode can be bound while owning no key at all: bind a vertical mirror on an
# outline with unpaired nodes (refused), then a half-turn on the same layer
# (written), then release the half-turn — and a test on the MODE keeps "==|" on
# a layer whose only live binding was refused it. S, N and Z are rotationally
# but not mirror symmetric, so that is an ordinary thing to draw.
#
# A THIRD top-level userData key, sibling of the other two and never a field
# inside KEY, for the reason spelled out above METRICS_KEY_STORE: ``_bound_modes``
# reads every top-level name in the KEY store as a bound symmetry.
METRICS_KEY_OWNERS = KEY + ".rightMetricsKeyOwners"

# Path symmetries: a list of {"id", "paths", "modes"}, one per symmetry that
# covers only some paths. Beside KEY rather than in it, for the reason above
# and because the native walk reads KEY's keys as modes too.
SCOPES_KEY = KEY + ".paths"

MENU_TITLE = "AZ Symmetrify"

# The plugin's ONE menu item, in the Window menu.
#
# The switch row in the AZ Symmetrify palette is still the entire
# interface for BINDING; this item neither binds nor releases anything. It opens
# a window with two panes — the layers that already carry a symmetry, and a
# survey of the ones that could. A row of three switches has nowhere to say "and
# here are the other 3000 layers", and neither of those views is a state to be
# toggled, so neither can be a fourth segment.
#
# WINDOW ▸ AZ TOOLKIT, where it replaces a ``Symmetrify`` submenu of the Glyph
# menu holding one item per window (Hugo, 2026-09-10). Two reasons it moved. The
# items were commands that opened windows, and the Window menu is where macOS
# puts those; and the rest of the AZ suite installs there, so this sits among
# its siblings rather than in a place only this plugin used. It moved once more
# into the suite's own AZ Toolkit submenu, so eight plugins cost one root item.
#
# The label drops the "AZ " prefix that MENU_TITLE carries: the parent item
# already says AZ, and every sibling in the submenu is labelled the same way.
#
# NO ELLIPSIS. The convention marks an item that needs more input before it can
# act — this one opens its window immediately, and the window asks its own
# questions once it is up.
#
# The plugin's own name and not a verb: the Window menu names windows, and this
# item's job is to say which window it brings up.
WINDOW_ITEM_TITLE = "Symmetrify"

# Glyphs' node connections, mapped onto the names azsym_topology encodes.
# Imported, never hand-numbered: GSBase.h declares GSNodeConnection as
# Sharp = 0, Smooth = 100, SuperSmooth = 102, and 100/102 are exactly the pair
# nobody would guess. THREE states and not the ``GSNode.smooth`` boolean,
# because that BOOL reads True for a super-smooth node as well as a smooth one
# while its setter writes plain smooth — so a super-smooth node round-tripped
# through it silently comes back one grade down.
CONNECTION_NAMES = {
    GSSHARP: azsym_topology.SHARP,
    GSSMOOTH: azsym_topology.SMOOTH,
    GSSUPERSMOOTH: azsym_topology.SUPERSMOOTH,
}
CONNECTION_VALUES = {name: value for value, name in CONNECTION_NAMES.items()}

# THE ONE PLACE THE WORDS ARE CHOSEN, and the one place this file's naming
# inverts. Read this before "fixing" anything below that looks backwards.
#
# A mode is named for the AXIS the mirror stands on: ``VERTICAL`` mirrors across
# a vertical line, so the ink moves left to right. Hugo names a mirror for the
# direction THE INK MOVES, so the same operation is a "horizontal flip" to him.
# The two conventions are exact opposites, and both are in this repo already —
# it is why ``symmetry_vertical.pdf`` is the tile split by a VERTICAL dash while
# his own source file for it was called Sym_H (see the icon-mapping test), and
# why the Window menu takes the vertical-axis tile for what he asked for as the
# horizontal flip.
#
# The geometry keeps the axis convention, because that is what every pure module
# and every stored binding is written in and a rename would invalidate files
# users already have. Only the WORDS shown on screen change (Hugo, 2026-09-10:
# "you always invert Vertical and Horizontal symmetrify, it should be the
# opposite"). So: mode VERTICAL is labelled "Horizontal", and it is correct.
#
# Every user-visible name derives from here — the switch row, its tooltips, the
# Browse filter, the legend along the window's foot — so the inversion is stated
# once and cannot half-land in one pane.
MODE_LABELS = {
    # mirrors across a vertical axis: left ↔ right, which Hugo calls horizontal
    azsym_geometry.VERTICAL: "Horizontal",
    # mirrors across a horizontal axis: top ↔ bottom, which Hugo calls vertical
    azsym_geometry.HORIZONTAL: "Vertical",
    # A half-turn about the centre, split on a line you can angle in the
    # Diagonal field, Auto the box's corner to corner (Hugo, 2026-10-16: "The
    # third symmetry option is not Rotate 180°, but custom with a auto value
    # based on bbox"). The mode key stays ``rotate``: saved bindings use it.
    azsym_geometry.ROTATE: "Custom",
}

# Segment order, and the order axes are applied in. Deterministic so two
# composed symmetries settle the same way every frame.
#
# The ORDER is the geometry's, not the label's, and is deliberately left alone:
# it decides which axis is applied first when two are bound, so shuffling it to
# make the words read alphabetically would change what a pair of mirrors
# settles to.
MODE_ITEMS = (
    (azsym_geometry.VERTICAL, f"Mirror {MODE_LABELS[azsym_geometry.VERTICAL]}"),
    (azsym_geometry.HORIZONTAL, f"Mirror {MODE_LABELS[azsym_geometry.HORIZONTAL]}"),
    (azsym_geometry.ROTATE, MODE_LABELS[azsym_geometry.ROTATE]),
)

# The same names, keyed by mode, for prose that has to name a symmetry the
# designer did not click — "switch off Rotate 180° first". Derived rather than
# restated so a renamed mode cannot end up called two things.
MODE_TITLES = dict(MODE_ITEMS)

# Both mirrors at once, named the way the scan window names it. ONE name for
# this state across the whole plugin: the Analyze list proposes it under this
# name, the Browse pane filters on it, and the icon map keys the pair tile on
# it — so the switch row calling it something of its own would be a fourth
# spelling of a state that already has one.
PAIR_MODE = azsym_scan.combo_name((azsym_geometry.VERTICAL, azsym_geometry.HORIZONTAL))

# What the palette's switch row offers: the three symmetries, and NO pair
# (Hugo, 2026-10-10: "Remove Both Mirror button"). It carried a fourth switch
# for the {V, H} state from 2026-09-10; the two mirrors still compose — light
# both — and the Browse pane still filters on Both, which is a question about
# the file rather than a switch on the layer.
#
# A name of its own rather than MODE_ITEMS used directly, because every consumer
# of the ROW — the segment count, the click index, the hover poll — walks this,
# and putting a switch back is one entry here rather than a hunt for them.
SEGMENT_ITEMS = MODE_ITEMS

# Symmetries that fix where the ink sits LEFT TO RIGHT, and therefore imply the
# two sidebearings are equal. Both negate the x-offset about the centre — see
# ``azsym_geometry.flip`` — so both map the left edge of the ink onto the right
# one, and for the advance strip to share that symmetry its middle has to sit on
# the axis. VERTICAL is the unambiguous case; a half-turn is the same geometry
# and matches how o, O, S, N and Z are normally spaced.
#
# HORIZONTAL is deliberately absent. A top-bottom mirror constrains y and says
# nothing whatsoever about the sidebearings, so tying RSB to LSB there would
# silently re-space every E, B, C and K for no reason at all.
METRICS_KEY_MODES = (azsym_geometry.VERTICAL, azsym_geometry.ROTATE)

# "==|" on the RIGHT key means: RSB := this same layer's own LSB, held as a
# master-local exception. Read out of the shipped parser,
# ``+[GSLayer metricsKey:name:operator:value:oppositeSite:atHeight:master:]``:
# "==" routes the key to the LAYER rather than the glyph, "|" asks for the
# OPPOSITE sidebearing, and an empty glyph name means this same glyph.
#
# The doubled "=" matters even though the evaluator discards it.
# ``-[GSLayer setRightMetricsKey:]`` normalises every value to a "==" prefix,
# but its equality short-circuit compares the INCOMING string against the
# STORED normalised one — so "=|" fails that test on every write and
# re-registers undo each time, while "==|" is a true no-op on repeat. A bare
# "|" is worse still: under two characters the parser reads the whole string as
# a glyph NAME and never looks for the pipe at all.
RIGHT_METRICS_KEY = "==|"

# The palette shows them as ONE switch row, so every state is visible at once
# and two symmetries can be on together. A segmented control and not a pop-up:
# the row was built for the contextual menu, where a pop-up cannot open its own
# menu, and in a palette it is still the one control that shows every state.
#
# ------------------------------------------------------------------ the icons
#
# Hugo's own art (2026-09-10), shipped in ``Resources/icons`` and replacing the
# SF Symbols this row used to draw. Each one is the same picture the Edit view
# draws over a bound layer, at thumbnail size: a rounded tile, split by the
# DASHED AXIS, with the half the plugin generates washed back. So the switch,
# the filter, the table and the canvas all say one thing in one shape, which no
# arrow pair from the system set could do.
#
# White art on transparency, which is what makes ``setTemplate_(True)`` correct:
# a template image is inked with the control's own colour through its ALPHA, so
# the frame and the dashes come out solid and the generated half stays washed,
# in both appearances and inverted under a highlight. A fixed-colour image would
# go invisible on the light appearance, where this art is white on white.
#
# THE FILE NAMES ARE NOT THE MODE NAMES, and the swap is deliberate. Hugo names
# them for the FLIP ("Sym_V" flips top over bottom); this plugin names a mode for
# its AXIS (``VERTICAL`` mirrors across a vertical line, left over right — see
# ``azsym_geometry.axis_direction``, which gives HORIZONTAL the (1, 0) line and
# VERTICAL the (shear, 1) one). The two conventions are exact opposites, so
# Sym_H.pdf — the tile split by a VERTICAL dash — is what belongs on VERTICAL.
# Copied into the bundle under the mode's own name rather than mapped across at
# the call site: a dict reading ``VERTICAL: "Sym_H.pdf"`` is a line nobody can
# read twice without re-deriving this paragraph.
#
# The PAIR has art of its own, which the SF Symbols never did — a corner
# quarter, the region two mirrors leave you drawing. That is what retires
# ``SYMMETRY_GLYPHS``: a combination used to be two characters joined by a "+"
# because a text cell was the only thing that could hold two symbols, and one
# picture of the actual thing beats a sum every time.
_ICONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")

# Every symmetry a row, a segment or a cell can carry — the three modes and the
# one combination of them that this plugin offers. Keyed by the SAME strings
# ``azsym_scan`` names a row's symmetry with, so a lookup needs no translation.
SYMMETRY_ICON_FILES = {
    azsym_geometry.VERTICAL: "symmetry_vertical.pdf",
    azsym_geometry.HORIZONTAL: "symmetry_horizontal.pdf",
    azsym_geometry.ROTATE: "symmetry_rotate.pdf",
    azsym_scan.combo_name((azsym_geometry.VERTICAL, azsym_geometry.HORIZONTAL)): (
        "symmetry_pair.pdf"
    ),
}
# Words for a host that cannot load the art at all — a stripped or damaged
# bundle, since these files ship inside it. This used to be the fallback for a
# macOS without SF Symbols 6, which is a thing that happens; a missing bundle
# resource is not, so the words are now belt-and-braces rather than a path
# anyone is expected to take. They stay because the alternative is three blank
# segments in the plugin's only binding interface.
SEGMENT_FALLBACKS = {
    # Hugo's words, abbreviated — see MODE_LABELS for why VERTICAL reads "Horiz".
    azsym_geometry.VERTICAL: "Horiz",
    azsym_geometry.HORIZONTAL: "Vert",
    azsym_geometry.ROTATE: "180°",
}
# 21 and not 20 (Hugo, 2026-08-11): one point taller. The row carries icons
# rather than a word, and a pill sized to a line of text is a shade short for
# them.
SEGMENT_HEIGHT = 21.0
# The size the icons are DRAWN at, which is not the same question as the size of
# the segment holding them (Hugo, 2026-08-11: "icons a bit too large, or button
# too small").
#
# SQUARE, and a plain ``setSize_``, where the SF Symbols this replaced needed an
# NSImageSymbolConfiguration and a point size — a symbol carries its own aspect
# and its own stroke weight, and setting a symbol's size in points squashed a
# wide arrow into a box. The art is a 200×200 tile, so a square size preserves
# it by construction and the whole macOS-11 configuration branch goes with it.
#
# 13 inside SEGMENT_HEIGHT's 21 leaves 4 points above and below.
SEGMENT_ICON_SIZE = 13.0

# THE SWITCHES LIVE IN A PALETTE (Hugo, 2026-10-10), in the sidebar beside the
# Edit view, and no longer in its contextual menu. A palette is on screen while
# the designer draws, so the state of the layer — which symmetries are on — is
# visible without asking for it, where the menu showed it only for as long as
# it was open. See ``AZSymmetryPalette``.
#
# The title Glyphs puts on the palette's header. The plugin's spaced display
# name, as every AZ palette's header is.
PALETTE_NAME = "AZ Symmetrify"
# NSUndoManager.h; each constant's value is its own name.
UNDO_NOTIFICATIONS = (
    "NSUndoManagerDidUndoChangeNotification",
    "NSUndoManagerDidRedoChangeNotification",
)
UNDO_STEP_CLOSING = "NSUndoManagerWillCloseUndoGroupNotification"
# Where the palette sits in the sidebar, which sorts on it ascending. Read live,
# 2026-10-10: Dimensions 10, Fit Curve 20, Layers 30, Italify Groups 35, AZ Camera
# 40, Transformations 40. Hugo wants it just above Transformations; the sidebar
# put AZ Camera before Transformations on the 40 tie, and puts this after AZ Camera.
PALETTE_SORT_ID = 40
# The row's frame before the sidebar has laid it out. Only the HEIGHT is the
# palette's to decide: the width is the sidebar's, and the row follows it.
PALETTE_WIDTH = 200.0
# Air around the row inside the palette. Less above than below: the section
# header already brings its own padding (Hugo, 2026-10-10: "reduce a bit the gap
# before the palette button, and add 2px below").
PALETTE_MARGIN_X = 10.0
PALETTE_MARGIN_TOP = 2.0
PALETTE_MARGIN_BOTTOM = 8.0
# The Diagonal row under the switches (decision 8 of the guide-axis spec): the
# angle Custom splits on, typed. Small-control metrics, as the switches. Empty,
# it shows the glyph's own Auto angle (``_fill_diagonal``); "auto" only when
# there is no outline to measure.
DIAGONAL_LABEL = "Diagonal"
DIAGONAL_PLACEHOLDER = "auto"
DIAGONAL_ROW_HEIGHT = 19.0
DIAGONAL_FONT_SIZE = 11.0
DIAGONAL_LABEL_WIDTH = 52.0
DIAGONAL_FIELD_WIDTH = 56.0
DIAGONAL_DEGREE_WIDTH = 14.0
DIAGONAL_GAP = 4.0
# Between the switches and the Diagonal row.
PALETTE_ROW_GAP = 6.0
# The swap triangle (Hugo, 2026-10-21): in each lit switch, beside its icon,
# pointing at the half its symmetry has the designer draw. A click swaps it. A
# TRIANGLE_SIZE triangle in the middle of a TRIANGLE_HIT chip at partial alpha,
# so it reads as a button ("make it more obvious that users can click it").
#
# Icon, SWAP_GAP and chip are ONE image the switch centres (Hugo's mock-up,
# 2026-10-22), so the switch inks the chip as it inks the icon: white on blue
# while the window is active, grey when it is not. A lit switch splits in two
# (Hugo, 2026-10-22): its left half switches it off, and a transparent button
# over its right half, chip and all, swaps the side. Icon and chip are the same
# width, so the switch's middle falls in the gap.
TRIANGLE_SIZE = 7.0
TRIANGLE_HIT = 13.0
TRIANGLE_CHIP_ALPHA = 0.25
TRIANGLE_CHIP_RADIUS = 3.0
# The share of its ink a chip that cannot swap is drawn with.
TRIANGLE_DIMMED = 0.35
SWAP_GAP = 5.0
SWAP_GROUP = SEGMENT_ICON_SIZE + SWAP_GAP + TRIANGLE_HIT
# Each half's triangle, y up, keyed by ``azsym_geometry.side_labels``' names.
TRIANGLES = {
    "Left": ((6.5, 0.5), (0.5, 3.5), (6.5, 6.5)),
    "Right": ((0.5, 0.5), (6.5, 3.5), (0.5, 6.5)),
    "Bottom": ((0.5, 6.0), (6.5, 6.0), (3.5, 1.0)),
    "Top": ((0.5, 1.0), (6.5, 1.0), (3.5, 6.0)),
    "Bottom Left": ((0.5, 0.5), (0.5, 6.5), (6.5, 0.5)),
    "Top Right": ((6.5, 6.5), (0.5, 6.5), (6.5, 0.5)),
}
SWAP_DORMANT = "It no longer mirrors the outline: switch it off and on again to rebind it"
SWAP_UNPAIRED = "Some of its nodes have no partner, so the other half cannot take over"
# The commands AppKit sends the field's delegate for ↑/↓, and for ⇧↑/⇧↓ (probed
# on Glyphs 3532 and 4202: they arrive as str), in degrees (Hugo, 2026-10-15).
DIAGONAL_STEPS = {
    "moveUp:": 1.0,
    "moveDown:": -1.0,
    "moveUpAndModifySelection:": 10.0,
    "moveDownAndModifySelection:": -10.0,
}
# And for Esc, and for Return or Enter. Module-level with the steps: these are
# AppKit's command names, not methods of the palette (the selector check in
# test_plugin_source reads every "name:" string inside a class as one of its own).
DIAGONAL_CANCEL = "cancelOperation:"
DIAGONAL_RETURN = "insertNewline:"
# Independent on/off switches, NOT a radio group: composing a vertical with a
# horizontal mirror is the normal way to drive all four corners of a glyph from
# one, so several segments have to stay lit at once.
#
# Imported, never hand-numbered. The enum is SelectOne=0, SelectAny=1,
# Momentary=2 — the order is not the order the names are usually recited in,
# and guessing 2 here silently yields MOMENTARY, which by definition holds no
# selection: every segment would flash under the cursor and none would stay on.
SELECT_ANY = NSSegmentSwitchTrackingSelectAny
# Explicit: the automatic style is resolved from the containing window, and a
# menu is not a window it has a rule for.
ROUNDED_SEGMENTS = NSSegmentStyleRounded
# NSSegmentedControl.h: NSSegmentDistributionFillEqually = 2 (macOS 10.13). The
# segments share the palette's width rather than sitting at a fixed size in one
# corner of it, which is also what lets the hover poll find a segment by
# dividing the control's own width.
FILL_EQUALLY = 2
# NSView.h: NSViewWidthSizable = 2.
WIDTH_SIZABLE = 2
# NSView.h: MinXMargin = 1 and MaxXMargin = 4. With the width, all three stretch,
# so a resize is shared in proportion and each switch's overlay keeps its third.
THIRD_RESIZING = 1 | WIDTH_SIZABLE | 4
# NSDocument.h: NSChangeDone = 0.
NS_CHANGE_DONE = 0
# NSTrackingArea.h. Written out like the two above rather than imported, and
# each value read back from the real AppKit rather than recited.
TRACK_ENTER_EXIT = 0x01  # NSTrackingMouseEnteredAndExited
TRACK_IN_ACTIVE_APP = 0x40  # NSTrackingActiveInActiveApp
TRACK_VISIBLE_RECT = 0x200  # NSTrackingInVisibleRect

# The one blue every healthy piece of AZ Symmetrify furniture is drawn in: the
# wash over the generated half, and the axis line that bounds it. ONE triple at
# two alphas, because the line and the wash describe the same thing and used to
# say it in two unrelated colours.
#
# sRGB rather than calibrated: colorWithCalibratedRed_ is gamma 1.8 and the same
# numbers come out visibly muddier than everywhere else in the UI.
MIRRORED_BLUE = (0.35, 0.65, 1.0)

STALE_COLOR = NSColor.systemRedColor
# Bound and mirroring, but some nodes found no partner and are not being
# mirrored. Distinct from both: it is neither healthy nor a reason to re-bind.
UNPAIRED_COLOR = NSColor.systemOrangeColor
# Over a generated handle a selected node would drag but cannot move
# (``azsym_sync.stranded_handles``): a half-strength dot, radius in SCREEN
# points, over Glyphs' own hollow mark, and the sign above it. The wash and the
# axis go orange with it.
STRANDED_SIGN = "\u26a0\ufe0f"
STRANDED_DOT_RADIUS = 3.5
STRANDED_DOT_ALPHA = 0.5
# What GSNode.type reads, and nothing else in a selection does.
NODE_TYPES = ("line", "curve", "qcurve", "offcurve")


def BOUND_COLOR():
    """Axis colour for a healthy binding: the wash blue, at full strength.

    Opaque, where the wash is not. The line is thin enough that the wash's own
    0.22 would leave it close to invisible, and the two are doing different
    jobs: the wash tints a whole region, the line states one boundary.

    Only the HEALTHY state is blue. Stale stays red and unpaired stays orange,
    because the axis colour is the only thing on canvas that says a binding has
    stopped mirroring — and those two are dynamic system colours that adapt to
    the light/dark appearance, which this literal does not.
    """
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*MIRRORED_BLUE, 1.0)


# How translucent that wash is.
MIRRORED_FILL_ALPHA = 0.22


def MIRRORED_FILL():
    """Light blue wash over the half of the outline the binding GENERATES.

    Translucent so the nodes and the glyph underneath stay readable — this is a
    hint about which half is not yours to draw, not a highlight.
    """
    return NSColor.colorWithSRGBRed_green_blue_alpha_(*MIRRORED_BLUE, MIRRORED_FILL_ALPHA)



# How far the SHADING half-plane reaches, in font units. The drawn axis line is
# held to the glyph's own extent instead (axis_span); this only has to be large
# enough that the clip covers any outline, and it is never seen directly.
AXIS_REACH = 4000.0
# Dash pattern in SCREEN points (divided by the zoom, so it looks the same at
# any magnification). Dashed rather than solid because the axis is a piece of
# plugin furniture, not part of the drawing — a solid line reads as an outline.
#
# One pattern for all three modes. A half-turn divides the glyph on the same
# line a vertical mirror does, so their lines are now identical on screen: what
# tells them apart is the outline each one produces, not the furniture.
AXIS_DASH = (6.0, 4.0)
# Line width in SCREEN points, divided by the zoom exactly as AXIS_DASH is — a
# width in font units would thicken as you zoom in until the axis outweighed the
# outline it sits on. Half a point is one device pixel on a retina display: thin
# enough that the axis states a boundary without competing with the drawing,
# which is the whole reason it is dashed rather than solid.
AXIS_LINE_WIDTH = 0.5
# The ghosted proposal, in SCREEN points and divided by the zoom for the same
# reason. Twice the axis width: the axis is furniture, but the ghost is standing
# in for an OUTLINE and has to read as one against the real one beside it.
PREVIEW_LINE_WIDTH = 1.0
# How much of the canvas colour the preview strokes over the outline it would
# replace (Hugo, 2026-10-10: "dim down the current side"), and how wide, in
# SCREEN points. Every dim width below is the line it covers plus about one
# DEVICE pixel, because Glyphs draws its own outline and marks slightly ABOVE
# where the same path lands from a draw callback: -0.2 to +0.9 device pixels in
# three views measured 2026-10-10 (2x screen), varying with the view, and the
# callback's own transform does not predict it. DIM_PIXEL_NUDGE lifts the dim
# to the middle of that range. Narrower misses the line on one side; wider
# lightens the zone or metric line beside it.
# ponytail: a constant nudge for a per-view offset — leaves up to ~0.5 px.
PREVIEW_DIM_ALPHA = 0.7
PREVIEW_DIM_WIDTH = 1.25
DIM_PIXEL_NUDGE = 0.4
# The node marks Glyphs 4 draws, in SCREEN points, measured off a 2x screenshot
# (2026-10-10). They are HOLLOW — a zone shows through them — so the dim
# re-strokes each one's rim rather than filling it: the radius (half-side for a
# corner node's square) to the middle of the rim, and the rim's width plus the
# pixel above — filling showed as a pale disc in a zone. Then the handle line.
# ponytail: the Regular handle size only, and unselected marks — a selected one
# is bigger and keeps a rim. Small/Large would need measuring the same way.
DIM_SMOOTH_RADIUS, DIM_SMOOTH_WIDTH = 3.75, 1.75
DIM_CORNER_HALF, DIM_CORNER_WIDTH = 2.25, 1.5
DIM_OFFCURVE_RADIUS, DIM_OFFCURVE_WIDTH = 2.25, 1.25
DIM_HANDLE_LINE_WIDTH = 1.0
# Tools during which the plugin draws nothing. Two different reasons and one
# rule: panning is navigation, and the Text tool is for setting text rather than
# shaping outlines. In neither case is the axis or the wash any use, and in both
# they are in the way. Mirroring keeps running either way — only the furniture
# goes.
#
# Class NAMES compared as strings, never isinstance: importing a tool class
# would tie this plugin to a bundle it does not own. "GlyphsToolText" is the
# Text tool's principal class in Glyphs 4.0.1, from two independent sources —
# TextTool.glyphsTool's Info.plist declares it as NSPrincipalClass, and Glyphs'
# own SDK maps "TextTool" to it in ``toolClassAbbreviations``.
HAND_TOOL = "GlyphsToolHand"
TEXT_TOOL = "GlyphsToolText"
SUPPRESSING_TOOLS = (HAND_TOOL, TEXT_TOOL)
# A toolbar STACK hands back the group rather than the tool inside it, and a
# group's class name matches no tool at all. Glyphs' own "which tool is
# selected" accessor unwraps it exactly this way before reading the name.
TOOL_GROUP = "GSToolGroup"
# Bit 0 of NSEvent.pressedMouseButtons(), the left button. Any drag holds it
# down, which is the only thing the repaint guard needs to know.
LEFT_MOUSE_BUTTON = 1 << 0
# NSWindingRuleNonZero. Spelled out because PyObjC only exposes the enum on
# some bridge versions and a missing name here would break the draw callback.
NONZERO_WINDING = 0

# The running plugin instance, for the contextual-menu callback to reach.
#
# That callback is a CLASS method — the protocol says so — but the state it
# needs (the snapshot cache) lives on the instance. A module global rather than
# a class attribute because PyObjC classes reject attributes assigned after the
# class is built.

# Which half of the outline a BATCH keeps: the negative side — left for both
# mirrors, bottom for a horizontal one — which is also what ``_source_sign``
# falls back to with nothing selected.
#
# Held as one constant because the preview pane and the Apply button both pass
# it, and the pane's whole claim is that it drew the half applying will keep.
# It is NOT ``_source_sign``'s answer, deliberately: a selection lives on the
# LAYER and survives tab navigation, so a batch is not selection-free — one
# double-click, one click on a node to look at it, and back to the window is
# enough to leave a selection voting for the other half of a glyph nobody is
# looking at any more. The palette still asks ``_source_sign``, because there
# the designer is standing in the glyph choosing a side.
BATCH_SOURCE_SIGN = -1

# Column titles are what the designer reads; the keys are what the list sorts
# on. The two numeric columns hold FLOATS and not preformatted strings: the
# table binds its sort to the underlying value, so strings would sort 10.0
# before 9.0 and quietly mis-rank exactly the column people click to rank by.
# Symmetry is the widest because it also carries the COMBINATIONS —
# "vertical + horizontal" is twice the length of any single mode's name.
#
# NO Verdict column. It spent 110pt printing a word the bar in the last column
# already draws: a Safe row is one whose bar is nearly full. ``_row_item`` still
# carries ``band`` — the table ignores keys no column names — because the Apply
# gate reads it to decide whether to ask first.
#
# No "% of size" and no "Notes" either (Hugo, 2026-08-11). The share and the
# shift are two views of one measurement, and the row is ranked on the share
# already; "components" was a one-word note on a minority of rows.
# NO MASTER COLUMN (Hugo, 2026-09-10). A row is only proposed when every master
# is covered — see ``azsym_scan.best_per_glyph`` — so the column printed
# "All Masters" on every row of every multi-master file. ``_row_item`` still
# carries ``master`` and ``master_id``: they are half the row's identity, and
# the id is what a double-click opens.
#
# A GLYPH COLUMN AND NO GROUP HEADERS (Hugo, 2026-09-10). The headers came in
# when a glyph held one row per master; the survey now proposes at glyph level
# (``azsym_scan.best_per_glyph``), so a header would stand over exactly one row
# and double the table's height to say each name twice.
#
# "Type" and not "Symmetry" (Hugo): the column draws a tile rather than a name,
# and the tile is a kind rather than a thing. SECOND, right after the tick,
# because it is what the eye runs down — the master beside it only says which
# layer of the glyph already named above.
#
# "Outline changes" and not "Moves" (Hugo). The bar measures how far binding
# would move the outline (issue #3: the curve, not its handles); "Moves"
# reads as a count of them. It draws the HEADROOM left under the ceiling rather
# than the movement itself — see MOVE_BAR_WARNING_PER_UPM — so the identifier
# says headroom even though the column is titled for what it is about.
SCAN_COLUMNS = (
    ("Type", "icons", 64),
    ("Glyph", "glyph", 120),
    ("Outline changes", "headroom", 200),
)
# ``maxWidth`` falls back to ``width`` in List2, which pins a column at whatever
# it was born as. Every column that may stretch names a number it will never
# reach instead, so the table divides its width between them on a resize.
COLUMN_MAX = 10_000

# Six points taller than vanilla's own 17 (Hugo, 2026-08-11, 21 then 23). The
# rows carry SF Symbols now rather than words, and a symbol set at the row's text
# size fills more of its line than a lowercase letter does — at 17 the icons in
# neighbouring rows nearly touched, and the horizontal rules had nothing to
# separate.
#
# Passed explicitly rather than left to the default, because the default is the
# thing being moved away from: vanilla's List signature carries rowHeight=17.0,
# and a host whose vanilla chose differently should still give these rows the
# room the icons were sized against.
#
# Every point added here is also a point of slack inside each row, and the two
# kinds of cell divide that slack differently — which is what put the tick and
# the words on separate lines. ``AZSymmetryCenteredCell`` is what keeps them
# together, and it is load-bearing at this height rather than cosmetic.
SCAN_ROW_HEIGHT = 23.0

# The Moves column is drawn as a BAR rather than a number (Hugo, 2026-08-11).
# A number has to be read and then compared against two thresholds the designer
# has to remember; a bar that turns yellow and then red states the comparison
# itself, and a column of them can be scanned down at a glance for the one row
# that is worse than its neighbours.
#
# Continuous rather than discrete: the measurement is continuous, and segments
# would invent a precision the number does not have.
#
# The trade is that the exact figure is no longer on screen. That is the point
# of the change, but it is a real loss — the number is still what the row sorts
# on, so clicking the column still ranks by it exactly as before.
MOVE_BAR_STYLE = "continuous"

# Where the bar changes colour, as a share of the em — the same treatment every
# other absolute limit in this plugin now gets, so a 2048-upm file is not gauged
# on a 1000-upm scale. At 1000 upm these are exactly the 5 and 30 they were
# asked for, and the bar fills at MOVE_CEILING_PER_UPM's 40.
#
# 5 is not an arbitrary warning line: it is where the absolute Safe cap used to
# sit, so the bar goes yellow exactly where a snap stops being invisible.
MOVE_BAR_WARNING_PER_UPM = 0.005
MOVE_BAR_CRITICAL_PER_UPM = 0.03

# THE BAR DRAWS HEADROOM, NOT MOVEMENT (Hugo, 2026-09-10). A layer that would
# not move at all fills the column with green, and the bar shortens as the snap
# gets further — so a full row of bars reads at a glance as a page of glyphs
# worth binding, which is the question the table is there to answer.
#
# The value handed to the gauge is therefore ``ceiling - shift`` and the two
# colour thresholds are subtracted from the ceiling with it. That REVERSES them
# — the critical line ends up BELOW the warning line — which is what tells
# NSLevelIndicator to run its colours the other way, green at the full end. It
# is the same arrangement a battery or a disk-space gauge uses.
#
# ponytail: the reversal is AppKit's own behaviour for critical < warning, not
# something drawn by hand here. If a future macOS stops honouring it the bars go
# one flat colour; the LENGTH, which is the part that carries the measurement,
# is unaffected either way.

# The tick column, which has no title and holds one 14pt checkbox. 18 and not
# the 24 it shipped with (Hugo, 2026-08-11): 24 was a text column's width on a
# column that never holds text, and the six points it gives back come off the
# left edge of the glyph names — the thing the eye actually runs down.
CHECKBOX_WIDTH = 18

# The Symmetry column draws Hugo's tiles as IMAGES (2026-09-10), where it used
# to set three SF Symbols' Private Use codepoints in a text cell.
#
# The text cell existed for one reason: a combination has to show what BOTH of
# its mirrors do, and concatenating two characters with a " + " was the only way
# a table cell could hold two symbols. The new art has a tile for the pair
# itself — the corner quarter — so a combination is one picture of the region it
# leaves you drawing, and the whole join disappears with the sum it joined.
#
# 15 and not the row's 23: a tile is a solid shape where a symbol was line art,
# and one set to the full height of the row would sit against the horizontal
# rules above and below it. See SCAN_ROW_HEIGHT, which was raised to 23 to give
# the old symbols air; the tile takes its air from being smaller instead.
SCAN_ICON_SIZE = 15.0

# Said by every switch's tooltip and by Analyze when libazsym.dylib did not load.
# Pairing and measuring both run in it, so without it there is nothing to offer.
ENGINE_MISSING = "AZ Symmetrify's engine did not load"

# Said once, in one voice, by everything in the window that would otherwise act
# on whatever document happens to be in front. See ``ScanWindow._is_surveyed_font``.
SCAN_OTHER_FONT = (
    "[AZ Symmetrify] this window surveyed another font, and a row only means "
    "anything in the font it was measured in. Bring that font forward, or run "
    "Scan for Symmetry again on this one."
)

# ---------------------------------------------------------- the empty pane
#
# Switching to Analyze must NOT survey the font (Hugo, 2026-09-10). It used to,
# and the pane therefore cost seconds of a frozen application to look at. The
# pane now opens with a disabled table, one button in the middle of it, and a
# sentence saying what that button is about to do — so the survey is a press
# rather than a side effect of a toolbar click.
SCAN_BUTTON_TITLE = "Scan Font"
# The same button after a scan that came back with nothing. An unchanged "Scan
# Font" leaves "did it even run?" unanswered.
SCAN_BUTTON_EMPTY_TITLE = "Nothing to Bind — Scan Again"
SCAN_EXPLAINER = (
    "Measures every master layer in this file and lists the ones that could take "
    "a symmetry without the bind reshaping them. Nothing is written until you "
    "tick rows and press Apply."
)
SCAN_BUTTON_WIDTH = 150.0
SCAN_BUTTON_HEIGHT = 24.0
# The window's own margin and the air between stacked rows, taken from AZ
# Dynamic Anchors (Hugo, 2026-09-10) so two windows of the same suite are not
# inset by different amounts. Every edge in both panes reads these.
WINDOW_MARGIN = 14.0
WINDOW_GUTTER = 12.0
BAR_HEIGHT = 22.0
# Where the table starts with the button bar above it, and where it starts
# without one. The empty state HIDES the bar — both buttons there act on rows —
# and a hidden bar that kept its band left the table starting below nothing
# (Hugo, 2026-09-10).
LIST_TOP = WINDOW_MARGIN + BAR_HEIGHT + WINDOW_GUTTER
LIST_TOP_EMPTY = WINDOW_MARGIN
# Wide enough for three comfortable lines, narrow enough to stay a caption
# rather than a paragraph the eye has to track across the whole window.
SCAN_EXPLAINER_WIDTH = 380.0
SCAN_EXPLAINER_HEIGHT = 48.0
# Air between the button and its caption.
SCAN_NOTE_GAP = 12.0
# How far the table fades while it is disabled and unscanned. Low enough that
# the headers read as furniture rather than as content you could act on.
DIMMED_ALPHA = 0.1

# ---------------------------------------------------------------- the legend
#
# At the BOTTOM of the WINDOW and not inside either pane (Hugo, 2026-09-10), so
# it survives every switch: both panes draw the same four tiles, and a key that
# came and went with the pane would read as belonging to only one of them.
#
# SHORT names, where the switch row's tooltips use the full ones. "Mirror
# Horizontal" is 88pt of type at the system size and four of those do not fit
# the window's own minimum width — and next to the tile it labels, the axis is
# the only word doing any work.
LEGEND_ITEMS = (
    (azsym_geometry.VERTICAL, MODE_LABELS[azsym_geometry.VERTICAL]),
    (azsym_geometry.HORIZONTAL, MODE_LABELS[azsym_geometry.HORIZONTAL]),
    (PAIR_MODE, "Both"),
    (azsym_geometry.ROTATE, MODE_LABELS[azsym_geometry.ROTATE]),
)
LEGEND_HEIGHT = 30.0
LEGEND_ICON_SIZE = 14.0
# EQUAL GAPS BETWEEN ITEMS (Hugo, 2026-09-10). The row used to be four fixed
# 96pt slots, which makes the gap a leftover: "Vertical" and "Rotate 180°" are
# not the same width, so the air after each name was different and the row read
# as four ragged columns rather than as one key. Each item is now as wide as its
# own name and the gap between them is the constant.
LEGEND_ICON_GAP = 4.0
LEGEND_ITEM_GAP = 20.0
LEGEND_LABEL_HEIGHT = 16.0
# Measured off the built text field. The fallback is only for a host that will
# not answer — 6.5pt per character at the small system size, which over-measures
# slightly and so leaves the row a little airy rather than overlapping.
LEGEND_LABEL_FALLBACK_PER_CHAR = 6.5
# The tiles sit LOW against their names otherwise (Hugo, 2026-09-10). A 14pt
# tile centred in the band and a 16pt text box centred in the band do not put
# the art and the type on the same optical line: the type sits high in its box.
# Lifting the tile is what closes it. In vanilla's coordinates y runs from the
# TOP, so the lift is subtracted.
LEGEND_ICON_RISE = 2.0
# And the whole key rides higher than the middle of its own band (Hugo,
# 2026-09-10, "shift up icons and text label by 6"). 6 puts its top edge on the
# band's top edge, which leaves exactly WINDOW_MARGIN of air under the row —
# the same inset every other edge of this window has.
LEGEND_BLOCK_RISE = 6.0


# NSTableViewStyle.h: Plain = 4.
TABLE_VIEW_STYLE_PLAIN = 4


def _flatten_table(table) -> None:
    """Make a List2 render as a plain table rather than an inset one.

    Two host-decided behaviours, neither of which shows up anywhere but on a
    real screen:

    - ``NSTableViewStyleAutomatic`` resolves to INSET depending on where the
      table lives, which insets every row from the frame and turns the selection
      into a capsule floating inside the list. Plain pins it.
    - macOS 26 adds a frosted "scroll pocket" band at each scroll-view edge. On
      a table WITH column titles it lifts the header clear of the border, so the
      list reads as a row of headings above an unrelated box. See
      [[reference_macos26-scroll-pocket]].

    Both are things vanilla has no argument for, which is why this reaches past
    it at all — the focus ring and the column order, which it does have
    arguments for, are set on the table itself.

    Guarded by ``respondsToSelector_``, so this is a no-op on the older systems
    where neither API exists.
    """
    with contextlib.suppress(Exception):
        native = table.getNSTableView()
        if native.respondsToSelector_("setStyle:"):
            native.setStyle_(TABLE_VIEW_STYLE_PLAIN)
    with contextlib.suppress(Exception):
        scroller = table.getNSScrollView()
        if scroller.respondsToSelector_("setAllowedPocketEdges:"):
            scroller.setAllowedPocketEdges_(0)
        # Allowed edges alone leaves the ALWAYS-SHOWN set intact, which is the
        # half that keeps painting while a scroll is in flight.
        if scroller.respondsToSelector_("setAlwaysShownPocketEdges:"):
            scroller.setAlwaysShownPocketEdges_(0)


class ScanWindow:
    """A floating list of what each symmetry would cost each layer, and a way in.

    Plain Python and not an ObjC class: vanilla bridges its own callbacks, so
    the methods below are ordinary methods and nothing here needs a selector.

    The survey behind it is read-only. This window is NOT: ticking rows and
    pressing Apply binds them, which snaps half of each outline. Everything it
    writes goes through the same ``_bind_layer`` the palette uses, and
    every bind is undoable: one Cmd-Z per glyph takes back the artwork, the
    binding and the metrics key together.

    It acts on ONE font: the one it surveyed, held on the window. Apply, the
    preview and the survey all refuse anything else — see ``_is_surveyed_font``.

    Selecting a row previews it — the pane draws the outline as it is and as
    binding would leave it, from ``azsym_scan.proposal``, at the same
    ``BATCH_SOURCE_SIGN`` the apply path binds with.

    That is a resemblance and not an identity, which is worth being exact
    about: ``_bind_layer`` does NOT call ``proposal``. It re-derives the same
    sequence from the same primitives, because it also needs the ``Binding``
    to persist and ``proposal`` returns only the outline. The two are pinned
    against each other by a test rather than by
    construction.
    """

    def __init__(self, plugin, font, previous=None, *, container: Any, host: Any) -> None:
        self._plugin = plugin
        # The window this pane lives in, which owns everything about BEING a
        # window: opening, closing, being brought forward. This class owns what
        # is drawn inside its own group and nothing else. See
        # ``SymmetrifyWindow``.
        self._host = host
        # NO SCAN YET (Hugo, 2026-09-10). Switching to this pane used to run
        # one, which made looking at the pane cost seconds of a frozen
        # application. It now opens empty, with the survey behind a button in
        # the middle of its own table — see ``_update_empty_state``.
        #
        # None means "never scanned" and an empty list means "scanned, nothing
        # to bind". They read the same in the table and want different words on
        # the button, which is the whole reason they are two states.
        self._scan = None
        # The document this survey describes, held rather than re-read. Every
        # row names its layer by glyph name and master id, and NEITHER is
        # unique to a file — see ``_is_surveyed_font``.
        self._font = font
        # How many masters each glyph has, kept so rows re-merge the same way
        # after an Apply re-measures a layer. Without it, applying one merged
        # row would splice per-master rows back in and the glyph would silently
        # expand into three.
        self._counts: dict = {}
        self._visible: list = []
        # What the table was last handed: group rows and row dicts interleaved.
        # List2 hands these very objects back on a selection, so this is what
        # turns a selected index into the row it stands for.
        self._items: list = []
        self._rows: list = []
        # Ticked rows, by identity. See _row_key for why not by index.
        self._checked: set[tuple[str, str, str]] = set()
        if previous is not None:
            # Reopened. Carry the LIST rather than rebuild it from the survey:
            # ``_rows`` is not a pure function of the scan once anything has
            # been applied — a bound row leaves the table, and re-deriving from
            # a survey taken before the bind would put it back and offer the
            # designer work they have already done.
            #
            # The ticks come too, for the same reason they survive a reload: a
            # tick is the designer's own work, and closing a window is not a
            # decision to throw it away.
            self._scan = previous._scan
            self._counts = dict(previous._counts)
            self._rows = list(previous._rows)
            self._checked = set(previous._checked)
        # The ONE tab this window drives. Selecting a row retypes it rather than
        # opening another, or browsing a thirty-row survey would leave thirty
        # tabs behind. None until the first row is selected — the window must
        # not open a tab merely by existing.
        self._tab = None

        # A GROUP handed down by the window, not a window of its own — a vanilla
        # Group takes the same relative coordinates a window does.
        self.w = container

        # THE BUTTONS SIT ABOVE THE TABLE (Hugo, 2026-09-10). They were along
        # the bottom, which put the two that write furthest from the rows they
        # write to and left the window's foot carrying a button, a hint and a
        # summary. The foot is the legend's now, and the legend belongs to the
        # window rather than to this pane.
        # TWO BUTTONS, one at each end (Hugo, 2026-09-10). The bar carried a
        # band filter, a Symmetrise button and two lines of small type as well;
        # the filter went with the Safe / Worth a look split, the summary and
        # the double-click hint were furniture, and Symmetrise was a second verb
        # on a bar whose one verb is Apply.
        #
        # Named ``rescan`` and titled "Scan Font": the ACTION is a re-survey —
        # bind something, press it, watch the row leave — and the title is what
        # the button in the middle of the empty table says, so the same words
        # do the same thing in both places.
        self.w.rescan = vanilla.Button(
            (WINDOW_MARGIN, WINDOW_MARGIN, 100, BAR_HEIGHT),
            SCAN_BUTTON_TITLE,
            callback=self.scanClicked,
        )
        self.w.apply = vanilla.Button(
            (-(140 + WINDOW_MARGIN), WINDOW_MARGIN, 140, BAR_HEIGHT),
            "Apply Symmetry",
            callback=self.applyClicked,
        )
        self.w.list = List2(
            (WINDOW_MARGIN, LIST_TOP, -WINDOW_MARGIN, -WINDOW_MARGIN),
            [],
            columnDescriptions=self._column_descriptions(),
            doubleClickCallback=self.rowOpened,
            editCallback=self.checkboxEdited,
            selectionCallback=self.rowSelected,
            # Several rows at once, so a run of glyphs can be ticked in one
            # click — see ``_spread``. The preview follows the row the table
            # calls selected, which is the last one clicked.
            allowsMultipleSelection=True,
            allowsEmptySelection=True,
            # Rank order is the whole point of this table
            # (``azsym_scan.symmetry_rank``), and a header click could only
            # replace it with an order the survey did not compute. It also
            # retires the unhashable-dict trap the old sortable
            # ``vanilla.List`` carried.
            allowsSorting=False,
            # NO FOCUS RING (Hugo, 2026-09-10). Around a table the ring is drawn
            # on the whole scroller, which inside a pane inside a window reads
            # as a second border rather than as a hint about the keyboard.
            #
            # vanilla's own argument, not a reach past it into AppKit: List2
            # takes the flag and applies it to the view that actually draws the
            # ring, which is not the one an outsider would guess.
            drawFocusRing=False,
            # The column order carries meaning here — the tick, then what kind
            # of symmetry, then which layer, then what it would cost — and a
            # drag that rearranges it is silent and sticky. Note the spelling:
            # vanilla's argument has no "s", unlike the AppKit setter under it.
            allowColumnReordering=False,
            showColumnTitles=True,
            alternatingRowColors=True,
        )
        _flatten_table(self.w.list)

        # The empty state, assigned AFTER the table so it draws on top of it.
        # With nothing scanned there is exactly one useful action, so the Scan
        # button leaves the bar and sits in the middle of the table it is about
        # to fill.
        self.w.scan = vanilla.Button(
            (0, 0, SCAN_BUTTON_WIDTH, SCAN_BUTTON_HEIGHT),
            SCAN_BUTTON_TITLE,
            callback=self.scanClicked,
        )
        # THE DEFAULT BUTTON (Hugo, 2026-09-10), which is two fixes in one line.
        # A stock push-button bezel is TRANSLUCENT, so over the table this one
        # rendered as a ghost with the header rules showing through it; the
        # accent-filled default bezel is opaque, and blue. Return earning the
        # press is right anyway — until a survey has run, it is the only thing
        # this pane offers. Safe while the table has rows: the button is hidden
        # then, and AppKit does not fire a hidden view's key equivalent.
        with contextlib.suppress(Exception):
            self.w.scan.getNSButton().setKeyEquivalent_("\r")
        # A caption under it: the pane is otherwise a blank grid and a verb, and
        # "Scan Font" does not say what it is about to measure. NOT dimmed with
        # the table — it is the button's label, not the disabled table's
        # content.
        self.w.note = vanilla.TextBox(
            (0, 0, SCAN_EXPLAINER_WIDTH, SCAN_EXPLAINER_HEIGHT),
            SCAN_EXPLAINER,
            sizeStyle="small",
            alignment="center",
        )
        # Centred by frame and re-centred on every resize, because vanilla
        # cannot say "centred on that view" without auto layout and this pane is
        # laid out in frames throughout.
        host.w.bind("resize", self.windowResized)
        self._centre_empty_state()
        # On the WINDOW, not on this group — a group is never closed. The
        # ghost this pane draws in the Edit view outlives the window otherwise,
        # with nothing left on screen to say where it came from.
        host.w.bind("close", self.windowClosed)
        self._reload()

    # ------------------------------------------------------------- building

    @staticmethod
    def _column_descriptions() -> list:
        """One description per column, each carrying the cell class that draws it.

        A LIST2 table, so a column names a ``cellClass`` rather than carrying a
        built cell — List2 makes one view per column and recycles it down the
        rows, which is also why nothing here is shared between two columns the
        way the old cell-based table's cells had to be.

        See SCAN_COLUMNS for the order.

        ``Outline changes`` is a level-indicator bar rather than a number,
        scaled to the same ceiling that decides whether a row is listed at all.
        It draws the HEADROOM under that ceiling, so a FULL bar means "this
        would not move" and a short one means "this would move nearly as far as
        anything you will be shown". Its warning and critical lines come from
        the same em, so a 2048-upm file is not gauged on a 1000-upm scale.
        """
        upm = azsym_scan.DEFAULT_UPM
        descriptions: list[dict[str, Any]] = [
            {
                "identifier": "apply",
                "title": "",
                "width": CHECKBOX_WIDTH,
                "minWidth": CHECKBOX_WIDTH,
                "maxWidth": CHECKBOX_WIDTH,
                "cellClass": CheckBoxList2Cell,
                "editable": True,
            }
        ]
        for title, key, width in SCAN_COLUMNS:
            column: dict[str, Any] = {
                "identifier": key,
                "title": title,
                "width": width,
                "minWidth": width,
                "maxWidth": COLUMN_MAX,
                # Explicit, not inherited: a table given an editCallback (which
                # this one is, for the tick) would otherwise let a stray drag on
                # the level indicator rewrite a measurement.
                "editable": False,
            }
            if key == "icons":
                # The tile is drawn at the size ``_row_item`` set, so extra
                # column width is air either side rather than a stretched tile.
                column["cellClass"] = ImageList2Cell
                column["cellClassArguments"] = {"scale": "none"}
                # It never stretches, so it never needs to.
                column["maxWidth"] = width
            elif key == "headroom":
                ceiling = azsym_scan.MOVE_CEILING_PER_UPM * upm
                column["cellClass"] = LevelIndicatorList2Cell
                column["cellClassArguments"] = {
                    "style": MOVE_BAR_STYLE,
                    "minValue": 0.0,
                    "maxValue": ceiling,
                    # Subtracted from the ceiling along with the value, which is
                    # what puts critical BELOW warning and runs the colours from
                    # green at the full end.
                    "warningValue": ceiling - MOVE_BAR_WARNING_PER_UPM * upm,
                    "criticalValue": ceiling - MOVE_BAR_CRITICAL_PER_UPM * upm,
                }
            descriptions.append(column)
        return descriptions

    def _reload(self) -> None:
        """Put every listed row into the table.

        ONE LIST (Hugo, 2026-09-10). Safe and Worth a look used to be two
        entries in a pop-up, and picking between them meant answering "what can
        I bind without looking?" and "what should I look at?" as separate
        questions. They are one table now, in the survey's own order — Safe
        first, because that is what ``azsym_scan.sort_rows`` ranks on — and the
        bar in the last column is what says how far each one would move.
        """
        self._visible = list(self._rows)
        self._items = self._grouped_items()
        self.w.list.set(self._items)
        self._update_empty_state()

    def _grouped_items(self) -> list:
        """The visible rows as List2 items, one per row, in the survey's order.

        ONE ROW PER GLYPH now (``azsym_scan.best_per_glyph``), so there is
        nothing left to group: the items list is the visible rows and the index
        into it is the index into ``_visible``. The back-pointer stays anyway —
        see ``_row_of`` — because it costs a key and it is what made the group
        headers safe to remove without touching selection, ticking or Apply.
        """
        upm = self._scan.upm if self._scan is not None else azsym_scan.DEFAULT_UPM
        return [
            _row_item(row, _row_key(row) in self._checked, index, upm)
            for index, row in enumerate(self._visible)
        ]

    def _row_of(self, item):
        """The scan row an item stands for, or None for a group header.

        List2 hands back the very dicts ``_grouped_items`` put in, so the row is
        reached through the index they carry rather than by matching fields.
        Group rows are not dicts and answer None, which every caller treats as
        "not a row" rather than as an error.
        """
        if not isinstance(item, dict):
            return None
        index = item.get("_row")
        if not isinstance(index, int) or not 0 <= index < len(self._visible):
            return None
        return self._visible[index]

    def _selected_rows(self) -> list:
        """Every selected scan row, group headers dropped."""
        rows = []
        try:
            indexes = self.w.list.getSelectedIndexes()
        except Exception:
            print(traceback.format_exc())
            return rows
        for index in indexes or []:
            if not 0 <= index < len(self._items):
                continue
            row = self._row_of(self._items[index])
            if row is not None:
                rows.append(row)
        return rows

    def _selected_row(self):
        """The row the table calls selected, which is the last one clicked."""
        rows = self._selected_rows()
        return rows[-1] if rows else None

    # -------------------------------------------------------- the empty pane

    def scanned(self) -> bool:
        """Whether a survey has RUN. An empty ``_rows`` cannot say: a file with
        nothing to bind and a file nobody has measured are the same empty
        table and want different words on the button."""
        return self._scan is not None

    def run_scan(self) -> bool:
        """Survey the font and fill the table. False if the survey did not run.

        The one place the survey is started from. Everything else — the button
        in the middle of the empty table, the one in the bar, and the plugin's
        own entry point — comes through here, so "the survey runs when the
        designer asks for it" is a property of one method rather than a habit
        three call sites share.
        """
        if azsym_native.ERROR is not None:
            print(f"[AZ Symmetrify] {ENGINE_MISSING}: {azsym_native.ERROR}")
            return False
        if not self._is_surveyed_font():
            print(SCAN_OTHER_FONT)
            return False
        try:
            scan = self._plugin._surveyed(self._font, self._host_window())
        except Exception:
            print(traceback.format_exc())
            print("[AZ Symmetrify] the survey could not be run.")
            return False
        self._scan = scan
        self._counts = azsym_scan.master_counts(scan)
        self._rows = azsym_scan.window_rows(scan)
        self._reload()
        return True

    def run_scan_once(self) -> bool:
        """Survey only if nobody has yet. True when there are rows to show.

        THE SURVEY RUNS ONCE PER FONT (Hugo, 2026-08-11). A pane that already
        holds one shows it rather than measuring the file over: a scan is
        seconds of a frozen application, and re-running it every time the pane
        is asked for made reopening the list cost as much as producing it.
        Re-measuring is the Scan Font button, which is a deliberate press.
        """
        if self.scanned():
            return True
        return self.run_scan()

    def scanClicked(self, sender) -> None:
        """The Scan button, in the middle of the empty table and in the bar."""
        try:
            self.run_scan()
        except Exception:
            print(traceback.format_exc())

    def _host_window(self):
        """The NSWindow this pane lives in, for the scan's sheet to hang from.

        None when there is not one to be had — a survey must not fail because
        its progress bar had nowhere to sit, so ``_ScanProgress`` falls back to
        the document window and, failing that, to no sheet at all.
        """
        try:
            return self._host.w.getNSWindow()
        except Exception:
            return None

    def windowResized(self, sender) -> None:
        """Keep the empty state on the middle of the pane as the window moves."""
        try:
            self._centre_empty_state()
        except Exception:
            print(traceback.format_exc())

    def _pane_size(self) -> tuple[float, float]:
        """The pane's own width and height, or the window's opening size.

        The opening size is what it answers before the window has ever been
        laid out, which is exactly when the empty state is first placed.
        """
        try:
            size = self.w.getNSView().bounds().size
            width, height = float(size.width), float(size.height)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            print(traceback.format_exc())
        return SYMMETRIFY_WINDOW_SIZE

    def _centre_empty_state(self) -> None:
        """Put the Scan button and its caption on the middle of the pane.

        By FRAME and re-run on every resize. vanilla cannot say "centred on that
        view" without auto layout, and this pane is laid out in frames
        throughout — mixing the two inside one container is not something
        vanilla supports.
        """
        width, height = self._pane_size()
        block = SCAN_BUTTON_HEIGHT + SCAN_NOTE_GAP + SCAN_EXPLAINER_HEIGHT
        top = max(SCAN_BUTTON_HEIGHT, (height - block) / 2.0)
        with contextlib.suppress(Exception):
            self.w.scan.setPosSize(
                (
                    max(WINDOW_MARGIN, (width - SCAN_BUTTON_WIDTH) / 2.0),
                    top,
                    SCAN_BUTTON_WIDTH,
                    SCAN_BUTTON_HEIGHT,
                )
            )
        with contextlib.suppress(Exception):
            self.w.note.setPosSize(
                (
                    max(WINDOW_MARGIN, (width - SCAN_EXPLAINER_WIDTH) / 2.0),
                    top + SCAN_BUTTON_HEIGHT + SCAN_NOTE_GAP,
                    min(SCAN_EXPLAINER_WIDTH, width - WINDOW_MARGIN * 2),
                    SCAN_EXPLAINER_HEIGHT,
                )
            )

    def _update_empty_state(self) -> None:
        """Nothing to pick from: move the one useful action into the middle of
        the table and disable the table, so an empty grid does not read as a
        result you could select in.

        The bar goes with it. Both buttons there act on rows, so over an empty
        table they are two dead controls — and an Apply that is merely disabled
        reads as a failure rather than a state.
        """
        empty = not self._visible
        with contextlib.suppress(Exception):
            self.w.scan.setTitle(
                SCAN_BUTTON_EMPTY_TITLE if self.scanned() else SCAN_BUTTON_TITLE
            )
        for widget, shown in (
            (self.w.scan, empty),
            (self.w.note, empty),
            (self.w.rescan, not empty),
            (self.w.apply, not empty),
        ):
            with contextlib.suppress(Exception):
                widget.show(shown)
        with contextlib.suppress(Exception):
            self.w.list.enable(not empty)
        # And it takes the hidden bar's band with it, so the empty state is one
        # table filling the pane rather than a table hanging below a gap.
        with contextlib.suppress(Exception):
            self.w.list.setPosSize(
                (
                    WINDOW_MARGIN,
                    LIST_TOP_EMPTY if empty else LIST_TOP,
                    -WINDOW_MARGIN,
                    -WINDOW_MARGIN,
                )
            )
        # Disabled says it on click; dimmed says it at a glance. With no rows
        # the column headers ARE the table's only visible content, so they are
        # what fades.
        with contextlib.suppress(Exception):
            header = self.w.list.getNSTableView().headerView()
            header.setAlphaValue_(DIMMED_ALPHA if empty else 1.0)

    def open(self) -> None:
        """Show the window this pane lives in, on this pane."""
        self._host.open()
        self._host.set_pane(PANE_ANALYZE)

    @property
    def host(self):
        """The window this pane lives in. Never None — a pane is built into one
        or not at all, which is what the keyword-only argument enforces."""
        return self._host

    def surveys(self, font) -> bool:
        """Whether this pane's survey describes ``font``.

        By IDENTITY, for the reason ``_is_surveyed_font`` gives: a Save-As copy
        shares every glyph name and master id with the file it came from, so
        equality would answer yes about the wrong document and hand back rows
        measured somewhere else.
        """
        return self._font is not None and self._font is font

    def reveal(self) -> bool:
        """Bring this window forward, or say it cannot be.

        False once the designer has CLOSED it. vanilla drops its ``_window`` in
        ``windowWillClose_`` and ``open()`` then raises ``ValueError: can't
        re-open a window`` — its own docstring says a closed window cannot be
        reopened — so a caller that gets False has to build another one rather
        than trying again.
        """
        return self._host.reveal() and self._host.set_pane(PANE_ANALYZE)

    def _is_surveyed_font(self) -> bool:
        """Whether the font this window measured is still the frontmost one.

        This is a FLOATING window, so bringing another document forward leaves
        it open, unchanged, and still listing the first font's rows. A row names
        its layer by GLYPH NAME and MASTER ID, and a Save-As copy or a second
        open of the same file shares both — so reaching for ``Glyphs.font`` at
        Apply time does not fail loudly on the wrong document, it succeeds on
        it, and reports success.

        Compared by IDENTITY. ``is`` asks "the document I measured"; ``==``
        would go through ``-isEqual:`` and could answer "a document that looks
        like the one you measured", which is the copy this guard exists to
        refuse. PyObjC hands back one proxy per ObjC object, so repeated reads
        of ``Glyphs.font`` compare identical while the same file is in front.
        """
        return self._font is not None and Glyphs.font is self._font

    # ------------------------------------------------------------ callbacks

    def rescanClicked(self, sender) -> None:
        """Re-survey the font in place, keeping the window and the scroll.

        Rebuilt rather than reopened so the designer can bind something, press
        this, and watch the row move out of Safe into Bound without losing
        where they were in the list.
        """
        try:
            if Glyphs.font is None:
                print("[AZ Symmetrify] no font is open, so there was nothing to scan.")
                return
            if not self._is_surveyed_font():
                # Re-surveying into this window would silently replace one
                # font's rows with another's under an unchanged title, and the
                # ticks — which are keyed by glyph and master id — would land
                # on whatever the other file happens to call the same thing.
                print(SCAN_OTHER_FONT)
                return
            self._scan = self._plugin._surveyed(self._font)
            # window_rows, not report_rows: rescan has to reduce the survey the
            # same way opening it does — band filter, one symmetry per layer,
            # masters merged. Calling report_rows here put every band back and
            # expanded every glyph again, silently, on the first Rescan.
            self._rows = azsym_scan.window_rows(self._scan)
            self._counts = azsym_scan.master_counts(self._scan)
            self._reload()
        except Exception:
            print(traceback.format_exc())

    def rowOpened(self, sender) -> None:
        try:
            row = self._selected_row()
            if row is None:
                return
            _open_layer(row.glyph, row.master_id)
        except Exception:
            print(traceback.format_exc())

    def checkboxEdited(self, sender) -> None:
        """Sync the ticked set from the table after a checkbox is clicked.

        What CHANGED is worked out by comparing the table against ``_checked``
        rather than read off the click, because vanilla does not say which cell
        was edited without reaching into its internals. One changed row is a
        click; that is the only case ``_spread`` acts on.

        Every item is resolved through ``_row_of`` rather than paired with
        ``_visible`` by position. It costs nothing and it is what makes the
        table's shape the table's business: when this pane carried a group
        header per glyph, a walk that assumed one item per row paired each
        glyph's rows with the ticks from the row after its header, and bound
        glyphs nobody chose.
        """
        try:
            changed = []
            for item in self.w.list.get():
                row = self._row_of(item)
                if row is None:
                    continue
                ticked = bool(item.get("apply"))
                if ticked != (_row_key(row) in self._checked):
                    changed.append((row, ticked))
            for row, ticked in changed:
                self._tick(row, ticked)
            if len(changed) == 1:
                self._spread(*changed[0])
        except Exception:
            print(traceback.format_exc())

    def _tick(self, row, ticked: bool) -> None:
        key = _row_key(row)
        if ticked:
            self._checked.add(key)
        else:
            self._checked.discard(key)

    def _spread(self, clicked, ticked: bool) -> None:
        """Give every SELECTED row the tick just made on one of them.

        Hugo, 2026-08-11: select a run of entries, click one checkbox, and the
        whole run takes it. Otherwise ticking forty glyphs is forty clicks on a
        14pt target, which is the one part of applying in bulk that was still
        one at a time.

        Only when the clicked row is IN the selection. Clicking the checkbox of
        a row that is not selected is how the designer ticks one thing without
        disturbing a selection they have built — AppKit does not move the
        selection to a clicked checkbox, so this stays reachable.

        The other rows' items are rewritten IN PLACE and the table re-set with
        the same objects, rather than going through ``_reload``: rebuilding the
        items would drop the very selection the designer just made, so a second
        tick would spread to nothing.
        """
        selected = {_row_key(row) for row in self._selected_rows()}
        if len(selected) < 2 or _row_key(clicked) not in selected:
            return
        for item in self._items:
            row = self._row_of(item)
            if row is None or _row_key(row) not in selected:
                continue
            self._tick(row, ticked)
            item["apply"] = ticked
        with contextlib.suppress(Exception):
            self.w.list.getNSTableView().reloadData()

    def rowSelected(self, sender) -> None:
        """Navigate to the row's glyph and ghost its proposal in the Edit view.

        ``BATCH_SOURCE_SIGN`` and not a literal, and not ``_source_sign``: the
        apply path passes the same constant, so the ghost cannot show one half
        and Apply keep the other. See the constant for why a batch must not ask
        the selection at all.
        """
        try:
            row = self._selected_row()
            if row is None:
                _clear_preview()
                return
            if not self._is_surveyed_font():
                # A same-named glyph in another document would otherwise be
                # navigated to and ghosted under this row's numbers, as though
                # it were the thing that was measured.
                _clear_preview()
                print(SCAN_OTHER_FONT)
                return
            self._show_row(row)
        except Exception:
            print(traceback.format_exc())

    def windowClosed(self, sender) -> None:
        """Take the ghost off the canvas when the survey window goes.

        Without this the proposal keeps being drawn over the glyph forever,
        with nothing left on screen to explain where it came from or to turn
        it off.
        """
        try:
            _clear_preview()
        except Exception:
            print(traceback.format_exc())

    def _show_row(self, row) -> None:
        """Put ``row``'s glyph in the scan tab and arm the ghost for it."""
        global _PREVIEW
        # ``row.masters`` and not ``row.master_id``: the tab this opens holds
        # every master of the glyph, so the ghost is armed for every master the
        # row stands for. A merged "All Masters" row means all of them; an
        # ordinary row means its own, which is what it always did.
        _PREVIEW = _ScanPreview(row.glyph, row.masters, azsym_scan.combo_modes(row.symmetry))
        self._tab = _show_layer(self._tab, row.glyph, row.master_id)
        Glyphs.redraw()

    def _batch(self) -> dict[tuple[str, str], set[str]]:
        """Ticked rows grouped per layer, as a union of their modes.

        Reads ``self._rows`` and not ``self._visible``: a tick is still a tick
        after the table has been laid out again, and dropping it would silently
        apply less than the designer asked for.

        Rows already Bound are left out. Re-binding an outline the snap has
        already made symmetric changes nothing and costs an undo entry.
        """
        wanted: dict[tuple[str, str], set[str]] = {}
        for row in self._rows:
            if _row_key(row) not in self._checked:
                continue
            if row.band == azsym_scan.BAND_BOUND:
                continue
            # Every master the row stands for: an "All Masters" row is ONE
            # tick and several layers, and binding only the first would report
            # a success the designer cannot see in the other masters.
            for master_id in row.masters:
                modes = wanted.setdefault((row.glyph, master_id), set())
                modes.update(azsym_scan.combo_modes(row.symmetry))
        return wanted

    def _risky(self, batch: dict) -> int:
        """How many layers in ``batch`` are reshaped past Safe by their UNION.

        Gated on the union and not on the ticked rows one at a time, because the
        union is what gets written. ``_batch`` collects a layer's ticked modes
        and ``_apply_batch`` binds them in sequence, each snap running on the
        outline the previous one already moved — so the pair costs more than
        either half of it. MEASURED on a near-rectangle: ``vertical`` and
        ``horizontal`` each move 0.4981% of the diagonal and both band Safe,
        while their union moves 0.7044%, which is 1.41x past the Safe line.
        Banding the two singles separately therefore said Safe about a composite
        nobody had measured and the pane had never drawn.

        The union's own row is ALREADY in ``self._rows`` — the scan measures
        every combination it offers, with ``measure_combo``, simulating exactly
        this sequence — so it is looked up rather than measured again here. Two
        measurements of one thing is how a gate starts disagreeing with the
        table it is quoting.

        A union with no row of its own — ticking ``vertical`` and ``rotate``,
        which is not one of ``azsym_scan.COMBOS`` — counts as RISKY. "Nobody
        measured this" is not "this is safe", and an unmeasured composite is
        precisely what a confirmation is for.

        The index is built over ``row.masters`` and NOT over ``_row_key``,
        which carries the row's singular ``master_id``. A merged "All Masters"
        row stands for every master while naming only the first, and ``_batch``
        emits one entry per master — so a ``_row_key`` index found nothing for
        every master after the first, and each of them fell into the
        unmeasured-so-risky branch above. A perfectly Safe merged row on a
        three-master font asked to confirm two reshapes that were not going to
        happen. Each master appears in exactly one row (``best_per_layer`` gives
        one row per layer, and the merge only groups whole sets), so the
        expanded keys stay unique.
        """
        by_key = {
            (row.glyph, master_id, row.symmetry): row
            for row in self._rows
            for master_id in row.masters
        }
        risky = 0
        for (glyph_name, master_id), modes in batch.items():
            row = by_key.get((glyph_name, master_id, _union_name(modes)))
            if row is None or row.band not in (
                azsym_scan.BAND_SAFE,
                azsym_scan.BAND_BOUND,
            ):
                risky += 1
        return risky

    def applyClicked(self, sender) -> None:
        try:
            if not self._is_surveyed_font():
                # First, and before anything is counted: this is the press that
                # writes, and the ticks are worth keeping for when the right
                # document comes back forward.
                print(SCAN_OTHER_FONT)
                return
            batch = self._batch()
            if not batch:
                print("[AZ Symmetrify] nothing is ticked, so nothing was applied.")
                return
            risky = self._risky(batch)
            if risky and not _confirm_risky(risky):
                return
            applied, failed = self._plugin._apply_batch(batch)
            # ``applied``, not ``batch``: a layer that failed to bind must not
            # be relabelled Bound, or the window reports work it did not do.
            lost = self._remeasure_applied(applied)
            # ``lost`` is counted for the same reason ``failed`` is: a layer that
            # bound but could not then be re-measured has its rows DROPPED from
            # the table, and rows disappearing under a line that says only how
            # much was bound reads as the window quietly losing track of them.
            notes = []
            if failed:
                notes.append(f"{failed} layer(s) failed")
            if lost:
                notes.append(f"{lost} layer(s) could not be re-measured and left the table")
            print(
                f"[AZ Symmetrify] bound {sum(len(m) for m in applied.values())} "
                f"symmetry(ies) across {len(applied)} layer(s)"
                + ("; " + "; ".join(notes) + " — see above." if notes else ".")
            )
            if applied:
                # The bind snap moved nodes on layers that may be on screen, and
                # it writes through the NON-reporting update pair — so nothing
                # invalidates the Edit view and it keeps showing the artwork as
                # it was until some unrelated event forces a repaint. Same
                # trigger the switch row uses after a bind.
                _request_repaint()
        except Exception:
            print(traceback.format_exc())

    def _remeasure_applied(self, applied: dict) -> int:
        """Fold a press's result back into the table, and clear the ticks it used.

        Named for what it does rather than for one of its callers — it used to
        be ``_mark_bound``, which stopped being true the moment a press could
        leave a layer symmetric and unbound. Apply is its only caller since the
        Symmetrise button went; the effect is the same for any press that moves
        an outline, which is why it is not named after Apply either.

        Returns how many applied layers could not be re-measured, so the caller
        can say so: their rows are dropped, and a row disappearing under a line
        that reports only what bound reads as the window losing track of it.

        NOT a ``dataclasses.replace(row, band=BOUND)`` on just the applied
        symmetry. The original argument for skipping a re-survey — "binding
        one layer's paths cannot change what another layer's own paths would
        cost" — holds ACROSS layers, but it does not hold for an APPLIED
        layer's own sibling rows: MEASURED, binding ``vertical`` on a layer
        changes what ``horizontal`` and ``vertical + horizontal`` on that SAME
        layer would now cost, because the second symmetry is always measured
        against the outline the first one has already rewritten (see
        ``proposal``). A stale sibling row is actively dangerous here: one
        still reading Safe after the layer no longer is would apply on the
        next press with no confirmation, because ``_risky`` gates on the
        (stale) band.

        So every row belonging to an applied ``(glyph, master_id)`` is thrown
        away and rebuilt from a fresh measurement of that layer's CURRENT
        paths — see ``_remeasure_layer``. That is also how a freshly-bound
        row ends up correctly banded Bound: the fresh measurement reads the
        layer's own ``_bound_modes()``, same as ``_survey`` does.

        Rows belonging to a layer NOT in ``applied`` are left completely
        alone, same objects and all — the original argument still holds
        there, so a full font rescan remains unnecessary.

        Each layer's re-measurement is behind its own guard, the same
        principle ``_survey`` and ``_apply_batch`` both follow: one layer
        whose paths raise while being re-measured must not cost every OTHER
        applied layer its re-measurement. A layer that fails this way simply
        has nothing to put back for its key — it was already dropped from
        ``kept`` because it belongs to ``applied``, so it disappears from the
        table rather than being left with a stale pre-apply row, which is the
        same choice ``_remeasure_layer`` already makes for a layer that has
        gone missing entirely.

        Only the APPLIED layers' ticks are dropped, and not every tick in the
        window. A tick is the designer's work: a batch that bound nothing —
        every layer failing, or the whole press refused — used to clear all of
        them anyway, which costs the selection and leaves nothing to show for
        it, and makes the obvious next move (press Apply again) apply nothing.
        The ticks that ARE dropped belong to layers that have just been
        re-measured, so leaving those would point at rows that no longer exist.

        The tick pruning and ``self._reload()`` are in a ``finally`` and not
        just the last two lines. Round 1 could not raise past them — it was a
        pure comprehension — but a real measurement can, and an exception
        escaping BEFORE these run would leave applied rows still ticked and
        still showing their pre-apply band: a second press would then re-bind
        through that stale gate, which is exactly the hole re-measuring was
        written to close, reached through a different door.
        """
        changed = set(applied)
        lost = 0
        try:
            kept = [row for row in self._rows if (row.glyph, row.master_id) not in changed]
            fresh: list = []
            for glyph_name, master_id in changed:
                try:
                    fresh.extend(azsym_scan.listed(list(_remeasure_layer(glyph_name, master_id))))
                except Exception:
                    lost += 1
                    print(traceback.format_exc())
            # ``self._scan.upm`` and not the default: the move ceiling is a
            # share of the em, and re-reducing one layer against a different em
            # from the one that banded the rest would splice in a row the table
            # around it disagrees with. Rescan replaces ``_scan``, so this is
            # always the em the visible rows were measured on.
            upm = self._scan.upm if self._scan is not None else azsym_scan.DEFAULT_UPM
            self._rows = azsym_scan.window_order(kept + fresh, self._counts, upm)
        finally:
            self._checked = {key for key in self._checked if (key[0], key[1]) not in changed}
            self._reload()
        return lost


# --------------------------------------------------------------- the window

# ONE window with a toolbar, where there were two floating windows reached from
# two Glyph-menu items (Hugo, 2026-09-10, after AZ Dynamic Anchors' dashboard).
# The two things a designer wants from a whole file are "what already uses
# this?" and "what could?", and those are two views of one document rather than
# two documents — so they are two PANES of one window, picked by a toolbar, and
# the Glyph menu is empty again.
SYMMETRIFY_WINDOW_TITLE = "AZ Symmetrify"
# The DEFAULT leaves the Analyze columns some air past the 576 they declare, so
# the glyph and master names are not read at the edge of their cells and the bar
# in the last column has room to be a length rather than a dot. The grid is
# happy in anything, and gets six cells across at this width.
SYMMETRIFY_WINDOW_SIZE = (720, 520)
# WIDTH comes from the Analyze pane, which is the demanding one: wide enough
# that shrinking to the minimum still fits every column at its declared width —
# 28 (insets) + 18 (the tick column) + 384 (SCAN_COLUMNS) = 430.
#
# The tick column is IN that sum. It was not always: the 419 this held for a
# while was 18pt short of the columns it claimed to fit, and the last one
# clipped at the minimum width — the arithmetic looked right because 24pt of
# insets and a 24pt tick column happen to be the same number.
#
# The Browse grid asks for far less: one cell across.
#
# HEIGHT is 40 over what the table alone wanted, because a toolbar takes a band
# off the top of the window before either pane sees any of it, and the legend
# takes another off the bottom.
#
# WIDTH is the TABLE's again (2026-09-10). It was the button bar's while the bar
# carried a band filter, a Rescan, a Symmetrise and an Apply — 608 before
# anything overlapped. Two buttons want 264 and the columns 28 + 18 + 384 = 430,
# so the floor is the legend's, which is around 400 plus its own margins.
SYMMETRIFY_WINDOW_MIN = (620, 320)
# Position and size are remembered between openings. This is something you put
# beside the Edit view and keep there, so reopening where you left it is most of
# what makes it usable rather than a dialog you dismiss.
#
# A NEW name, not either of the two it replaces. Both of those were saved for
# windows with different minimum sizes and no toolbar, and a restored frame
# narrower than 437 would open the Analyze pane with its last column clipped.
SYMMETRIFY_WINDOW_AUTOSAVE = KEY + ".window"

PANE_BROWSE = "browse"
PANE_ANALYZE = "analyze"
# Identifier, label, SF Symbol — the identifier IS what ``set_pane`` takes, so a
# toolbar click needs no mapping. Browse first: it describes the file as it is,
# and Analyze proposes changes to it.
#
# NOT Hugo's symmetry tiles. Those name a symmetry, and neither of these does —
# a tile on the Browse item would read as "browse the vertical mirrors".
# A standard fixed pad, 16pt wide. macOS 26 starts the item row about 3pt from
# the WINDOW edge — some 11pt outside the content margin — so the row and the
# selected item's capsule hung past the table under it (Hugo, 2026-09-10, with a
# screenshot of it against AZ Dynamic Anchors, which has carried these pads for
# the same reason). One at each end pushes the row inwards.
#
# It has to be AppKit's OWN identifier and not an empty custom item: macOS 26
# gives a custom item a glass capsule of its own, which reads as a blank pill
# flanking the row, where a standard space item draws as nothing.
TOOLBAR_PAD = "NSToolbarSpaceItem"
PANES = (
    (TOOLBAR_PAD, "", ""),
    (PANE_BROWSE, "Browse", "square.grid.2x2"),
    (PANE_ANALYZE, "Analyze", "sparkle.magnifyingglass"),
    (TOOLBAR_PAD, "", ""),
)
PANE_TOOLTIPS = {
    PANE_BROWSE: "Every layer in this file that already carries a symmetry.",
    PANE_ANALYZE: "Measure the file and list the layers that could take one.",
}
# Bumped whenever the item set changes. NSToolbar restores a saved configuration
# under any identifier it has seen before, so a new pane added under the old
# name would simply not appear — the toolbar would rebuild the row it remembers.
# Bumped whenever the item set changes: NSToolbar restores a saved configuration
# under an identifier it has seen, and a restored older row would simply not
# show a new item.
SYMMETRIFY_TOOLBAR_IDENTIFIER = KEY + ".panes.2"

# The Window menu item's icon (Hugo, 2026-09-10): the horizontal mirror's own
# tile. It is the plugin's face in a menu that is otherwise a column of bare
# titles, and what picks it out of the list of open windows underneath.
#
# One of the four rather than a mark of its own, and that is a known compromise:
# the tile NAMES a symmetry, so the item wears one of the three the plugin
# offers. Hugo's pick. Point this at another symmetry if a plugin mark appears.
# THE LEFT-RIGHT FLIP (Hugo, 2026-09-10, "use Horizontal flip instead of
# vertical"). Worth writing down, because this reads backwards: the modes are
# named for the AXIS the mirror stands on, and Hugo names them for the direction
# the ink moves. A mirror about a VERTICAL axis flips left to right, which is
# the "horizontal flip" he asked for — so his horizontal is this module's
# ``VERTICAL``. It is the same inversion his tile FILE names carry; see
# SYMMETRY_ICON_FILES.
MENU_ICON_SYMMETRY = azsym_geometry.VERTICAL
# AppKit's own menu-image size, and it is NOT optional: an NSImage handed to a
# menu item is drawn at whatever size it carries, and these tiles carry their
# 200pt MediaBox. Left unset, one filled the whole menu — measured on Hugo's
# screen, a tile taller than the rest of the menu put together.
#
# Square, because the art is. The tile goes through ``_symmetry_icon`` like
# every other icon site, which sets the size unconditionally — the helper this
# replaced read ``image.size()`` first to fit by the longer side, and PyObjC
# hands that back as an NSSize STRUCT rather than a tuple, so the tuple test
# guarding the resize was False on every real Mac and the resize never ran.
# 13 and not the 16 AppKit gives a menu image (Hugo, 2026-09-10). The tiles fill
# their own square edge to edge where an SF Symbol carries its own padding, so at
# a matched box this one drew larger than every other AZ item in the menu.
MENU_ICON_BOX = 13.0


# ------------------------------------------------- the bound-layers window

# One cell: the drawing, and the glyph's name under it. NO master name (Hugo,
# 2026-08-12) — the master pop-up above the grid says which master is being
# looked at, and repeating it under all-masters is only worth a line of type
# when the two cells beside each other differ, which is the rarer case.
# The width TRACKS the height (Hugo, 2026-08-12): a cell is never narrower than
# it is tall. The pitch grows with the window but its floor is this width, so
# any narrower a cell would be a portrait slot — and a column of portrait slots
# reads as a list that has lost its second column rather than as a grid.
BOUND_CELL_SIZE = (114.4, 114.4)
BOUND_CAPTION_HEIGHT = 14.0
# Under the caption, so the next row's drawing does not sit on the name above it.
BOUND_CELL_GAP = 4.0
BOUND_GRID_PAD = 8.0
# The cell's own ground, so the grid reads as a grid rather than as glyphs
# floating in a dark pane. Drawn from ``textColor`` at this alpha rather than
# from a fixed grey: that is a dynamic colour, so the same number gives a
# barely-lighter cell in the dark appearance and a barely-darker one in light,
# and neither ever fights the ink drawn on top of it.
BOUND_CELL_TINT = 0.06
# The half the designer DRAWS, at full text colour — white in the dark
# appearance, black in the light one. Tried at 0.62, 0.3 and 0.1 on the way here
# (Hugo, 2026-08-12): grey keeps the blue half unmistakably the loud one, but it
# costs the glyph its own silhouette against a cell ground only a shade darker,
# and at thumbnail size being able to tell which glyph it is comes first.
#
# Kept as a knob rather than dropped for a plain ``textColor``, because this is
# the fourth value it has had.
BOUND_INK_TINT = 1.0
BOUND_CELL_INSET = 2.0
BOUND_CELL_RADIUS = 6.0
# The symmetry's own tile, in the cell's top-right corner — the same art the
# scan table's Symmetry column and the switch row draw, so one icon means one
# thing across the whole plugin.
#
# 11 and not the 8 the SF Symbol had. A symbol is line art and stays legible
# small; this is a picture of a divided square, and below about 10 points the
# dashed axis closes up into a solid line and the tile reads as a plain block.
BOUND_ICON_SIZE = 11.0
BOUND_ICON_MARGIN = 5.0
# How much of the cell's drawing height one em is allowed. Under 1 so a glyph
# with a tall accent or a deep descender has somewhere to go before it meets
# the caption — the cell clips, so overflow is cut rather than overlapped.
#
# A SHARE of a box that has itself grown, so this has come down twice for the
# same reason and by different amounts: 0.82, then 0.74 (a tenth off), and now
# 0.60. That last step is a tenth off the SIZE IN POINTS, not off the share —
# the cell grew 10% in the same breath, and taking a tenth off 0.74 would have
# left the glyph exactly the size it already was while the cell around it got
# bigger. The glyph is being identified here, not measured; air around it reads
# better in a grid than a cell filled to its edges.
BOUND_EM_SHARE = 0.60
# Where the baseline sits in the drawing area, as a share of it measured up
# from the bottom. Lowered from a quarter, which left the drawings sitting high
# in their cells — the descender room a quarter reserves is more than most
# glyphs use, and the space it took came off the top where it showed. Then back
# up by 4 points' worth at the current cell height, which is where it settled.
#
# A SHARE rather than the 4 points it was asked for: the drawing area is sized
# from the cell, so a fixed offset would mean something different every time the
# cell changes size, and this number has already outlived two of those.
BOUND_BASELINE_SHARE = 0.222
BOUND_NAME_SIZE = 9.0
# The name, off the cell's bottom edge. Measured from the bottom rather than
# from the drawing above it, so nudging it cannot resize the drawing.
BOUND_NAME_LIFT = 4.0
# Clear space at each side of the name, inside the cell. The truncation runs
# against THIS width and not the cell's, so a long name gives up characters
# rather than running up against the cell beside it. 12 and not the 4 it opened
# with (Hugo, 2026-08-12): at the minimum cell width that is 72 points of name,
# which shortens more names than 4 did — the trade is deliberate, a name that
# stops well inside its own cell is easier to read down a column than one that
# fills it.
BOUND_NAME_INSET = 12.0
# NSParagraphStyle.h: NSLineBreakByTruncatingMiddle = 5, and NSText.h:
# NSTextAlignmentCenter = 1, NSTextAlignmentRight = 2. Written out rather than
# imported for the reason every other constant in this file is — a name PyObjC
# does not export would take the plugin down at import — and NUMBERED CAREFULLY:
# the legacy spelling had centre at 2, which in the modern enum is right.
#
# The MIDDLE and not the tail (Hugo, 2026-08-12): a glyph name carries as much
# meaning in its suffix as in its stem — .case, .alt, comb, .sc — so eating the
# end of "Ncommaaccent" says less than eating its middle does. "Nco…ccent" keeps
# both halves of what tells one name from its neighbours.
TRUNCATE_MIDDLE = 5
TEXT_ALIGN_CENTER = 1
TEXT_ALIGN_RIGHT = 2
ALL_MASTERS = "All Masters"
# One SEGMENT and no longer a pop-up item, so it is a word rather than a phrase:
# the tiles beside it say what it is all of.
ALL_SYMMETRIES = "All"
# The switch row's own icons, at the size that row uses them. One segment for
# All, then one per BROWSE_FILTERS entry.
# One entry per segment after All: which modes a layer has to carry to match,
# which tile the segment draws, and what it is called on hover.
#
# BOTH is a FILTER and not a mode (Hugo, 2026-09-10 — "Both is missing from type
# selection in Browse"). MODE_ITEMS is the switch row's three switches and stays
# three; a layer is "both" when it carries two of them, which is a question
# about a layer rather than a fourth thing to switch on. It sits between the two
# flips it is made of, in the same order the legend along the window's foot
# lists them.
BROWSE_FILTERS = (
    (
        frozenset({azsym_geometry.VERTICAL}),
        azsym_geometry.VERTICAL,
        MODE_TITLES[azsym_geometry.VERTICAL],
    ),
    (
        frozenset({azsym_geometry.HORIZONTAL}),
        azsym_geometry.HORIZONTAL,
        MODE_TITLES[azsym_geometry.HORIZONTAL],
    ),
    (
        frozenset({azsym_geometry.VERTICAL, azsym_geometry.HORIZONTAL}),
        PAIR_MODE,
        "Both",
    ),
    (frozenset({azsym_geometry.ROTATE}), azsym_geometry.ROTATE, MODE_TITLES[azsym_geometry.ROTATE]),
)

# What the grid says when the filters match nothing (Hugo, 2026-09-10). An empty
# grid on its own reads as a window that failed to load rather than as a file
# with nothing bound in it yet.
BROWSE_EMPTY_TEXT = "No Symmetry set"
BROWSE_EMPTY_WIDTH = 240.0
BROWSE_EMPTY_HEIGHT = 17.0

BOUND_SEGMENT_ALL_WIDTH = 40.0
BOUND_SEGMENT_WIDTH = 34.0
# Right-aligned against the window's edge, so the two filters sit at opposite
# ends of the row rather than side by side — see the master pop-up, which now
# takes everything up to it.
BOUND_SEGMENT_TOTAL = BOUND_SEGMENT_ALL_WIDTH + BOUND_SEGMENT_WIDTH * len(BROWSE_FILTERS)


def _grid_columns(width: float) -> int:
    """How many cells fit across ``width``. At least one, however narrow."""
    try:
        usable = float(width) - 2 * BOUND_GRID_PAD
    except (TypeError, ValueError):
        return 1
    return max(1, int(usable // BOUND_CELL_SIZE[0]))


def _grid_pitch(width: float, columns: int) -> float:
    """How far apart the columns sit at this width.

    The columns SHARE the slack rather than leaving it in a strip down the
    right-hand side: four 96pt cells in a 474pt window packed to the left left
    90 points of empty grid beside them, which reads as a window that failed to
    fill rather than as a grid. Only the pitch grows — the drawing is scaled off
    the cell HEIGHT, so a wide window spaces the glyphs out instead of
    magnifying them.

    Shared by the drawing and the hit-testing, which is the point: a
    double-click has to land on the cell the designer can see, and two copies
    of this arithmetic would eventually disagree by a column.
    """
    if columns <= 0:
        return BOUND_CELL_SIZE[0]
    return max(BOUND_CELL_SIZE[0], (width - 2 * BOUND_GRID_PAD) / columns)


def _cell_index(point, width: float, count: int) -> int:
    """Which cell is under ``point``, or -1 for none.

    ``point`` is in the grid view's own (flipped) coordinates, which is what
    ``convertPoint:fromView:`` hands back.
    """
    columns = _grid_columns(width)
    pitch = _grid_pitch(width, columns)
    try:
        column = int((point.x - BOUND_GRID_PAD) // pitch)
        row = int((point.y - BOUND_GRID_PAD) // BOUND_CELL_SIZE[1])
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return -1
    if column < 0 or column >= columns or row < 0:
        return -1
    index = row * columns + column
    return index if 0 <= index < count else -1


def _size_width(size) -> float:
    """The width of an NSSize, or of the plain tuple that stands in for one.

    ``setFrameSize_`` is handed a struct when AppKit calls it and whatever the
    caller passed when Python does, and this file calls it itself to re-height
    the grid after the list changes.
    """
    try:
        return float(size.width)
    except AttributeError:
        try:
            return float(size[0])
        except (TypeError, IndexError, ValueError):
            return 0.0


class AZSymmetryGridView(NSView):
    """Every listed layer drawn once, with the generated half filled in blue.

    Prefixed for the reason ``AZSymmetryCenteredCell`` is: an ObjC class name is
    process-global and Glyphs loads every plugin into one runtime.

    Drawn rather than built out of controls. A GlyphView per layer would be one
    NSView per cell — hundreds of them on a file that uses the plugin properly —
    and none of them could show WHICH half the symmetry writes, which is the
    only thing this window has to say that a tab does not.

    It carries its cells in ``_cells``, assigned from ``BoundWindow``: a list of
    ``(layer, glyph name, symmetry icons, axes)``. The axes come precomputed
    because rebuilding them means parsing userData, and a scroll would do it for
    every visible cell on every frame.
    """

    def isFlipped(self) -> bool:  # noqa: N802 - AppKit selector name
        """Rows fill from the top, which is where a scrolled list starts."""
        return True

    def setFrameSize_(self, size) -> None:  # noqa: N802 - AppKit selector name
        """Take the width given, keep the height the grid actually needs.

        How many rows a width leaves is ours to work out, and every caller —
        AppKit and this file both — gets the same answer out of it.
        """
        width = _size_width(size)
        objc.super(AZSymmetryGridView, self).setFrameSize_((width, self.heightForWidth(width)))

    @objc.python_method
    def heightForWidth(self, width: float) -> float:
        """How tall the grid is at this width. Never zero — an empty scroll
        view with a zero-height document view draws nothing at all, including
        its background."""
        cells = getattr(self, "_cells", ())
        columns = _grid_columns(width)
        rows = -(-len(cells) // columns) if cells else 0
        return max(1.0, 2 * BOUND_GRID_PAD + rows * BOUND_CELL_SIZE[1])

    def drawRect_(self, rect) -> None:  # noqa: N802 - AppKit selector name
        try:
            self._draw(rect)
        except Exception:
            print(traceback.format_exc())

    def resetCursorRects(self) -> None:  # noqa: N802 - AppKit selector name
        """A pointing hand over every cell, so what can be opened looks like it.

        Per CELL and not over the whole view: the gaps between cells and the
        empty grid under the last row open nothing, and a hand there would
        promise something that is not on offer.

        Only the cells in view. AppKit asks for these whenever the geometry
        changes, and a file with three hundred bound layers would otherwise
        build three hundred rectangles for the dozen that can be pointed at.
        """
        try:
            from AppKit import NSCursor

            cells = getattr(self, "_cells", ())
            if not cells:
                return
            width = _size_width(self.frame().size)
            columns = _grid_columns(width)
            pitch = _grid_pitch(width, columns)
            height = BOUND_CELL_SIZE[1]
            seen = self.visibleRect()
            first = max(0, int((seen.origin.y - BOUND_GRID_PAD) // height))
            last = int((seen.origin.y + seen.size.height - BOUND_GRID_PAD) // height)
            cursor = NSCursor.pointingHandCursor()
            for index in range(first * columns, min(len(cells), (last + 1) * columns)):
                x = BOUND_GRID_PAD + (index % columns) * pitch
                y = BOUND_GRID_PAD + (index // columns) * height
                self.addCursorRect_cursor_(((x, y), (pitch, height)), cursor)
        except Exception:
            print(traceback.format_exc())

    def mouseDown_(self, event) -> None:  # noqa: N802 - AppKit selector name
        """Double-click a cell to go to that layer; ignore everything else.

        Handled here rather than through a click-target control per cell: there
        are no controls, only drawing, and a hundred invisible buttons would be
        a hundred views to lay out for one gesture.

        The layer goes to ``_on_open``, assigned by ``BoundWindow``. The view
        knows where the cells ARE and nothing about what opening one means.
        """
        try:
            if event.clickCount() < 2:
                return
            point = self.convertPoint_fromView_(event.locationInWindow(), None)
            cells = getattr(self, "_cells", ())
            index = _cell_index(point, _size_width(self.frame().size), len(cells))
            opener = getattr(self, "_on_open", None)
            if index >= 0 and opener is not None:
                opener(cells[index][0])
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _draw(self, rect) -> None:
        """Only the cells the dirty rect touches. A file with three hundred
        bound layers scrolls one screenful at a time, and drawing the other
        two-hundred-and-eighty would cost a ``bezierPath`` each."""
        cells = getattr(self, "_cells", ())
        if not cells:
            return
        width = _size_width(self.frame().size)
        columns = _grid_columns(width)
        pitch = _grid_pitch(width, columns)
        cell_height = BOUND_CELL_SIZE[1]
        top = rect.origin.y
        bottom = top + rect.size.height
        for index, cell in enumerate(cells):
            y = BOUND_GRID_PAD + (index // columns) * cell_height
            if y > bottom or y + cell_height < top:
                continue
            x = BOUND_GRID_PAD + (index % columns) * pitch
            _draw_grid_cell(cell, (x, y, pitch, cell_height))


def _draw_grid_cell(cell, frame) -> None:
    """One layer in one cell: its ground, the outline, the blue half, the icon
    and the name.

    Module level rather than a method so the ObjC class stays thin, and because
    the interesting part is arithmetic that has nothing to do with the view.

    The blue is the SAME fill the Edit view puts over a bound layer — see
    ``_fill_generated_halves`` — but opaque here rather than a wash. On canvas
    it tints an outline the designer can see through; at 96 points there is
    nothing underneath worth seeing, and a 22% tint over black ink would read as
    a smudge rather than as a half.
    """
    layer, glyph_name, icons, groups = cell
    x, y, width, height = frame
    art = height - BOUND_CAPTION_HEIGHT - BOUND_CELL_GAP
    context = NSGraphicsContext.currentContext()
    if context is None:
        return
    ground = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        (
            (x + BOUND_CELL_INSET, y + BOUND_CELL_INSET),
            (width - 2 * BOUND_CELL_INSET, height - 2 * BOUND_CELL_INSET),
        ),
        BOUND_CELL_RADIUS,
        BOUND_CELL_RADIUS,
    )
    NSColor.textColor().colorWithAlphaComponent_(BOUND_CELL_TINT).set()
    ground.fill()
    outline = _outline_of(layer)
    if outline is not None:
        context.saveGraphicsState()
        try:
            from AppKit import NSAffineTransform

            # Clipped to the cell, in VIEW coordinates and before the transform
            # below: a wide glyph would otherwise draw over its neighbours, and
            # a tall one over the caption.
            NSBezierPath.bezierPathWithRect_(((x, y), (width, art))).addClip()
            upm = _upm_of_layer(layer)
            scale = art * BOUND_EM_SHARE / upm
            transform = NSAffineTransform.transform()
            transform.translateXBy_yBy_(
                x + width / 2.0 - _advance_of(layer) * scale / 2.0,
                y + art * (1.0 - BOUND_BASELINE_SHARE),
            )
            # NEGATIVE y: the view is flipped so that rows fill downwards, and
            # a glyph drawn into it without this would be upside down.
            transform.scaleXBy_yBy_(scale, -scale)
            transform.concat()
            NSColor.textColor().colorWithAlphaComponent_(BOUND_INK_TINT).set()
            outline.fill()
            for group, axes in groups:
                if not axes:
                    continue
                points = [point for path in _paths_of(group) for point in path]
                extent = azsym_geometry.extent_of(points, _shear_of(group))
                drawn = outline if group is layer else _outline_of(group)
                _fill_generated_halves(drawn, axes, extent, color=BOUND_COLOR())
        except Exception:
            print(traceback.format_exc())
        finally:
            context.restoreGraphicsState()
    if icons is not None:
        _draw_cell_icon(
            icons,
            (
                x + width - BOUND_ICON_MARGIN - BOUND_ICON_SIZE,
                y + BOUND_ICON_MARGIN,
                BOUND_ICON_SIZE,
                BOUND_ICON_SIZE,
            ),
            NSColor.secondaryLabelColor(),
        )
    _draw_cell_text(
        glyph_name,
        (
            x + BOUND_NAME_INSET,
            y + height - BOUND_CAPTION_HEIGHT - BOUND_NAME_LIFT,
            width - 2 * BOUND_NAME_INSET,
            BOUND_CAPTION_HEIGHT,
        ),
        BOUND_NAME_SIZE,
        NSColor.textColor(),
        TEXT_ALIGN_CENTER,
    )


def _draw_cell_icon(image, place, color) -> None:
    """One symmetry tile in a grid cell's corner, inked in ``color``.

    TINTED BY HAND, which a template image normally gets for free. AppKit only
    applies that treatment when the image is handed to a CONTROL — a cell, a
    menu item, a segment. This grid is one custom view drawing itself, so the
    art arrives as what it literally is: white on transparency, which is
    invisible in the light appearance. Filling ``sourceAtop`` over a copy
    replaces the colour and keeps every pixel's alpha, so the frame stays solid
    and the generated half stays washed.

    A COPY, because the fill would otherwise be baked into the shared image and
    every later cell would draw in whatever colour the first one used.

    ``respectFlipped`` is not optional here. The grid view is flipped so rows
    fill downwards, and these tiles are not symmetric about their own middle —
    the horizontal mirror's wash is on the bottom half. Drawn without it, that
    tile would come out claiming the plugin generates the top.
    """
    try:
        from AppKit import (
            NSCompositingOperationSourceAtop,
            NSCompositingOperationSourceOver,
            NSRectFillUsingOperation,
        )

        x, y, width, height = place
        tinted = image.copy()
        tinted.lockFocus()
        try:
            color.set()
            NSRectFillUsingOperation(
                ((0, 0), tinted.size()), NSCompositingOperationSourceAtop
            )
        finally:
            tinted.unlockFocus()
        tinted.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
            ((x, y), (width, height)),
            ((0, 0), (0, 0)),
            NSCompositingOperationSourceOver,
            1.0,
            True,
            None,
        )
    except Exception:
        print(traceback.format_exc())


def _draw_cell_text(text: str, place, size: float, color, alignment: int) -> None:
    """One line in one rect, shortened with an ellipsis if it does not fit.

    TRUNCATED rather than clipped, and truncated in the MIDDLE (Hugo,
    2026-08-12). A glyph name is often far longer than a cell —
    "strokeshortcomb.case" beside "slashlongcomb.case" ran straight into each
    other and read as one word — and a clipped name loses its end silently, so
    two names differing only in their suffix look identical. Taking the middle
    out keeps both ends, which is where a glyph name says what it is: see
    TRUNCATE_MIDDLE.

    ``drawInRect:`` and a paragraph style, not ``drawAtPoint:``: the truncation
    and the centring are both properties of the RECT the line is drawn into, and
    there is no way to ask an attributed string to shorten itself without one.
    """
    x, y, width, height = place
    context = NSGraphicsContext.currentContext()
    if context is None:
        return
    context.saveGraphicsState()
    try:
        from AppKit import NSMutableParagraphStyle, NSParagraphStyleAttributeName

        style = NSMutableParagraphStyle.alloc().init()
        style.setAlignment_(alignment)
        style.setLineBreakMode_(TRUNCATE_MIDDLE)
        attributes = {
            NSFontAttributeName: NSFont.systemFontOfSize_(size),
            NSForegroundColorAttributeName: color,
            NSParagraphStyleAttributeName: style,
        }
        NSAttributedString.alloc().initWithString_attributes_(text, attributes).drawInRect_(
            ((x, y), (width, height))
        )
    except Exception:
        print(traceback.format_exc())
    finally:
        context.restoreGraphicsState()


def _advance_of(layer) -> float:
    """The layer's advance, or 0 when it has none worth centring on.

    The advance and not the ink's own bounds, because that is where the axis of
    a properly spaced symmetric glyph sits — so a row of them lines up on their
    axes rather than on whatever each one's overshoots do.
    """
    try:
        width = float(layer.width)
    except Exception:
        return 0.0
    return width if width > 0 else 0.0


class BoundWindow:
    """The layers that carry a symmetry, drawn in a grid rather than listed.

    Plain Python and not an ObjC class, like ``ScanWindow``: vanilla bridges its
    own callbacks.

    It writes nothing at all. Two pop-ups narrow what is shown — by master and
    by symmetry — and one button puts whatever is left into a tab, which is the
    only thing here that touches the document.

    It holds the font it was opened on, by identity, for the reason
    ``ScanWindow._is_surveyed_font`` gives at length: this is a floating window,
    a Save-As copy shares every glyph name and master id with its original, and
    reaching for ``Glyphs.font`` at button time would succeed on the wrong
    document rather than fail on it.
    """

    def __init__(
        self, plugin, font, master_id: str | None = None, *, container: Any, host: Any
    ) -> None:
        self._plugin = plugin
        # The window this pane lives in — see ``ScanWindow.__init__``.
        self._host = host
        self._font = font
        self._masters = _masters_of(font)
        self._layers: list = []

        # A GROUP handed down by the window — see ``ScanWindow.__init__``,
        # which says the same thing at length. The layout below is untouched.
        self.w = container
        # Wide, and FLEXIBLY wide: it runs from the left edge to the segmented
        # control, which at the default window is about 450 points — a bit over
        # three times the 140 it had, which is what was asked for, and enough
        # for a master called "Condensed Bold Italic" without truncation.
        #
        # A fixed 420 would have been the literal 3×, and would have run under
        # the segmented control on any window narrower than about 590 — this
        # one opens at 624 and its minimum is 330.
        self.w.master = vanilla.PopUpButton(
            (
                WINDOW_MARGIN,
                WINDOW_MARGIN,
                -(BOUND_SEGMENT_TOTAL + WINDOW_MARGIN + WINDOW_GUTTER),
                20,
            ),
            [ALL_MASTERS] + [name for _id, name in self._masters],
            callback=self.filterChanged,
        )
        # A SEGMENTED control and not a second pop-up (Hugo, 2026-08-12): four
        # claims are few enough to show at once, and shown at once they are the
        # same tiles the legend along the window's foot names — so the filter is
        # read rather than opened.
        #
        # The count that used to sit to the right of it has moved into the
        # window's TITLE. Both would not fit beside each other at the window's
        # minimum width, and the two of them overlapping there is worse than a
        # number in a title bar, which is where a document says how much of
        # itself there is anyway.
        #
        # One point above its neighbour and one point shorter, because these two
        # controls
        # cannot be made the same height and can only be centred on each other.
        # Their bezels, MEASURED on macOS 26 (Hugo, 2026-08-12), by control
        # size, in a frame tall enough not to clip them:
        #
        #             mini  small  regular  large
        #   pop-up    13.5   16.5     19.5   28.5
        #   segment   15.0   18.0     22.0   30.0
        #
        # No pair matches: the nearest is the pop-up at regular against the
        # segmented at small, which is the 1.5 too SHORT this control started
        # out. Neither does the frame help — a segmented control clamps its own
        # frame up to its natural height and draws that, whatever it is given.
        # So: regular, the 2.5 too tall, split 1.25 above and 1.25 below rather
        # than left to hang off one edge.
        #
        # The arithmetic, since the numbers on their own look arbitrary. vanilla
        # gives a regular segmented control 5 more height and drops it 4, so a
        # 19 asked for is the 24-high frame AppKit was going to clamp to anyway
        # and the bezel starts 1 inside it — which puts it at y + 0. The pop-up
        # bezel starts at 12.0 and is 19.5 tall, so its centre is 21.75, and a
        # 22-tall bezel shares that centre from 10.75. 11 is that rounded to a
        # whole point: a quarter-point high, half a pixel at 2×.
        #
        # sizeStyle is spelled out for the same reason all of this exists:
        # vanilla defaults a segmented control to "small" and a pop-up to
        # "regular", so the two of them at one declared height still differ.
        self.w.symmetry = vanilla.SegmentedButton(
            (
                -(BOUND_SEGMENT_TOTAL + WINDOW_MARGIN),
                WINDOW_MARGIN - 1,
                BOUND_SEGMENT_TOTAL,
                19,
            ),
            _symmetry_segments(),
            callback=self.filterChanged,
            selectionStyle="one",
            sizeStyle="regular",
        )
        # Explicit: a fresh segmented control has NO segment selected, which
        # reads as a filter that has not been set rather than as All.
        self.w.symmetry.set(0)
        _segment_tooltips(self.w.symmetry)

        self._grid = AZSymmetryGridView.alloc().initWithFrame_(
            ((0, 0), (SYMMETRIFY_WINDOW_SIZE[0], 1))
        )
        self._grid._cells = []
        self._grid._on_open = self.cellOpened
        # THE GRID TAKES THE WHOLE PANE (Hugo, 2026-09-10). A "Double-click a
        # cell to go to it" caption and an "Open in Tab" button used to share
        # the foot of it: the caption said once what the pointer says every time
        # it turns into a hand, and the button was a second way to reach what a
        # double-click already reaches. The band they took is the grid's now.
        self.w.scroll = vanilla.ScrollView(
            (
                WINDOW_MARGIN,
                WINDOW_MARGIN + 20 + WINDOW_GUTTER,
                -WINDOW_MARGIN,
                -WINDOW_MARGIN,
            ),
            self._grid,
            hasHorizontalScroller=False,
        )
        # Over the grid, so it is assigned after it. Centred by frame and
        # re-centred on every resize, for the reason the Analyze pane's empty
        # state is: vanilla cannot centre one view on another without auto
        # layout, and both panes are laid out in frames throughout.
        self.w.empty = vanilla.TextBox(
            (0, 0, BROWSE_EMPTY_WIDTH, BROWSE_EMPTY_HEIGHT),
            BROWSE_EMPTY_TEXT,
            alignment="center",
        )
        # No focus rings anywhere in this window (Hugo, 2026-08-12). It is a
        # floating panel of two filters and a grid, all of them reachable by
        # mouse and none of them typed into, so the ring only ever marks which
        # control was touched last — and it draws OUTSIDE the bezel, which is
        # what makes the two filters look misaligned when one of them has it.
        for control in (self.w.master, self.w.symmetry, self.w.scroll):
            _no_focus_ring(control)
        # Re-read the font whenever the designer comes back to this window.
        # It floats over the Edit view, so the obvious thing to do with it open
        # is go and bind something — and a grid that then went on showing the
        # file as it was when the window opened would be lying about the one
        # thing it is for.
        host.w.bind("became key", self.windowBecameKey)
        # Reflow when the window is dragged wider or narrower.
        #
        # NOT the grid view's own autoresizing, and not
        # ``resizeWithOldSuperviewSize:`` either — MEASURED, both of them.
        # ``NSViewWidthSizable`` resizes a view by the DELTA its superview
        # changed by, and a document view is allocated before the clip view is
        # laid out at all: the clip grew from nothing to 598 and the mask added
        # that 598 to the 624 the grid already had, giving a 1222-point grid
        # inside a 598-point window — twelve columns where six fit, with half
        # the file off the right-hand edge. The obvious repair, overriding
        # ``resizeWithOldSuperviewSize:``, never runs at all: NSClipView sizes
        # its document view itself rather than through the subview-resize path,
        # and a spy on that selector counted zero calls across two resizes.
        #
        # The window's own resize notification is neither of those things — it
        # fires after the clip view has its new bounds, which is exactly when
        # the answer is known.
        host.w.bind("resize", self.windowResized)
        self.set_master(master_id)

    # ------------------------------------------------------------- filters

    def set_master(self, master_id: str | None) -> None:
        """Point the master pop-up at ``master_id``, None meaning all of them.

        Any id this font does not have lands on All Masters. That is what makes
        the remembered choice safe to carry between documents: a master id is
        unique to the file it came from, so the one from the last file is
        simply not found in this one.
        """
        index = 0
        for position, (identifier, _name) in enumerate(self._masters):
            if master_id is not None and identifier == master_id:
                index = position + 1
                break
        try:
            self.w.master.set(index)
        except Exception:
            print(traceback.format_exc())
        self._reload()

    def _selected_master(self) -> str | None:
        """The master id the pop-up is on, or None for all of them."""
        index = _popup_index(self.w.master)
        if index <= 0 or index > len(self._masters):
            return None
        return self._masters[index - 1][0]

    def _selected_modes(self) -> frozenset | None:
        """Every mode a layer must carry to pass the filter, None for no filter.

        A SET, because one of the segments asks for two at once — see
        BROWSE_FILTERS.
        """
        index = _popup_index(self.w.symmetry)
        if index <= 0 or index > len(BROWSE_FILTERS):
            return None
        return BROWSE_FILTERS[index - 1][0]

    def filterChanged(self, sender) -> None:  # noqa: N802 - vanilla callback
        try:
            # Published back to the plugin so the next window opens where this
            # one was left. The symmetry segment is NOT remembered: it is a
            # question about what you are looking for right now, where the
            # master is which part of the file you work in.
            self._plugin._browse_master = self._selected_master()
            self._reload()
        except Exception:
            print(traceback.format_exc())

    def windowBecameKey(self, sender) -> None:  # noqa: N802 - vanilla callback
        """Whatever was bound or released while this window was behind."""
        try:
            self._reload()
        except Exception:
            print(traceback.format_exc())

    def windowResized(self, sender) -> None:  # noqa: N802 - vanilla callback
        """Re-column the grid for the new width. The CELLS are untouched — this
        is a relayout, not a reload, so dragging the window does not walk the
        font."""
        try:
            self._refit()
        except Exception:
            print(traceback.format_exc())

    def _reload(self) -> None:
        """Rebuild the grid from the font, through both pop-ups.

        Read fresh from the font rather than filtered from a list taken when
        the window opened: this window stays up while the designer works, and
        binding or releasing a symmetry in the Edit view has to show up here
        the next time they touch a filter.
        """
        master_id = self._selected_master()
        modes = self._selected_modes()
        layers = [
            layer
            for layer in _bound_layers(self._font, master_id)
            if modes is None or modes <= _all_bound_modes(layer)
        ]
        self._layers = layers
        self._grid._cells = [
            (
                layer,
                _glyph_name(layer),
                # Only while the pop-up is on All Symmetries (Hugo, 2026-08-12).
                # Narrowed to one, the pop-up has already said what every cell
                # carries, and repeating it in every corner is an icon that
                # cannot vary — which reads as decoration rather than as
                # information.
                #
                # Through the same loader the scan table and the switch row use,
                # so the icon in a cell and the icon in a survey row cannot come
                # to mean different things.
                #
                # None and not a blank image, unlike the table's: this one is
                # drawn by our own view, which simply skips it, where an
                # NSImageCell handed None crashes — see ``_symmetry_cell_image``.
                None
                if modes is not None
                else _symmetry_icon(_union_name(_all_bound_modes(layer)), BOUND_ICON_SIZE),
                # Each symmetry with what it covers: the layer, or a path symmetry.
                [
                    (group, self._plugin._axes_for_layer(group))
                    for group in (layer, *self._plugin._scoped_views(layer))
                ],
            )
            for layer in layers
        ]
        with contextlib.suppress(Exception):
            self.w.empty.show(not layers)
        self._refit()

    def _refit(self) -> None:
        """Lay the cells out again at the width they are actually seen at.

        The width comes from the CLIP view and not from the grid's own frame:
        the grid is what is being resized, so its frame is the stale number
        here, and the clip's is the fresh one.
        """
        self._grid.setFrameSize_((self._visible_width(), 0.0))
        self._grid.setNeedsDisplay_(True)
        self._centre_empty()
        # The cells have moved, so the hands over them have to be rebuilt —
        # AppKit caches cursor rects until it is told they are stale.
        try:
            self._grid.window().invalidateCursorRectsForView_(self._grid)
        except Exception:
            print(traceback.format_exc())

    def _centre_empty(self) -> None:
        """Put "No Symmetry set" on the middle of the grid's own area."""
        top = WINDOW_MARGIN + 20 + WINDOW_GUTTER
        width, height = SYMMETRIFY_WINDOW_SIZE
        with contextlib.suppress(Exception):
            size = self.w.getNSView().bounds().size
            if float(size.width) > 0 and float(size.height) > 0:
                width, height = float(size.width), float(size.height)
        with contextlib.suppress(Exception):
            self.w.empty.setPosSize(
                (
                    max(WINDOW_MARGIN, (width - BROWSE_EMPTY_WIDTH) / 2.0),
                    top + (height - top - WINDOW_MARGIN - BROWSE_EMPTY_HEIGHT) / 2.0,
                    min(BROWSE_EMPTY_WIDTH, width - WINDOW_MARGIN * 2),
                    BROWSE_EMPTY_HEIGHT,
                )
            )

    def _visible_width(self) -> float:
        """How wide the grid is actually seen, from the scroll view's clip.

        Falls back to the grid's own frame, which is the same number once the
        window has been laid out once and the only one available before that.
        """
        try:
            return _size_width(self.w.scroll.getNSScrollView().contentView().bounds().size)
        except Exception:
            return _size_width(self._grid.frame().size)

    # -------------------------------------------------------------- the tab

    def cellOpened(self, layer) -> None:
        """A double-clicked cell: go to that layer in the tab already open."""
        try:
            _reveal_in_tab(self._font, layer)
        except Exception:
            print(traceback.format_exc())

    # ------------------------------------------------------------- the window

    def open(self) -> None:
        """Show the window this pane lives in, on this pane."""
        self._host.open()
        self._host.set_pane(PANE_BROWSE)

    @property
    def host(self):
        """The window this pane lives in. Never None — a pane is built into one
        or not at all, which is what the keyword-only argument enforces."""
        return self._host

    def shows(self, font) -> bool:
        """Whether this pane lists ``font``. By identity — see the class."""
        return self._font is not None and self._font is font

    def reveal(self) -> bool:
        """Bring the window forward on this pane, or say it cannot be."""
        return self._host.reveal() and self._host.set_pane(PANE_BROWSE)


class SymmetrifyWindow:
    """The plugin's one window: a toolbar, and two panes under it.

    It owns the vanilla window and nothing else. The two panes are the classes
    that already existed as windows of their own — ``BoundWindow`` draws the
    grid, ``ScanWindow`` lists the survey — each now building into a
    ``vanilla.Group`` this hands it instead of into a window of its own. That
    split is deliberate: everything about what a pane SHOWS stays where it was,
    and everything about being a window is here, once, rather than twice.

    Plain Python and not an ObjC class, like both of them: vanilla bridges its
    own callbacks.

    ONE FONT, held by identity, for the reason both panes already held one — see
    ``ScanWindow._is_surveyed_font``. A Save-As copy shares every glyph name and
    master id with its original, so a window that reached for ``Glyphs.font``
    would act on the wrong document rather than refuse.

    THE ANALYZE PANE IS BUILT ON FIRST USE, and that is the one piece of
    behaviour worth knowing here. Surveying a file is seconds of a frozen
    application behind a progress sheet — it is why the survey has always been a
    deliberate command — so opening this window does not run one. Clicking
    Analyze does, which is the same deliberate press the old menu item was.
    Browse costs a walk of the open layers and is built immediately.
    """

    def __init__(self, plugin, font, pane: str = PANE_BROWSE, previous=None) -> None:
        self._plugin = plugin
        self._font = font
        self._pane = PANE_BROWSE
        # The ScanWindow this window's Analyze pane replaces, when the designer
        # closed the last window and asked for another. Carried rather than
        # re-derived: see ``ScanWindow.__init__`` for why rows measured before a
        # bind cannot simply be measured again.
        self._previous_scan = previous

        # AN ORDINARY WINDOW, not a floating panel (Hugo, 2026-09-10). Both
        # windows this replaces floated, and a panel does sit beside the Edit
        # view rather than behind it — but a panel also gets AppKit's UTILITY
        # frame: a short title bar, small close buttons, and a toolbar drawn at
        # the compact size with its icons shrunk to match. Next to AZ Dynamic
        # Anchors the two toolbars did not look like the same suite. The frame
        # is not a thing this window can style around; it comes with the class.
        self.w = vanilla.Window(
            SYMMETRIFY_WINDOW_SIZE,
            SYMMETRIFY_WINDOW_TITLE,
            minSize=SYMMETRIFY_WINDOW_MIN,
            autosaveName=SYMMETRIFY_WINDOW_AUTOSAVE,
        )
        # Both panes fill the window whole. A toolbar is part of the window's
        # frame rather than of its content, so neither pane has to leave a band
        # for it — which is the difference between this and the tab-bar-shaped
        # thing it would be if the row were drawn inside the content view.
        # Both panes stop short of the legend's band, which is the window's own
        # and belongs to neither of them.
        self.w.browse = vanilla.Group((0, 0, 0, -LEGEND_HEIGHT))
        self.w.analyze = vanilla.Group((0, 0, 0, -LEGEND_HEIGHT))
        self._build_legend()

        self.browse = BoundWindow(
            plugin,
            font,
            getattr(plugin, "_browse_master", None),
            container=self.w.browse,
            host=self,
        )
        # Built by ``_build_analyze`` on the first switch to it. None means "not
        # yet", never "failed" — a survey that cannot be run leaves this None
        # and the window on the pane it was already showing.
        self.analyze = None

        self.w.bind("resize", self.legendResized)
        self._add_toolbar()
        self.set_pane(pane)

    # ------------------------------------------------------------ the panes

    def set_pane(self, pane: str) -> bool:
        """Show one pane and hide the other. False if it could not be shown.

        A refusal is not an error: asking for Analyze runs the survey, and a
        file with no font behind it or a survey that raised leaves the window
        exactly where it was rather than on an empty pane claiming to be a
        table.
        """
        if pane == PANE_ANALYZE and self.analyze is None and not self._build_analyze():
            return False
        self._pane = pane
        for name, group in ((PANE_BROWSE, self.w.browse), (PANE_ANALYZE, self.w.analyze)):
            try:
                group.show(name == pane)
            except Exception:
                print(traceback.format_exc())
        self._sync_toolbar()
        return True

    def host(self):
        """This window. Named so a pane and the window it is in answer the same
        question the same way — see ``ScanWindow.host``."""
        return self

    def pane(self) -> str:
        """Which pane is showing. Read by the tests and by the menu, so that
        asking for the pane already up is a no-op rather than a rebuild."""
        return self._pane

    def _build_analyze(self) -> bool:
        """Put the Analyze pane in its group. False if it could not be built.

        IT DOES NOT SURVEY (Hugo, 2026-09-10). The pane arrives empty, with a
        disabled table and the survey behind a button in the middle of it, so
        switching to Analyze costs a switch rather than seconds of a frozen
        application. Everything the survey needs is reached from the pane
        itself — see ``ScanWindow.run_scan``.

        A host without List2 has no pane to build: the table's GROUP ROWS are
        what carry the glyph names over their layers, and a flat table is not a
        narrower version of this pane. So it falls through to the printed
        report, which is where the whole feature falls back anyway.
        """
        if List2 is None:  # pragma: no cover - older vanilla
            print("[AZ Symmetrify] this vanilla has no List2, so Analyze cannot be shown.")
            self._plugin._without_a_window(self._font, PANE_ANALYZE)
            return False
        try:
            self.analyze = ScanWindow(
                self._plugin,
                self._font,
                self._previous_scan,
                container=self.w.analyze,
                host=self,
            )
        except Exception:
            print(traceback.format_exc())
            print("[AZ Symmetrify] the Analyze pane could not be built.")
            return False
        # The rows carried over have been carried; a later rebuild starts from
        # what this pane holds rather than from a window two generations back.
        self._previous_scan = None
        self._plugin._scan_window = self.analyze
        return True

    # ----------------------------------------------------------- the legend

    def _build_legend(self) -> None:
        """A key to the four tiles, along the foot of the window.

        ON THE WINDOW and not in either pane (Hugo, 2026-09-10). Both panes draw
        the same tiles — the Analyze table in its Type column, the Browse grid in
        each cell's corner — so a key that came and went with the pane would read
        as belonging to only one of them.

        Its items are laid out in FRAMES and re-centred on resize, for the reason
        the Analyze pane's empty state is: vanilla cannot centre one view on
        another without auto layout, and mixing the two inside one container is
        not something it supports.

        Guarded whole. A window with no key to its icons is a window; a window
        that failed to build is not.
        """
        try:
            self.w.legend = vanilla.Group((0, -LEGEND_HEIGHT, 0, LEGEND_HEIGHT))
            for index, (symmetry, label) in enumerate(LEGEND_ITEMS):
                image = vanilla.ImageView((0, 0, LEGEND_ICON_SIZE, LEGEND_ICON_SIZE))
                # Through the same loader every other icon site uses, so the key
                # and the thing it is a key to cannot come to differ.
                tile = _symmetry_icon(symmetry, LEGEND_ICON_SIZE)
                if tile is not None:
                    image.setImage(imageObject=tile)
                setattr(self.w.legend, f"icon{index}", image)
                setattr(
                    self.w.legend,
                    f"label{index}",
                    vanilla.TextBox((0, 0, 0, 16), label, sizeStyle="small"),
                )
            self._centre_legend()
        except Exception:
            print(traceback.format_exc())

    def _label_width(self, index: int, label: str) -> float:
        """How wide item ``index``'s name actually draws.

        Asked of the built text field rather than guessed: an item is as wide as
        its own name now, so a guess would put the constant gap between the
        wrong edges. ``fittingSize`` answers before the field has ever been in a
        window, which is when this first runs.
        """
        try:
            width = float(
                getattr(self.w.legend, f"label{index}").getNSTextField().fittingSize().width
            )
            if width > 0:
                return width
        except Exception:
            pass
        return len(label) * LEGEND_LABEL_FALLBACK_PER_CHAR

    def _centre_legend(self) -> None:
        """Put the key's items on the middle of the window's foot.

        Each item is a tile, a fixed gap, and its own name; the items are then
        separated by one constant gap and the whole row is centred as a block.
        See LEGEND_ITEM_GAP for why the slots are not fixed, and LEGEND_ICON_RISE
        for why the tile does not sit on the name's own centre line.
        """
        width = SYMMETRIFY_WINDOW_SIZE[0]
        with contextlib.suppress(Exception):
            size = self.w.getNSWindow().contentView().bounds().size
            if float(size.width) > 0:
                width = float(size.width)
        labels = [self._label_width(index, label) for index, (_, label) in enumerate(LEGEND_ITEMS)]
        items = [LEGEND_ICON_SIZE + LEGEND_ICON_GAP + label for label in labels]
        block = sum(items) + LEGEND_ITEM_GAP * (len(items) - 1)
        x = max(WINDOW_MARGIN, (width - block) / 2.0)
        top = (LEGEND_HEIGHT - LEGEND_ICON_SIZE) / 2.0 - LEGEND_BLOCK_RISE
        label_top = (LEGEND_HEIGHT - LEGEND_LABEL_HEIGHT) / 2.0 - LEGEND_BLOCK_RISE
        for index, label in enumerate(labels):
            with contextlib.suppress(Exception):
                getattr(self.w.legend, f"icon{index}").setPosSize(
                    (x, top - LEGEND_ICON_RISE, LEGEND_ICON_SIZE, LEGEND_ICON_SIZE)
                )
            with contextlib.suppress(Exception):
                getattr(self.w.legend, f"label{index}").setPosSize(
                    (
                        x + LEGEND_ICON_SIZE + LEGEND_ICON_GAP,
                        label_top,
                        label,
                        LEGEND_LABEL_HEIGHT,
                    )
                )
            x += items[index] + LEGEND_ITEM_GAP

    def legendResized(self, sender) -> None:
        """Keep the key centred as the window is dragged wider or narrower."""
        try:
            self._centre_legend()
        except Exception:
            print(traceback.format_exc())

    # ---------------------------------------------------------- the toolbar

    def _add_toolbar(self) -> None:
        """The panes as a real window toolbar, as AZ Dynamic Anchors does it.

        ``expanded``: the panes get their own full-width row under the title.
        ``preference`` is the only style that centres the window title, but it
        centres the pane row with it and resets the toolbar to icon-only.

        Customisation is OFF. This toolbar picks which pane is showing, and a
        user who drags a pane out of it loses the only way back to that pane;
        with customising off there is nothing to autosave either.

        Guarded whole. A window without its pane picker still shows Browse,
        which is what it opens on — so a toolbar that will not build must not
        take the window with it.
        """
        items = []
        for pane, label, symbol in PANES:
            # AppKit's own items are asked for by identifier alone — building an
            # NSToolbarItem for one would replace the space with an empty custom
            # item, which macOS 26 draws as a capsule.
            if pane.startswith("NS"):
                items.append({"itemIdentifier": pane})
                continue
            item: dict[str, Any] = {
                "itemIdentifier": pane,
                "label": label,
                "toolTip": PANE_TOOLTIPS.get(pane, ""),
                "selectable": True,
                "callback": self._paneClicked,
            }
            image = _toolbar_symbol(symbol)
            if image is not None:
                item["imageObject"] = image
                # Inked by the toolbar rather than drawn as it is, so the row
                # follows the window's own appearance and its selected item.
                item["imageTemplate"] = True
            items.append(item)
        try:
            self.w.addToolbar(
                toolbarIdentifier=SYMMETRIFY_TOOLBAR_IDENTIFIER,
                toolbarItems=items,
                addStandardItems=False,
                displayMode="iconLabel",
                toolbarStyle="expanded",
            )
        except Exception:
            print(traceback.format_exc())
            return
        with contextlib.suppress(Exception):
            toolbar = self.w.getNSWindow().toolbar()
            toolbar.setAllowsUserCustomization_(False)
            toolbar.setAutosavesConfiguration_(False)

    def _paneClicked(self, sender) -> None:
        """A toolbar item click. The sender is the NSToolbarItem, which carries
        the identifier that IS the pane name."""
        try:
            self.set_pane(str(sender.itemIdentifier()))
        except Exception:
            print(traceback.format_exc())

    def _sync_toolbar(self) -> None:
        """Move the toolbar's highlight to the pane actually showing.

        Needed because the pane can change without a click: the window opens on
        one, and the Window menu can ask for either. Guarded whole — a window
        whose toolbar failed to build has nothing to highlight, and that is not
        a reason to refuse the pane.
        """
        with contextlib.suppress(Exception):
            self.w.getNSWindow().toolbar().setSelectedItemIdentifier_(self._pane)

    # ----------------------------------------------------------- the window

    def open(self) -> None:
        self.w.open()
        # After the window exists: a toolbar has no selected item to set until
        # there is a window under it, so the sync in ``__init__`` ran against
        # nothing.
        self._sync_toolbar()

    def shows(self, font) -> bool:
        """Whether this window is the one for ``font``. By identity — see the
        class, and ``ScanWindow._is_surveyed_font`` for the reason at length."""
        return self._font is not None and self._font is font

    def reveal(self) -> bool:
        """Bring it forward, or say it cannot be.

        False once the designer has CLOSED it: vanilla drops its ``_window`` in
        ``windowWillClose_`` and ``open()`` then raises ``ValueError: can't
        re-open a window``, so a caller that gets False has to build another.
        """
        try:
            native = self.w.getNSWindow()
            if native is None:
                return False
            native.makeKeyAndOrderFront_(None)
            return True
        except Exception:
            print(traceback.format_exc())
            return False


def _toolbar_symbol(name: str):
    """An SF Symbol for a toolbar item, or None on a host without it.

    None costs the item its icon and leaves its label, which is a toolbar that
    reads as words rather than one that fails to build. Unlike the symmetry
    tiles — which ship with the plugin and are therefore always there — these
    two are the system's, and ``imageWithSystemSymbolName:`` is macOS 11: on
    anything older it is a missing SELECTOR that raises rather than a name that
    answers None, which is why the guard is around the call and not a test of
    the result.
    """
    try:
        return NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    except Exception:
        return None


def _symmetry_segments() -> list:
    """One segment per filter: All, then each of BROWSE_FILTERS as an icon.

    The icons come from ``_segment_image``, the same function the contextual
    menu's switch row uses — including its word fallback for a host without SF
    Symbols, which is why the title is only set when there is no image. A
    segment carrying both would show the word beside the icon.
    """
    segments = [{"title": ALL_SYMMETRIES, "width": BOUND_SEGMENT_ALL_WIDTH}]
    for _modes, mode, title in BROWSE_FILTERS:
        # Annotated: a segment holds a width, and then either an image or a
        # title, and inferring the type from the width alone makes it a dict of
        # floats that neither of the others can be put in.
        segment: dict[str, Any] = {"width": BOUND_SEGMENT_WIDTH}
        image = _segment_image(mode, title)
        if image is not None:
            segment["imageObject"] = image
        else:
            segment["title"] = SEGMENT_FALLBACKS.get(mode, title)
        segments.append(segment)
    return segments


def _no_focus_ring(control) -> None:
    """Stop a vanilla control drawing the blue ring when it has focus.

    Through ``_nsObject`` because there is no vanilla wrapper for this, and
    guarded whole: a control that keeps its ring is a control that looks wrong,
    not a window that fails to open.
    """
    try:
        # NSFocusRingTypeNone. Written out rather than imported: the name lives
        # in AppKit for the enum's sake and the value is what the setter takes.
        control._nsObject.setFocusRingType_(1)
    except Exception:
        print(traceback.format_exc())


def _segment_tooltips(control) -> None:
    """Name each segment on hover, because all but the first are only icons.

    Reached through vanilla's own accessor and guarded whole: a host whose
    vanilla spells it differently loses the tooltips, not the filter.
    """
    try:
        cell = control.getNSSegmentedButton().cell()
        cell.setToolTip_forSegment_("Every symmetry", 0)
        for index, (_modes, _mode, title) in enumerate(BROWSE_FILTERS, start=1):
            cell.setToolTip_forSegment_(title, index)
    except Exception:
        print(traceback.format_exc())


def _popup_index(control) -> int:
    """Which entry a vanilla control is on, as an int. -1 when it cannot be
    read, which every caller treats as "all of them" — and which is also what a
    segmented control answers with nothing selected."""
    try:
        return int(control.get())
    except (TypeError, ValueError, AttributeError):
        return -1


def _masters_of(font) -> list[tuple[str, str]]:
    """Every master as ``(id, name)``, in the font's own order."""
    masters: list[tuple[str, str]] = []
    try:
        for master in font.masters:
            masters.append((str(master.id), str(master.name)))
    except Exception:
        print(traceback.format_exc())
    return masters


def _glyph_name(layer) -> str:
    """What the designer calls this layer's glyph, or "?" if it cannot say."""
    try:
        return str(layer.parent.name)
    except Exception:
        return "?"


def _reveal_in_tab(font, layer) -> bool:
    """Show ``layer`` in the tab the designer is already looking at.

    THE CURRENT TAB and not a new one (Hugo, 2026-08-12). This window floats
    over the Edit view and a grid is something you click through — a tab per
    double-click would leave twenty behind before the designer found the glyph
    they were after.

    If that tab ALREADY holds the layer — most likely a list some other command
    button opened — the cursor moves to it and the tab is otherwise left alone.
    Retyping there would replace the very list being worked through with the one
    glyph just clicked, which is the opposite of navigating it. Matched on the
    glyph name and the layer id rather than by identity: a tab hands back its
    own layer objects, and a proxy that compares unequal would silently fall
    through to retyping.

    Falls back to opening a tab when there is none open at all, because "show it
    in the current tab" has to mean something in a window with no tabs.
    """
    if font is None or layer is None:
        return False
    glyph_name = _glyph_name(layer)
    master_id = _master_id(layer)
    if glyph_name == "?":
        return False
    try:
        tab = font.currentTab
    except Exception:
        print(traceback.format_exc())
        tab = None
    if tab is not None and _place_cursor(tab, glyph_name, master_id):
        _select_master(font, master_id)
        return True
    try:
        if tab is not None:
            tab.text = "/" + glyph_name
        else:
            tab = font.newTab("/" + glyph_name)
    except Exception:
        print(traceback.format_exc())
        return False
    _select_master(font, master_id)
    return True


def _place_cursor(tab, glyph_name: str, master_id: str) -> bool:
    """Put the tab's cursor on this layer if the tab is holding it already.

    False means "not in this tab", which is the caller's cue to retype it.
    Guarded whole: ``tab.layers`` is a proxy, and the one thing this must not do
    is turn a tab that cannot be walked into a click that appears to do nothing
    — the caller still has the retype to fall back on.
    """
    try:
        for index, candidate in enumerate(tab.layers):
            if _glyph_name(candidate) == glyph_name and _master_id(candidate) == master_id:
                tab.textCursor = index
                return True
    except Exception:
        print(traceback.format_exc())
    return False


def _open_layers_tab(font, layers) -> bool:
    """Put these layers, in this order, into a new tab.

    Through ``tab.layers`` rather than ``newTab("/a/b/c")``. A text tab names
    GLYPHS, and the master it shows is whichever one the font is pointed at —
    so it cannot express a list drawn from several masters at all, and would
    quietly show the current master's layer for a glyph bound only in another.

    A NEW tab every time: this is a list to keep open and work through, not a
    preview to be retyped under the designer as they click.
    """
    if font is None or not layers:
        return False
    try:
        tab = font.newTab()
        tab.layers = list(layers)
    except Exception:
        print(traceback.format_exc())
        return False
    print(f"[AZ Symmetrify] {len(layers)} layer(s) using Symmetrify, in a new tab.")
    return True


def _confirm_risky(layers: int) -> bool:
    """Ask once before applying anything outside Safe.

    Counts LAYERS and not rows, because the thing measured against Safe is the
    union each layer will be bound with — see ``ScanWindow._risky``.

    What it says about undo is what undo actually does: ``_apply_batch`` opens
    each glyph's undo group before ``_bind_layer``, and the snap, the binding
    in ``layer.userData`` and the metrics key all land in it (the store probed
    as an undo step on Glyphs 3532, 2026-10-20).

    ``askYesNo`` rather than a vanilla Sheet: a Sheet's ``.close()`` raises a
    TypeError in Glyphs 4, and it is catchable, so it leaks instead of crashing
    and would survive exactly the manual test that should have caught it.
    """
    try:
        return bool(
            vanilla.dialogs.askYesNo(
                messageText="Apply symmetries that will reshape outlines?",
                informativeText=(
                    f"{layers} layer(s) land outside Safe once their ticked "
                    "symmetries are applied together, and applying them will "
                    "visibly move nodes. One Cmd-Z per glyph brings that "
                    "glyph's outline back and switches its symmetry off."
                ),
            )
        )
    except Exception:
        print(traceback.format_exc())
        return False


def _confirm_symmetrise(layers: int) -> bool:
    """Ask once before settling anything outside Safe.

    A separate message from ``_confirm_risky`` and not a flag on it, because the
    two presses leave the file in genuinely different states and the difference
    is the whole point of the button.

    What it says about undo is, again, what undo actually does — and here the
    story is simply shorter. Symmetrise writes the outline and NOTHING else: no
    binding in ``layer.userData``, no metrics key. So one Cmd-Z per glyph really
    does put that glyph back exactly as it was, with no symmetry left switched
    on behind it and nothing to release. That is the one promise
    ``_confirm_risky`` cannot make.

    Counts LAYERS and not rows, for the same reason: what is measured against
    Safe is the union each layer will be settled with.
    """
    try:
        return bool(
            vanilla.dialogs.askYesNo(
                messageText="Symmetrise outlines that will visibly move?",
                informativeText=(
                    f"{layers} layer(s) land outside Safe once their ticked "
                    "symmetries are settled together. Both halves meet in the "
                    "middle, so each node moves about half as far as binding "
                    "would move it — but on these layers that is still visible. "
                    "Nothing is bound: one Cmd-Z per glyph puts that glyph back."
                ),
            )
        )
    except Exception:
        print(traceback.format_exc())
        return False


def _item_key(item) -> tuple[str, str, str]:
    """A table item's identity, matching ``_row_key`` field for field.

    The MASTER ID and not the master's name. Two masters may carry the same
    name — ``Row`` and ``_select_master`` both say so — and keying on the name
    made two such rows indistinguishable to every lookup in this file: the
    clicked row resolved to whichever came first, so the preview drew the wrong
    master, and ``_selected_rows`` reported both as selected, so ``_spread``
    gave the second one the tick made on the first. Apply then bound a layer
    the designer never ticked, which is the exact failure ``_row_key`` exists
    to prevent, reached through the view instead of through an index.

    ``str()`` on every field: these come back from AppKit as NSString proxies,
    which do not compare equal to a Python str under ``==`` in every direction.
    """
    return (str(item["glyph"]), str(item["master_id"]), str(item["symmetry"]))


def _row_key(row) -> tuple[str, str, str]:
    """What identifies a row across a rebuild of the table.

    NOT the list index. ``_reload`` rebuilds the table from scratch on every
    rescan, so an index-keyed tick would quietly re-attach to whatever row
    landed in its place — and the first the designer would know is Apply
    binding a glyph they never ticked.

    The same three fields ``_item_key`` reads back off the table, so a Row and
    the item built from it resolve to each other. They were two different keys
    once — this one on the master's id, that one on its name — and the pair of
    them agreed on every font where no two masters shared a name.
    """
    return (row.glyph, row.master_id, row.symmetry)


def _union_name(modes) -> str:
    """A layer's ticked modes as the symmetry name a row would carry.

    Built in ``azsym_geometry.MODES`` order, which is both the order
    ``_apply_batch`` binds them in and the order ``azsym_scan.COMBOS`` names
    them in — so the string lines up with the row that measured that exact
    sequence. Not ``COMBO_JOIN.join(modes)``: ``modes`` is a set, Python
    randomises string hashing per process, and a name built from its own
    iteration order would match the table under one hash seed and miss it under
    the next.

    One mode gives that mode's own name back, which is what a single row is
    called.
    """
    return azsym_scan.combo_name(tuple(mode for mode in azsym_geometry.MODES if mode in modes))


def _symmetry_cell_image(symmetry: str, size: float):
    """A row's symmetry as ONE image, never None.

    Never None is the whole reason this is not just ``_symmetry_icon``. An
    NSImageCell handed None does not draw a blank cell — it takes the whole
    application down, and this plugin lives in a font editor where a crash
    costs unsaved drawing. A blank tile is the safe shape of "the art is
    missing", so a symmetry with no file gets an empty image of the right size
    rather than a hole in the table.

    That is not a theoretical worry here: this bundle is symlinked out of
    Dropbox, which renames files out from under a running app.
    """
    image = _symmetry_icon(symmetry, size)
    if image is not None:
        return image
    try:
        return NSImage.alloc().initWithSize_((size, size))
    except Exception:
        print(traceback.format_exc())
        return None


def _headroom(shift: float) -> float:
    """How much room a shift leaves under the move ceiling, never below zero.

    Clamped rather than trusted: rows past the ceiling are filtered out before
    the table ever sees them, but a negative value handed to a level indicator
    draws as a full bar — which would say "this does not move" about the one
    row that moves furthest.
    """
    ceiling = azsym_scan.MOVE_CEILING_PER_UPM * azsym_scan.DEFAULT_UPM
    return max(0.0, ceiling - shift)


def _row_item(row, ticked: bool, index: int, upm: float) -> dict:
    """One scan row as the table's column identifiers.

    ``headroom`` is the one thing here that is not simply a field copied across.
    It is the room left under the move ceiling — ``ceiling - shift`` — because
    the bar draws how little this would move rather than how much, so a layer
    that would not move fills the column. See MOVE_BAR_WARNING_PER_UPM.

    The shift is NORMALISED to a 1000-upm scale before the subtraction. A List2
    column names its cell class and that class's arguments ONCE, when the table
    is built — and the table is built before any font has been surveyed, so the
    bar's ceiling cannot be the font's own em the way it was when a cell object
    was made per scan. Scaling the VALUE instead puts a 2048-upm file back on
    the same bar: the row still sorts and reports in its own units, and only
    the number handed to the gauge is converted.

    ``_row`` is the row's index in ``_visible`` and is what turns a selected
    item back into a row. List2 hands back the very dicts this builds, so a
    back-pointer is enough and nothing has to be matched by field.

    ``symmetry`` and ``master_id`` ride along even though no column shows
    either: together with the glyph they ARE the row's identity, and the tick
    set is keyed on all three. The table ignores identifiers no column names.

    ``master_id`` rides alongside the master's NAME rather than replacing it:
    the name is what a row would be described by in prose, and the id is what
    tells two same-named masters apart. See ``_item_key``.
    """
    return {
        "apply": ticked,
        "_row": index,
        "glyph": row.glyph,
        "master": row.master,
        "master_id": row.master_id,
        "symmetry": row.symmetry,
        "icons": _symmetry_cell_image(row.symmetry, SCAN_ICON_SIZE),
        "headroom": _headroom(row.shift * azsym_scan.DEFAULT_UPM / upm if upm else row.shift),
        "band": row.band,
    }


# What the Edit view is ghosting, or None when nothing is selected.
#
# Module level because the drawing happens in the plugin's own draw callback
# while the selection happens in the scan window, and the two objects share
# nothing else. The window clears it on deselect and on close — a ghost that
# outlived its window would be drawn over the glyph forever with nothing left on
# screen to say where it came from.
_PREVIEW = None


# The shortest gap between two rebuilds of the ghost WHILE A DRAG IS RUNNING.
# ``proposal()`` is quadratic in the node count — measured at 12.9 ms for a
# 128-node contour and 26 ms for a pair of mirrors, against a 16.7 ms frame —
# and the ghost's cache is keyed on node positions, so a drag invalidated it on
# every single frame. 20 rebuilds a second is enough for the ghost to be seen
# following the shape, and leaves the frame budget to the drag itself.
PREVIEW_REBUILD_INTERVAL = 1.0 / 20.0

def _repeating_timer(interval: float, block):
    """A repeating timer that keeps firing while a MENU is open, or None.

    ``scheduledTimerWithTimeInterval:`` adds to the default run-loop mode only,
    and a menu runs its own modal event loop in ``NSEventTrackingRunLoopMode`` —
    so a timer scheduled the usual way stops dead the moment any menu appears
    and starts again when it closes. Added to the COMMON modes instead,
    which is the set the tracking mode belongs to.

    That matters for the hover preview, which lives entirely inside a menu
    tracking session and would never tick at all.

    Guarded: a host that will not schedule the timer loses the animation, not
    the feature that asked for it.
    """
    try:
        timer = NSTimer.timerWithTimeInterval_repeats_block_(interval, True, block)
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)
        return timer
    except Exception:
        print(traceback.format_exc())
        return None


# How often the switch row is asked which segment the cursor is over. 30 fps:
# any slower and the ghost lags the cursor across the three icons, which reads
# as it previewing the wrong one.
HOVER_POLL_FRAME = 1.0 / 30.0

# The poll driving that, and what ``_PREVIEW`` held before the hover borrowed
# it. A scan-window row can be selected while the cursor crosses the palette,
# and its ghost is their work — the hover gives it back on the way out.
_HOVER_TIMER = None
_HOVER_RESTORE = None
_HOVER_SEGMENT = -1


def _segment_under_cursor(control, count: int) -> int:
    """Which switch the cursor is over, or -1 for none.

    Arithmetic on a mouse position, not something AppKit is asked for. The
    segments FILL the control equally (``FILL_EQUALLY``), so the one under the
    cursor is the cursor's x over an equal share of the control's own width —
    whatever width the sidebar has given it. The mouse position is available
    from ``NSEvent`` at any time.

    Screen coordinates down to the control's own, and the y band is checked as
    well as the x: a cursor in the palette's margin above or below the row must
    preview nothing rather than the nearest icon.
    """
    try:
        window = control.window()
        if window is None or count <= 0:
            return -1
        local = window.convertPointFromScreen_(NSEvent.mouseLocation())
        point = control.convertPoint_fromView_(local, None)
        bounds = control.bounds()
        width = float(bounds.size.width)
        if width <= 0.0 or not 0.0 <= point.y <= bounds.size.height:
            return -1
        if not 0.0 <= point.x < width:
            return -1
        return int(point.x // (width / count))
    except Exception:
        return -1


def _stop_hover() -> None:
    """Take the poll down and hand the scan window's ghost back.

    Restores rather than clears: a row selected in the scan window is the
    designer's own work, and a cursor crossing the palette that quietly threw
    it away would be the hover taking something it was only borrowing.
    """
    global _HOVER_TIMER, _HOVER_RESTORE, _HOVER_SEGMENT, _PREVIEW
    if _HOVER_TIMER is None and _HOVER_RESTORE is None:
        # Nothing borrowed, so nothing to give back. Without this the tidy-up
        # call at the top of ``_watch_segment_hover`` restored a saved ghost of
        # None over whatever the scan window had armed — opening the contextual
        # menu wiped a selected row's preview before this ever saved it.
        return
    if _HOVER_TIMER is not None:
        try:
            _HOVER_TIMER.invalidate()
        except Exception:
            print(traceback.format_exc())
    _HOVER_TIMER = None
    _HOVER_SEGMENT = -1
    _PREVIEW = _HOVER_RESTORE
    _HOVER_RESTORE = None
    Glyphs.redraw()


def _watch_segment_hover(plugin_instance, container, layer, specs) -> None:
    """Ghost the symmetry the cursor is resting on, while it rests on the row.

    STARTED AND STOPPED by the palette's tracking area (``mouseEntered_`` and
    ``mouseExited_``), so it runs only while the cursor is on the row and costs
    nothing the rest of the time. The area answers WHEN; this poll answers
    WHICH segment, with the arithmetic in ``_segment_under_cursor`` — the poll
    this was built as inside the contextual menu, kept rather than re-derived
    from mouse-moved events.

    The poll still takes itself down when ``container`` loses its window — the
    palette's document closing under a cursor that never left — because an exit
    event is not promised from a view that has gone.

    Guarded end to end, and the guard matters more here than anywhere else in
    this file: the switch row is the ONLY way to bind, and a preview that raised
    would cost the designer the feature itself rather than a flourish on top.
    """
    global _HOVER_TIMER, _HOVER_RESTORE, _HOVER_SEGMENT, _PREVIEW
    try:
        _stop_hover()
        glyph = str(layer.parent.name)
        master_id = _master_id(layer)
        modes = [mode for _label, _bound, mode in specs]
        _HOVER_RESTORE = _PREVIEW
        target = _acting(layer)
        scope = target.scope if isinstance(target, _ScopedLayer) else None

        def tick(timer) -> None:
            global _HOVER_SEGMENT, _PREVIEW
            try:
                if container.window() is None:
                    _stop_hover()
                    return
                index = _segment_under_cursor(container, len(modes))
                if index == _HOVER_SEGMENT:
                    return
                _HOVER_SEGMENT = index
                if index < 0 or not _previewable(modes[index], layer):
                    _PREVIEW = _HOVER_RESTORE
                else:
                    # combo_modes, so hovering the pair ghosts BOTH mirrors —
                    # the state that switch would leave — rather than the first
                    # of them. One tuple for a plain mode.
                    hovered = azsym_scan.combo_modes(modes[index])
                    # About the selected guide, as the click would bind. A guide
                    # the switch cannot use has already been refused by
                    # ``_previewable``, so this is a point or None.
                    guide = _guide_axis(target, modes[index])
                    _PREVIEW = _ScanPreview(
                        glyph,
                        master_id,
                        hovered,
                        # The side the CLICK would keep, asked per mode exactly
                        # as ``_bind_layer`` asks it — so the ghost, its wash and
                        # the switch all agree on which half is copied.
                        tuple(plugin_instance._source_sign(target, mode) for mode in hovered),
                        centre=guide if isinstance(guide, tuple) else None,
                        # The typed angle's split, with or without a guide
                        # (decision 8): ``proposal`` previews it either way.
                        split=_typed_split(target, modes[index]),
                        scope=scope,
                    )
                Glyphs.redraw()
            except Exception:
                print(traceback.format_exc())
                _stop_hover()

        _HOVER_TIMER = _repeating_timer(HOVER_POLL_FRAME, tick)
        if _HOVER_TIMER is None:
            _HOVER_RESTORE = None
    except Exception:
        print(traceback.format_exc())
        _HOVER_TIMER = None
        _HOVER_RESTORE = None


def _previewable(mode: str, layer) -> bool:
    """Whether hovering this switch should ghost anything at all.

    Only a switch whose click would BIND — off and clickable (Hugo, 2026-10-10:
    "When an option have been disabled, don't show preview"). A greyed switch
    cannot be clicked, so a ghost over it previews something that will not
    happen; a lit one RELEASES on click, which leaves the outline exactly as it
    is, so a ghost there is a picture of no change at all. Read from the layer
    rather than the control, because the layer is what a click acts on.
    """
    try:
        if _PLUGIN is not None and not isinstance(layer, _ScopedLayer):
            layer = _PLUGIN._target_of(layer)
            if isinstance(layer, str):
                return False
        bound = _bound_modes(layer)
        return (
            mode not in bound
            and _segment_enabled(mode, bound, bool(_shear_of(layer)))
            and not _impure(_PLUGIN, layer, mode)
        )
    except Exception:
        return False


def _drop_hover_ghost() -> None:
    """Take the hover's ghost down after a click, until the cursor moves on.

    The poll only reconsiders when the cursor reaches a DIFFERENT switch, so
    without this the ghost armed before a click outlived it: switch a mirror off
    and the outline it no longer keeps went on being drawn over the glyph, which
    reads as the symmetry still being on. ``_HOVER_SEGMENT`` is left on the
    clicked switch on purpose — moving to another and back is how the designer
    asks to see a preview again.
    """
    global _PREVIEW
    if _HOVER_TIMER is None:
        return
    _PREVIEW = _HOVER_RESTORE
    Glyphs.redraw()


def _clear_preview() -> None:
    global _PREVIEW
    if _PREVIEW is None:
        return
    _PREVIEW = None
    Glyphs.redraw()


class _ScanPreview:
    """The proposal being ghosted over one layer, cached against its outline.

    ``proposal()`` re-derives a pairing, which is quadratic, and the draw
    callback runs on EVERY frame of a drag — so the result is cached and rebuilt
    only when the layer's own coordinates change. That is the same gate the
    bound path keeps for ``classify``/``reconcile``, and for the same reason:
    without it, previewing a row would make dragging a node in that glyph
    re-solve the whole symmetry sixty times a second.

    Positions are the right cache key here, unlike the bound path's
    structure-only signature. A ghost has to follow the shape as it is dragged,
    so a moved node MUST invalidate it.
    """

    def __init__(
        self,
        glyph: str,
        master_id: str | tuple[str, ...],
        modes: tuple,
        sign: int | tuple[int, ...] = BATCH_SOURCE_SIGN,
        centre: tuple[float, float] | None = None,
        split: tuple[float, float] | None = None,
        ghost: bool = True,
        scope: list[int] | None = None,
    ) -> None:
        self.glyph = glyph
        # The path indices of the path symmetry this previews; None is the
        # whole layer. ``_draw_preview`` draws it through a view of them.
        self.scope = scope
        # ONE id or SEVERAL (Hugo, 2026-09-10). A scan row arms every master it
        # stands for, because the tab a click opens now holds every master of
        # the glyph — and a ghost on the first layer only, with the other two
        # sitting there bare, reads as the preview being broken rather than as
        # the row being about one master.
        #
        # The palette's hover still passes a single string: there the designer
        # is standing in one layer and that layer is the subject.
        if isinstance(master_id, str):
            self.master_ids: tuple[str, ...] = (master_id,)
        else:
            self.master_ids = tuple(str(one) for one in master_id)
        # The row's OWN master, which is where the cursor lands and what the
        # numbers in the row were measured on. First of the set, never empty.
        self.master_id = self.master_ids[0] if self.master_ids else ""
        self.modes = modes
        # Which half survives, ONE PER MODE, in the outline this draws AND in
        # the wash over it. The scan window's rows keep BATCH_SOURCE_SIGN for
        # every mode, because that is what its Apply does. The palette's hover
        # passes the SELECTION's side for each mode instead — there the designer
        # is standing in the glyph with nodes selected, and that selection is
        # the choice of side, so a ghost drawn the other way round would show
        # the opposite of what clicking would do (Hugo, 2026-10-10).
        #
        # Per mode because the pair asks twice: nodes in the bottom left keep the
        # left for one mirror and the bottom for the other, and a single sign
        # gave the second mirror the first one's answer.
        self.signs: tuple[int, ...] = (
            (sign,) * len(modes) if isinstance(sign, int) else tuple(sign)
        )
        # A selected guide's centre (``_guide_axis``) when the palette's hover
        # ghosts a bind about it; None everywhere else. Read once, here: the
        # guide cannot move while the cursor is on the palette, and the hover
        # builds a new preview each time it reaches a switch.
        self.centre = centre
        # False for the Diagonal field's preview of an angle that cannot bind:
        # its line and wash are drawn over the outline as it is, with no ghost.
        self.ghost = ghost
        # And the typed angle's split for the half-turn (``_typed_split``,
        # decision 8),
        # so the ghost keeps the half the click would keep.
        self.split = split
        # KEYED BY MASTER, all four of them: the same glyph is a different
        # shape in every master, so one cache shared between them would rebuild
        # on every frame that drew a different layer — which is every frame, in
        # a tab holding the whole set.
        self._paths: dict[str, Any] = {}
        self._bezier: dict[str, Any] = {}
        self._centre: dict[str, Any] = {}
        self._shear: dict[str, float] = {}
        # The proposal's own points, for the axis line and the box its wash splits.
        self._proposed: dict[str, Any] = {}
        # When each master's ghost was last re-solved, for the drag throttle.
        # Per master as well: a shared clock would let the layer being dragged
        # spend the whole budget and leave the others frozen.
        self._rebuilt: dict[str, float] = {}

    @objc.python_method
    def _due(self, master_id: str) -> bool:
        """Whether this master's ghost may be re-solved now. See
        ``_preview_due``."""
        return _preview_due(self, master_id)

    @objc.python_method
    def matches(self, layer) -> bool:
        """Whether ``layer`` is the one this preview was armed for.

        Compared by glyph name and MASTER id, not by object identity: the draw
        callback is handed whichever layer is on screen, and the layer object
        for a glyph is not guaranteed to be the same one the window resolved.

        ANY of the armed masters, not just the row's own — see ``__init__``.
        """
        try:
            return (
                str(layer.parent.name) == self.glyph
                and str(layer.associatedMasterId) in self.master_ids
            )
        except Exception:
            return False

    @objc.python_method
    def outline(self, layer) -> tuple | None:
        """``(bezier, centre, shear, paths, current)`` for this layer, or None.

        Recomputed only when the layer's coordinates have changed.

        ``paths`` is the PROPOSAL's coordinates, handed back because the
        caller spans its axis line and sizes the half-turn's corner-to-corner
        split from them — the box the layer will have once bound. Returning
        them is what stops ``_draw_preview`` walking ``layer.paths`` a second
        time on every frame it draws.

        ``current`` is the layer's own outline as read for the cache key, with
        its node types — what the dim of the replaced side marks — so that
        needs no walk of its own either.
        """
        master_id = _master_id(layer)
        current = _structure_of(layer)
        paths = current.paths
        if not paths:
            return None
        if paths != self._paths.get(master_id) and self._due(master_id):
            self._rebuilt[master_id] = time.time()
            self._paths[master_id] = paths
            shear = _shear_of(layer)
            self._shear[master_id] = shear
            after, _ = azsym_scan.proposal(
                paths, self.modes, shear, self.signs, self.centre, self.split
            )
            self._proposed[master_id] = after
            self._bezier[master_id] = _bezier_of_coords(layer, after)
            # From the PROPOSAL, symmetric about the axis it was mirrored on —
            # which is not the box centre of the outline as drawn (model_centre).
            self._centre[master_id] = azsym_geometry.centre_of(
                [point for path in after for point in path], shear
            )
        bezier = self._bezier.get(master_id)
        centre = self._centre.get(master_id)
        if bezier is None or centre is None:
            return None
        return (
            bezier,
            centre,
            self._shear.get(master_id, 0.0),
            self._proposed[master_id],
            current,
        )


def _preview_due(preview, master_id: str) -> bool:
    """Whether this master's ghost may be re-solved on this frame.

    Always, unless a drag is running. Between edits the coordinates are still,
    so the cache carries the cost and an immediate rebuild is both exact and
    free. DURING a drag they change every frame, and rebuilding means a
    quadratic re-pairing plus a full ``GSLayer.copy()``, its nodes re-pointed
    one at a time across the bridge, and a fresh bezier — per frame, for a
    picture whose whole job is to be glanced at.

    Throttled rather than frozen, so the ghost still visibly follows the shape.
    Nothing has to catch it up when the drag ends: Glyphs repaints at mouse-up
    itself, and from then on this answers True.

    The first build is never throttled — a row selected while the mouse happens
    to be down would otherwise show nothing at all.
    """
    if preview._bezier.get(master_id) is None:
        return True
    if not _drag_in_progress():
        return True
    return time.time() - preview._rebuilt.get(master_id, 0.0) >= PREVIEW_REBUILD_INTERVAL


def _bezier_of_coords(layer, coords):
    """``coords`` as an NSBezierPath with its CURVES intact, or None.

    The pure model is ``list[list[(x, y)]]`` and carries no node types at all,
    so anything drawn from it alone can only join the points with straight lines
    — which is what once rendered every curve segment as a polygon. The types
    live on the layer, so a copy of it is re-pointed and Glyphs is asked for the
    bezier IT would draw. Already in LAYER coordinates, which is what the Edit
    view draws in, so nothing has to be transformed.
    """
    twin = _preview_layer(layer, coords)
    if twin is None:
        return None
    return _outline_of(twin)


def _master_order(font) -> dict[str, int]:
    """``{master id: position in the font}``.

    Masters are ordered by weight and width, never alphabetically, so a list
    sorted on the master NAME reads Black, Black Italic, Hairline, Regular —
    which is the font shuffled. Sorting on the position gives the order the
    designer set in Font Info.
    """
    order: dict[str, int] = {}
    try:
        for index, master in enumerate(font.masters):
            order[str(master.id)] = index
    except Exception:
        print(traceback.format_exc())
    return order


def _select_master(font, master_id: str) -> None:
    """Point the font at ``master_id``, by ID and never by name.

    Two masters may share a name, and a name may have been edited since the
    scan ran. A master that cannot be matched leaves the index alone — the
    glyph is the useful half.
    """
    if not master_id:
        return
    try:
        for index, master in enumerate(font.masters):
            if str(master.id) == master_id:
                font.masterIndex = index
                return
    except Exception:
        print(traceback.format_exc())


def _all_master_layers(font, glyph_name: str) -> list:
    """Every MASTER layer of this glyph, in the font's own order.

    Through ``_master_layers``, which is what the survey itself walks with, so
    the tab a row opens holds exactly the layers the row was measured against —
    masters only, with brace and bracket alternates left out.

    Empty when the glyph has gone or its layers cannot be walked, which the
    caller reads as "type the name instead".
    """
    if font is None:
        return []
    try:
        glyph = _glyph_named(font, glyph_name)
        if glyph is not None:
            layers, _unclassified = _master_layers(glyph, _master_order(font))
            return layers
    except Exception:
        print(traceback.format_exc())
    return []


def _glyph_named(font, glyph_name: str) -> Any:
    """The glyph called exactly ``glyph_name``, or None.

    ``glyphForName_`` first: a dictionary lookup, where the walk costs ~5 ms on
    a 900-glyph file, twice per layer of a batch Apply. Kept only when the name
    matches exactly; anything else, or a host without the selector, walks
    ``font.glyphs`` as before, so the answer cannot change.
    """
    try:
        glyph = font.glyphForName_(glyph_name)
        if glyph is not None and str(glyph.name) == glyph_name:
            return glyph
    except Exception:
        pass
    for candidate in font.glyphs:
        if str(candidate.name) == glyph_name:
            return candidate
    return None


def _fill_tab(font, tab, glyph_name: str) -> bool:
    """Show every master of this glyph in ``tab``. False if nothing was written.

    ALL MASTERS, not one (Hugo, 2026-09-10). Through ``tab.layers`` and not
    ``tab.text``, which is the whole of the fix: a text tab names a GLYPH and
    draws it in whichever master the font happens to be pointed at, so it can
    only ever show one — and a row about a symmetry is a row about a shape,
    which is the thing that differs between masters. ``_open_layers_tab``
    already reaches for layers over text for the same reason.

    Falls back to typing the glyph when the master layers cannot be read at
    all: one master beats an empty tab.
    """
    layers = _all_master_layers(font, glyph_name)
    try:
        if layers:
            tab.layers = layers
        else:
            tab.text = "/" + glyph_name
    except Exception:
        print(traceback.format_exc())
        return False
    return True


def _point_at(font, tab, glyph_name: str, master_id: str) -> None:
    """Put the cursor on the row's own master within a tab holding them all.

    ``_select_master`` is the FALLBACK and no longer the first move. Pointing
    the font at a master re-resolves a tab's text to that master, which on a
    tab whose whole purpose is to hold several would collapse it back to the
    one thing this change was made to stop showing. So the cursor is moved
    instead, and the font is only pointed when there was no such layer to move
    it to — which is the text-tab fallback above.
    """
    if not _place_cursor(tab, glyph_name, master_id):
        _select_master(font, master_id)


def _show_layer(tab: Any, glyph_name: str, master_id: str) -> Any:
    """Put this glyph's EVERY master into ``tab``, and hand the tab back.

    Every master and not just this row's (Hugo, 2026-09-10): the row is a claim
    about a shape, and whether that claim holds is a question you answer by
    looking along the masters. ``master_id`` still says which one the cursor
    lands on — see ``_point_at``.

    Reuses the tab across selections — retyping it rather than opening another,
    because browsing a thirty-row survey one arrow-key at a time would otherwise
    leave thirty tabs behind.

    A tab the designer has since CLOSED must not be written to: setting ``text``
    on one that is no longer in ``font.tabs`` either raises or writes into a
    detached object nobody can see, and both read as "clicking the row did
    nothing". Membership is re-tested every time, and a tab that has gone is
    replaced by a fresh one.
    """
    font = Glyphs.font
    if font is None:
        return None
    live = False
    if tab is not None:
        try:
            live = any(open_tab is tab for open_tab in font.tabs)
        except Exception:
            live = False
    if live and tab is not None:
        live = _fill_tab(font, tab, glyph_name)
    if not live:
        try:
            tab = font.newTab()
        except Exception:
            print(traceback.format_exc())
            return None
        _fill_tab(font, tab, glyph_name)
    _point_at(font, tab, glyph_name, master_id)
    return tab


def _open_layer(glyph_name: str, master_id: str) -> None:
    """Show this glyph across every master, for a double-clicked row.

    The same view a single click gives — a double click is the designer asking
    for a tab that will STAY, not for a different picture.

    A NEW tab every time, unlike the single-click path: selecting rows reuses
    ONE tab so a survey can be browsed without burying the window, and this is
    how the designer asks for a tab that will stay put while they carry on
    clicking.
    """
    font = Glyphs.font
    if font is None:
        return
    try:
        tab = font.newTab()
    except Exception:
        print(traceback.format_exc())
        return
    _fill_tab(font, tab, glyph_name)
    _point_at(font, tab, glyph_name, master_id)


def _layer_for(glyph_name: str, master_id: str) -> Any:
    """The layer named by a glyph and a master id, or None if it has gone.

    Two strings rather than a ``Row``, because the Apply path (Task 7) holds a
    batch key and not a row, and a function that demanded a Row would be
    handed a dummy one built to get past the signature.

    Found through ``_glyph_named`` rather than by subscripting ``font.glyphs``
    — a lookup that raised on a proxy that does not support that would turn a
    merely-missing glyph into a crash instead of the None the preview pane
    needs to tolerate.
    """
    return _layer_in(Glyphs.font, glyph_name, master_id)


def _layer_in(font, glyph_name: str, master_id: str) -> Any:
    """The same lookup, in a font named outright rather than the frontmost one.

    The dormant-binding notice needs this: it is raised from DOCUMENTOPENED and
    answered from a modal alert, and ``Glyphs.font`` between those two moments
    is whichever document the designer happens to be in front of — which on a
    session that opened several files at once is not the one being warned about.
    """
    if font is None:
        return None
    glyph = _glyph_named(font, glyph_name)
    if glyph is None:
        return None
    for layer in _all_layers(glyph):
        if str(getattr(layer, "layerId", "")) == master_id:
            return layer
    return None


def _remeasure_layer(glyph_name: str, master_id: str) -> tuple:
    """Fresh rows for exactly one layer, measured the same way ``_survey`` does.

    Called by ``ScanWindow._remeasure_applied`` after a press — see its docstring
    for why an applied layer's OWN sibling rows cannot be trusted to still
    read what they read before the bind. This re-runs the identical per-layer
    measurement ``_survey`` performs on every layer in the file, scoped to
    just this one ``(glyph, master_id)``: the other layers in the table were
    never touched by the bind and do not need walking again.

    Built as a one-``LayerFinding`` ``FontScan`` and handed to
    ``azsym_scan.report_rows`` rather than reimplementing its banding and
    sort here — sharing that call is what keeps a freshly-bound row's Bound
    band computed the exact same way a full survey would compute it, from the
    layer's own ``_bound_modes()``.

    Empty when the layer has gone missing since the scan; the caller then has
    nothing to put back for that key, which reads as the row quietly
    disappearing rather than as a crash.
    """
    layer = _layer_for(glyph_name, master_id)
    if layer is None:
        return ()
    # Through the LAYER, not through Glyphs.font: this runs after an apply, and
    # the em that banded the rest of the table is this layer's own file's — the
    # same one ``_survey`` read. Re-banding one row against a different font's
    # em would put a row in the table that the rest of it disagrees with.
    upm = _upm_of_layer(layer)
    finding = azsym_scan.LayerFinding(
        glyph=glyph_name,
        master=_master_name(layer),
        scan=_scan_of(layer, upm),
        components=_holds_components(layer),
        bound=tuple(sorted(_bound_modes(layer))),
        master_id=_master_id(layer),
        master_index=_master_order(Glyphs.font).get(_master_id(layer), 0),
    )
    return tuple(azsym_scan.report_rows(azsym_scan.FontScan(findings=(finding,), upm=upm)))


# ------------------------------------------------------- the scan's own sheet

# A sheet, and not a floating window (Hugo, 2026-08-11): the scan freezes the
# application while it runs, and a sheet is the one thing that says so in the
# place the freeze is happening. MEASURED at about 0.64 ms per Latin glyph, so a
# 3000-glyph three-master file is around six seconds of an application that
# looks hung.
# NO LAYER COUNTER, and the line centred over the bar (Hugo, 2026-09-10). The
# sheet used to carry "312/1734 layers" under the bar, which is a number nobody
# acts on — the bar already says how far along it is, and the count only
# invited the reader to work out how long the rest would take. What is left is
# one sentence saying what is happening, over the bar saying how far.
#
# 88 rather than 96: the counter's own line and the air over it, gone.
SCAN_PROGRESS_SIZE = (300, 88)
SCAN_PROGRESS_TITLE = "Scanning font…"

# LAYERS between redraws. Painting on every glyph costs more than measuring one
# does — a forced window display is a full redraw — so the bar moves in steps.
# 25 layers is about 16 ms of measurement, which is one frame.
#
# LAYERS and not glyphs (Hugo, 2026-08-11 — "the progress bar isn't really
# incrementing"). The throttle counted GLYPHS while the work is per LAYER, so on
# a multi-master file it repainted once per (masters x 25) layers: MEASURED at 36
# repaints for a 900-glyph seven-master file, one every ~112 ms instead of every
# 16. The bar moved in lurches, and the constant's own comment claiming "one
# frame" was true only at a single master.
SCAN_PROGRESS_EVERY = 25


def _document_window(font):
    """The font's own document window, for a sheet to hang from.

    ``font.parent`` is the GSDocument. Guarded end to end and never allowed to
    raise: a font with no document, or a proxy that does not answer, simply
    gets no sheet — the scan is what the designer asked for, and it must not
    fail because a progress bar had nowhere to sit.
    """
    try:
        controller = font.parent.windowController()
        return None if controller is None else controller.window()
    except Exception:
        return None


def _layer_count(font) -> int:
    """How many master layers the survey will measure.

    LAYERS and not glyphs, because that is what the bar is scaled against and
    what the survey's own summary line counts. The two differ by
    the number of masters, so a 900-glyph file on seven masters is a bar that
    would otherwise be seven times further along than it says.

    Counted with the SAME ``_master_layers`` the walk uses rather than as
    glyphs x masters. That product is right only where every glyph has a layer
    for every master and nothing else, and a file with brace layers — which the
    survey deliberately skips — would give a total the count can never reach.
    Sharing the function makes the two agree by construction.

    The second pass costs a read of each layer's two ids, against roughly
    0.64 ms of MEASUREMENT per glyph. Guarded end to end and never allowed to
    raise, for the reason the sheet itself is: the scan is what the designer
    asked for, and it must not fail because a total could not be reached.
    """
    total = 0
    order = _master_order(font)
    try:
        for glyph in font.glyphs:
            layers, _unclassified = _master_layers(glyph, order)
            total += len(layers)
    except Exception:
        return 0
    return total


def _flush_drawing() -> None:
    """Push what was just drawn all the way to the screen.

    ``NSWindow.display()`` runs ``drawRect:`` immediately, but on a layer-backed
    window — which every window has been since 10.14 — that only puts the result
    into the layer. The layer tree reaches the screen when the current
    ``CATransaction`` commits, and the implicit transaction commits when the run
    loop next turns. The whole scan blocks the main thread, so the run loop
    never turns, and the bar can be set and displayed on every step while the
    screen still shows the frame it had when the sheet opened.

    Flushing commits that transaction WITHOUT processing events, which is the
    distinction that matters here: pumping the run loop instead would let the
    designer click into a document that is mid-measurement and start a second
    scan on top of the first.

    A host without the Quartz binding loses the flush, not the scan — and on
    such a host ``display()`` alone is what it always was.

    A raise from the flush itself is deliberately NOT caught here. ``step`` runs
    once per glyph and already owns the give-up rule: it catches, prints once,
    and stops painting for the rest of the survey. Swallowing the error here
    would leave that switch unflipped and print a traceback on every repaint,
    over the report the designer is waiting for.

    NOT verified inside a running Glyphs: this is reasoned from how layer-backed
    drawing reaches the screen. The other half of the fix — the throttle in
    ``step`` — is measured, and is independent of this.
    """
    if CATransaction is not None:
        CATransaction.flush()


class _ScanProgress:
    """A sheet on the document window with a bar, ticked as the survey walks.

    Best-effort by construction: every step is guarded, and a host that cannot
    build the sheet scans without one. A survey that FAILED because its progress
    bar could not be drawn would be the tail wagging the dog.

    The whole scan runs synchronously on the main thread, so nothing repaints
    by itself while it is going. The bar is therefore drawn by FORCING a display
    rather than by returning to the run loop: pumping the loop on every step
    would let the designer click into a document that is mid-measurement, and
    start a second scan on top of the first from the menu bar.
    """

    def __init__(self, font, parent=None) -> None:
        self._sheet = None
        # Whether the bar is still worth drawing. SEPARATE from ``_sheet``,
        # which is the handle that dismisses it: a paint failure gives up on
        # the drawing and must NOT give up on the dismissal, or the document
        # window is left blocked behind a sheet with nothing holding it.
        self._painting = True
        # LAYERS measured, which is what the bar is scaled against.
        self._done = 0
        # GLYPHS walked, which is what the repaint is throttled on. Kept apart
        # from ``_done`` on purpose: SCAN_PROGRESS_EVERY was measured in glyphs
        # — 25 of them is about one frame of work — and counting the throttle
        # in layers instead would repaint once per master as often, which is
        # the cost the throttle exists to avoid.
        self._glyphs = 0
        # Layers measured as of the last repaint, which is what the throttle
        # counts from. Starting at 0 means the first paint falls due once a
        # frame's worth of measurement has actually happened.
        self._painted = 0
        # At least 1: an NSProgressIndicator with a maximum of zero is a bar
        # that can only ever read full, and an empty font is exactly when the
        # arithmetic below would divide into it.
        self._total = max(1, _layer_count(font))
        # THE WINDOW THAT ASKED, when there is one (Hugo, 2026-09-10). A sheet
        # belongs to the window whose button started it: run from the Symmetrify
        # window, a sheet that dropped out of the document window instead
        # blocked a window the designer was not looking at and left the one they
        # pressed in apparently doing nothing. The document window is the
        # fallback for the callers that have no window of their own.
        window = parent if parent is not None else _document_window(font)
        if vanilla is None or window is None:
            return
        try:
            sheet = vanilla.Sheet(SCAN_PROGRESS_SIZE, window)
            sheet.caption = vanilla.TextBox(
                (20, 20, -20, 17), SCAN_PROGRESS_TITLE, alignment="center"
            )
            sheet.bar = vanilla.ProgressBar(
                (20, 48, -20, 20), maxValue=self._total, isIndeterminate=False
            )
            sheet.open()
            self._sheet = sheet
            # ONE turn of the run loop, and only here. ``beginSheet:`` schedules
            # the sheet rather than drawing it, so without this the scan can run
            # to completion behind a sheet that never appeared — which is worse
            # than having no sheet at all, because the window is blocked either
            # way.
            NSRunLoop.currentRunLoop().runUntilDate_(NSDate.date())
        except Exception:
            print(traceback.format_exc())
            self._sheet = None

    def step(self, layers: int = 1) -> None:
        """One glyph measured, carrying how many LAYERS it held.

        Cheap on all but every SCAN_PROGRESS_EVERY-th LAYER. The default of 1
        keeps the old call shape working, so a caller that only knows it
        finished a glyph still moves the bar.

        Two things decide a repaint, and the second is not an optimisation.
        A frame's worth of layers since the last one — which is the throttle —
        OR having reached the total, so the bar is SEEN to finish. Without that
        second clause the remainder never painted: MEASURED, a 26-glyph font
        painted once, at 25, and the sheet came down with the bar still short of
        the end; a font under 25 glyphs never painted at all and the bar sat at
        zero for the whole scan.

        A ``_total`` the count could not work out leaves this painting on every
        glyph rather than every frame — slower, still correct. It is not worth
        guarding: ``_layer_count`` only returns 0 when walking ``font.glyphs``
        raises, and the survey's own walk of the same collection would not get
        this far.
        """
        self._done += layers
        self._glyphs += 1
        if self._sheet is None or not self._painting:
            return
        if self._done - self._painted < SCAN_PROGRESS_EVERY and self._done < self._total:
            return
        self._painted = self._done
        try:
            self._sheet.bar.set(self._done)
            self._sheet.getNSWindow().display()
            _flush_drawing()
        except Exception:
            # Given up on rather than retried: this runs once per glyph, and a
            # fault that repeats would print hundreds of identical tracebacks
            # over the report the designer is waiting for. The sheet itself is
            # kept — it is on screen, and something has to take it down.
            print(traceback.format_exc())
            self._painting = False

    def close(self) -> None:
        """Take the sheet down. NOT ``vanilla.Sheet.close()``.

        That raises a TypeError in Glyphs 4 — and it is CATCHABLE, so it leaks
        the sheet instead of crashing: the document window would stay blocked
        behind a sheet nothing can dismiss, with the scan window opening on top
        of it. Dismissed through AppKit instead, which is what ``close()`` was
        trying to do.

        What this runs is vanilla's own ``close()`` MINUS its last line. That
        method does ``isSheet()`` → ``NSApp().endSheet_()`` → ``orderOut_()``
        → ``self._window.close()``, and only the final call is the one that
        raises. Reproducing the first three is what was verified working in the
        app (AZ-Keyring's pickers, 2026-07-02) rather than merely reasoned
        about.

        ``NSApp`` and not ``-[NSWindow endSheet:]``, even though the latter is
        what Apple has been pointing at since 10.10: vanilla OPENS the sheet
        with ``NSApp beginSheet:modalForWindow:…``, so this is the matching
        half of the pair it actually used. Ending it on the wrong one is not a
        visible failure — the sheet would simply stay up, with the document
        window blocked behind it.

        The known cost is vanilla's open-time ``retain()``: with no real close,
        ``windowWillClose_`` never fires and the NSWindow leaks. One per scan,
        and a leak beats a blocked document.

        Cleared before the work, so a second call cannot dismiss a window that
        has already gone.
        """
        sheet, self._sheet = self._sheet, None
        if sheet is None:
            return
        try:
            window = sheet.getNSWindow()
            if window.isSheet():
                NSApplication.sharedApplication().endSheet_(window)
            window.orderOut_(None)
        except Exception:
            print(traceback.format_exc())


# How long the SAME traceback is held after being printed once. Long enough
# that a fault repeating on every frame is quiet, short enough that the report
# still says it is ongoing.
TRACEBACK_REPEAT = 10.0

# The last traceback printed from the draw path, and when.
_LAST_TRACEBACK: tuple[str, float] = ("", 0.0)


def _print_once(text: str) -> None:
    """Print a traceback from the draw path, but not sixty times a second.

    Every handler inside the tick prints and carries on, which is right: one
    bad layer must not stop the mirroring on every other one. What it must not
    also do is print the SAME traceback on every frame. The Macro window is a
    text view on the main thread, so a fault that repeats spends the frame
    budget rendering the report of itself — it takes the application down more
    surely than the fault it is reporting, and buries the first occurrence,
    which is the one worth reading.

    A DIFFERENT traceback always prints immediately: the second distinct
    failure is usually what explains the first.

    Deliberately not a latch that gives up. Suppressing the PRINT keeps the
    plugin's behaviour exactly as it was; suppressing the tick would stop
    mirroring for every layer in the font because one of them raised.
    """
    global _LAST_TRACEBACK
    last, moment = _LAST_TRACEBACK
    now = time.time()
    if text == last and now - moment < TRACEBACK_REPEAT:
        return
    _LAST_TRACEBACK = (text, now)
    print(text)


_PLUGIN = None


# ──────────────────────────────────────────────────────────────────────────────
# The plugin's face
# ──────────────────────────────────────────────────────────────────────────────
# One piece of art per plugin, bundled as `az-icon.pdf`, drawn wherever the
# plugin identifies itself: its item in Window ▸ AZ Toolkit and its tab in the
# Glyphs 4 left sidebar. It replaces the SF Symbol each surface used to pick on
# its own — the suite reads as a suite only if a plugin wears one face.
#
# TEMPLATE, always. The art is solid black on transparency, which is invisible
# against a dark menu and against a selected row. A template image is inked by
# AppKit with the surrounding text colour instead of its own, so the same file
# is black in light mode, white in dark mode, and white while highlighted.
#
# Inlined identically in every module that needs it — plugins share one
# sys.modules, so a bare `import sidebar` is a collision waiting to happen —
# so KEEP THE COPIES IN SYNC.

AZ_ICON_FILE = "az-icon.pdf"
_AZ_ICON_CACHE: dict = {}


def _az_icon(point_size: float = 14.0):
    """The bundled plugin icon as a template NSImage `point_size` tall, or None.

    Cached per size: an NSImage is mutable and carries its own size, so two
    surfaces asking for different sizes must not be handed the same object —
    that is how one caller's resize silently shrinks another's icon.
    """
    cached = _AZ_ICON_CACHE.get(point_size)
    if cached is not None:
        return cached
    try:
        import os

        from AppKit import NSImage

        # The art sits in the bundle's Resources/, but the module asking for it
        # may be a package DEEPER than that (azkeyring/sidebar.py), so walk up.
        folder = os.path.dirname(os.path.abspath(__file__))
        path = None
        for _ in range(3):
            candidate = os.path.join(folder, AZ_ICON_FILE)
            if os.path.exists(candidate):
                path = candidate
                break
            folder = os.path.dirname(folder)
        if path is None:
            return None
        image = NSImage.alloc().initByReferencingFile_(path)
        if image is None or not image.isValid():
            return None
        # `size()` answers an NSSize STRUCT, never a tuple — testing for a tuple
        # here is what once left a 200pt tile at its natural size in a menu row.
        natural = image.size()
        if natural.height:
            image.setSize_((natural.width * point_size / natural.height, point_size))
        image.setTemplate_(True)
        _AZ_ICON_CACHE[point_size] = image
        return image
    except Exception:
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Window ▸ AZ Toolkit
# ──────────────────────────────────────────────────────────────────────────────
# The suite shows ONE item at the root of the Window menu — "AZ Toolkit" — with
# every AZ plugin as a child, plus a link to the site at the bottom. Whichever
# plugin loads first builds the submenu; the rest find it by title and insert
# themselves alphabetically, so load order never shows.
#
# Child titles drop the "AZ " prefix (the parent already says it), matching the
# AZ Toolkit Settings hub, which labels its tabs the same way.
#
# This block is inlined identically in every AZ plugin — separate bundles share
# no module — so KEEP THE COPIES IN SYNC.

AZ_TOOLKIT_MENU_TITLE = "AZ Toolkit"
AZ_TOOLKIT_SITE_TITLE = "More AZ plugins…"
AZ_TOOLKIT_SITE_URL = "https://azerty-typefaces.com"
# NSMenuItem does not retain its target, and a plugin reload drops the module
# global that would hold it — so the site link's target is parked on the main
# thread's dictionary, which lives as long as the app does. Same rendezvous the
# AZ Toolkit Settings hub uses ("AZToolkit.SettingsHub.v1").
AZ_TOOLKIT_OPENER_KEY = "AZToolkit.WindowMenu.SiteOpener.v1"


def _az_toolkit_title(name: str) -> str:
    """A child item's label: the plugin name without its "AZ " prefix."""
    return name[3:] if name.startswith("AZ ") else name


def _az_toolkit_site_opener():
    """The retained object behind "More AZ plugins…". One per app session."""
    import objc
    from AppKit import NSWorkspace
    from Foundation import NSURL, NSObject, NSThread

    store = NSThread.mainThread().threadDictionary()
    opener = store.objectForKey_(AZ_TOOLKIT_OPENER_KEY)
    if opener is not None:
        return opener
    try:
        # Another AZ plugin already defined it: ObjC class names are global to
        # the process and all Python plugins share one interpreter, so
        # re-declaring the class raises instead of shadowing.
        opener_class = objc.lookUpClass("AZToolkitSiteOpener")
    except objc.error:

        class AZToolkitSiteOpener(NSObject):
            def openAZSite_(self, sender) -> None:
                NSWorkspace.sharedWorkspace().openURL_(
                    NSURL.URLWithString_(AZ_TOOLKIT_SITE_URL)
                )

        opener_class = AZToolkitSiteOpener
    opener = opener_class.alloc().init()
    store.setObject_forKey_(opener, AZ_TOOLKIT_OPENER_KEY)
    return opener


def _az_toolkit_menu():
    """Find-or-create ``Window ▸ AZ Toolkit`` and return its NSMenu, or None if
    the Window menu is not the shape we expect."""
    from AppKit import NSImage, NSMenu, NSMenuItem

    try:
        window_menu = Glyphs.menu[WINDOW_MENU].submenu()
        existing_items = list(window_menu.itemArray())
    except Exception:
        return None
    for item in existing_items:
        if str(item.title() or "") == AZ_TOOLKIT_MENU_TITLE:
            return item.submenu()

    submenu = NSMenu.alloc().initWithTitle_(AZ_TOOLKIT_MENU_TITLE)
    parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        AZ_TOOLKIT_MENU_TITLE, None, ""
    )
    parent.setSubmenu_(submenu)
    # The same symbol the AZ Toolkit pane carries in Settings, so the two
    # surfaces read as one thing. Guarded: a host without SF Symbols has no
    # such selector, and an icon is never worth losing the whole menu over.
    try:
        icon = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "wrench.and.screwdriver", AZ_TOOLKIT_MENU_TITLE
        )
        if icon is not None:
            icon.setTemplate_(True)
            icon.setSize_((14.0, 14.0))  # menu-item icon metrics
            parent.setImage_(icon)
    except Exception:
        pass

    # Same reasoning as the icon: if the opener cannot be built the plugin items
    # still belong in the menu, so only the link is dropped.
    try:
        site_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            AZ_TOOLKIT_SITE_TITLE, "openAZSite:", ""
        )
        site_item.setTarget_(_az_toolkit_site_opener())
        submenu.addItem_(NSMenuItem.separatorItem())
        submenu.addItem_(site_item)
    except Exception:
        pass

    window_menu.addItem_(NSMenuItem.separatorItem())
    window_menu.addItem_(parent)
    return submenu


def _install_az_toolkit_item(menu_item) -> None:
    """Put a plugin's item in the AZ Toolkit submenu, alphabetically, above the
    trailing separator + site link.

    IDEMPOTENT BY TITLE: reloading a plugin while Glyphs runs calls ``start``
    again on a menu that already holds the item, and AppKit will happily show
    two of them.
    """
    from AppKit import NSMenuItem

    submenu = _az_toolkit_menu()
    if submenu is None:  # unexpected menu shape — do not lose the item
        Glyphs.menu[WINDOW_MENU].append(NSMenuItem.separatorItem())
        Glyphs.menu[WINDOW_MENU].append(menu_item)
        return
    title = str(menu_item.title() or "")
    for existing in list(submenu.itemArray()):
        if str(existing.title() or "") == title:
            submenu.removeItem_(existing)
    items = list(submenu.itemArray())
    # The trailing separator is the end of the plugin block; the site link sits
    # below it and always stays last.
    end = next(
        (i for i, item in enumerate(items) if item.isSeparatorItem()), len(items)
    )
    insert_at = next(
        (i for i in range(end) if str(items[i].title() or "") > title), end
    )
    submenu.insertItem_atIndex_(menu_item, insert_at)


class AZSymmetry(GeneralPlugin):
    """Live symmetry, with the axis derived rather than placed."""

    @objc.python_method
    def settings(self) -> None:
        self.name = MENU_TITLE
        # (glyph name, layer id) -> {node ref: position} from the previous frame.
        self._snapshots: dict[tuple, dict] = {}
        # (glyph name, layer id) -> the previous frame's Structure, for
        # classifying what structurally changed.
        # ``(structure, signature)``: the signature is cached beside the
        # structure it describes because it cannot change while that structure
        # sits here, and recomputing it was half the gate's per-frame cost.
        self._structures: dict[tuple, tuple] = {}
        # Smooth nodes the live sync turned sharp, per layer, with the
        # connection each had: (structure signature, {node ref: connection}).
        # Stamped with the signature so a structural edit, which moves node
        # indices, discards it — see ``_keep_connections_honest``.
        self._sharpened: dict[tuple, tuple] = {}
        # (glyph name, layer id) -> (store object, parsed axes). See
        # ``_axes_for_layer`` for why the store object is half the key.
        self._axes: dict[tuple, tuple] = {}
        # (font, layer key, path symmetry id) -> (its GSPath objects as last
        # seen, the layer's path count then, the ones deleted) — see
        # ``_scoped_views``.
        self._scope_paths: dict[tuple, tuple] = {}
        # The plugin's one window, if it is open. HELD, because a vanilla
        # window belongs to whoever keeps a Python reference to it: dropped on
        # the floor at the end of the method that built it, it would be
        # collected and close itself, usually within a second of opening.
        self._window = None
        # The Analyze pane's controller, which outlives the window it was built
        # in. That is the point of keeping it: when the designer closes the
        # window and asks for it again, the rows and the ticks come across
        # rather than being measured over — see ``ScanWindow.__init__``.
        self._scan_window = None
        # Which master the Browse pane is filtered to, remembered across
        # openings (Hugo, 2026-09-10). None is ALL MASTERS and is where it
        # starts: a window that opened on the master that happened to be in
        # front showed a fraction of the file and looked like the whole of it.
        #
        # Held on the plugin rather than written to Glyphs' defaults, because a
        # master id means nothing in another document — ``set_master`` falls back
        # to All Masters for any id the font does not have, which is what makes
        # carrying one between files harmless.
        self._browse_master = None

    @objc.python_method
    def start(self) -> None:
        """The palette binds; the Window menu opens the window.

        TWO entry points and no more. The switch row in the palette sidebar is
        what binds a layer, and it is per-glyph — see ``AZSymmetryPalette``,
        which Glyphs builds from the bundle's ``Principal Classes`` and not from
        here. Everything font-wide lives in one window, and a window belongs in
        the Window menu — which is also where the rest of the AZ suite puts its
        own, so ours sits among its siblings rather than in a submenu of Glyph
        that only this plugin used.

        The Glyph menu is empty again as a result. It carried a ``Symmetrify``
        submenu with two items, one per window; both windows are now panes of
        one, and a submenu holding a single command to open it would be a level
        of menu for nothing.
        """
        global _PLUGIN
        _PLUGIN = self
        try:
            self._install_window_menu()
        except Exception:
            print(traceback.format_exc())
        # The tick. Registered unconditionally and never removed: this is what
        # makes mirroring independent of anything the user can switch off.
        Glyphs.addCallback(self.tick, DRAWFOREGROUND)
        # A node added or deleted on the drawn half is mirrored as the
        # designer's undo step closes, so the mirror joins the step, and an
        # undo or redo that brought both halves back is taken as it stands.
        center = NSNotificationCenter.defaultCenter()
        center.addObserver_selector_name_object_(
            self, "undoStepWillClose:", UNDO_STEP_CLOSING, None
        )
        for name in UNDO_NOTIFICATIONS:
            center.addObserver_selector_name_object_(self, "undoStepReplayed:", name, None)
        # Opening a file is the one moment the plugin can tell the designer that
        # a symmetry stopped being kept somewhere they were not looking. Guarded
        # separately: an older host without the constant must lose the notice,
        # not the mirroring — start() has already registered the tick above.
        try:
            Glyphs.addCallback(self.documentOpened, DOCUMENTOPENED)
        except Exception:
            print(traceback.format_exc())
        # Closing a file is the only moment the per-layer caches can be told
        # that what they describe has gone. Guarded separately, and skipped
        # outright on a host with no name for the event: the caches growing is
        # a slow leak, and losing the mirroring would be immediate.
        if DOCUMENTCLOSED is not None:
            try:
                Glyphs.addCallback(self.documentClosed, DOCUMENTCLOSED)
            except Exception:
                print(traceback.format_exc())

    @objc.python_method
    def documentClosed(self, sender) -> None:
        """Drop every per-layer cache when any document closes.

        ALL of them, and not just the closed font's: a key holds a glyph, and
        reaching a closed glyph's font to match it is not worth it. Clearing the
        lot is both simpler and nearly free, because every one of these caches is
        rebuilt from the layer itself: the structure on the next frame, the
        snapshot one frame after that, the axes on the next read of userData.
        A document closing therefore costs any other open document a single
        frame of mirroring, once.

        Without this they outlived the font. Every layer bound in a file that
        was closed an hour ago still held its coordinates, and quitting Glyphs
        was the only way to get them back. It also lets go of the glyphs the
        keys hold, deleted ones included.
        """
        self._snapshots.clear()
        self._structures.clear()
        self._sharpened.clear()
        self._axes.clear()
        self._scope_paths.clear()
        _PENDING_ANGLES.clear()
        _clear_field_preview()

    @objc.python_method
    def documentOpened(self, sender) -> None:
        """Warn once, if this file arrived with symmetries that stopped mirroring.

        Deferred to the next turn of the run loop. The document is still being
        opened when this fires, and a modal alert put up inside that has no
        window to attach to — and would sit in front of a file the designer
        cannot see yet.
        """
        try:
            font = getattr(sender, "font", None) or Glyphs.font
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
                0.0, False, lambda timer: self._warn_if_dormant(font)
            )
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _warn_if_dormant(self, font) -> None:
        """The notice itself, and the release it offers."""
        try:
            dormant = _dormant_bindings(font)
            if not dormant:
                return
            response = _dormant_alert(dormant)
            if response == APPLY_RESPONSE:
                self._apply_dormant(font, dormant)
            elif response == RELEASE_RESPONSE:
                self._release_dormant(font, dormant)
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _apply_dormant(self, font, dormant) -> None:
        """Snap every dormant symmetry back onto its outline.

        BINDS AGAIN rather than replaying the stored pairing, which is the same
        thing the notice used to tell the designer to do by hand: switch the
        symmetry off and on. Three reasons it has to be the bind and not a
        one-shot ``mirror_source`` over what is already stored.

        The stored pairing may not describe the layer any more. Half of what
        counts as dormant is ``axis_is_stale`` — node counts that no longer
        match — and there the stored indices point at whatever now occupies
        those positions. Mirroring through them corrupts the outline. Binding
        re-pairs from the outline in front of it, and ``_alignment`` ranks
        candidates by how many pairs STRADDLE the axis rather than by distance,
        so it does not need the shape to be symmetric yet.

        The stored centre may not either. An outside edit that merely TRANSLATED
        the outline — changing the left sidebearing does exactly this, rewriting
        every node's x — leaves every pair out of step with a stored axis that
        stayed behind, and the layer reads as dormant while being perfectly
        symmetric about where it now sits. Re-deriving the centre from the
        current outline makes that case snap nothing and simply refresh the
        stored axis, which is the honest outcome.

        And it keeps one code path forcing a symmetry onto an outline. The
        palette, the scan window's Apply and this all reach
        ``_bind_layer``, so they cannot drift apart on which half survives or on
        what gets written to userData.

        The SIGN is read back from the store rather than from the selection or
        from ``BATCH_SOURCE_SIGN``: the designer already chose which half is the
        drawn one when they bound this, and an alert fired at document-open time
        must not quietly reverse that with whatever happens to be selected. The
        guide split a half-turn was bound with is read back too
        (``_stored_split``): the stored sign names a half only on the split it
        was chosen on.
        """
        applied = left = 0
        for glyph_name, master_id, modes in dormant:
            layer = _layer_in(font, glyph_name, master_id)
            if layer is None:
                continue
            italic = bool(_shear_of(layer))
            for mode in modes:
                # On an italic a bind releases every symmetry it excludes, and
                # one bound before that rule may still hold two: which to keep
                # is the designer's call, not this loop's (review, 2026-10-22).
                others = {azsym_geometry.switch_of(other) for other in _bound_modes(layer)}
                others.discard(azsym_geometry.switch_of(mode))
                if italic and others & set(azsym_geometry.excluded_by(mode, italic=True)):
                    left += 1
                    continue
                if self._bind_layer(
                    layer, mode, _stored_sign(layer, mode), split=_stored_split(layer, mode)
                ):
                    applied += 1
        print(f"[AZ Symmetrify] re-applied {applied} dormant binding(s).")
        if left:
            print(
                f"[AZ Symmetrify] {left} left as they are: on an italic master only one "
                "symmetry can be on at a time, so switch the others off first."
            )
        self._refresh()

    @objc.python_method
    def _release_dormant(self, font, dormant) -> None:
        """Drop the bindings that stopped mirroring, leaving every outline alone.

        Releasing has never touched an outline — the shape a symmetry produced
        belongs to the designer once it is drawn — so this is only ever the
        pairing going away. What it buys is an honest file: no axis claiming to
        mirror something it stopped mirroring.
        """
        released = 0
        for glyph_name, master_id, modes in dormant:
            layer = _layer_in(font, glyph_name, master_id)
            if layer is None:
                continue
            for mode in modes:
                self._release_layer(layer, mode)
                released += 1
        print(f"[AZ Symmetrify] released {released} dormant binding(s).")
        self._refresh()

    # ------------------------------------------------------------------ menu

    @objc.python_method
    def _item_specs(
        self, layer, signs: dict[str, int] | None = None
    ) -> list[tuple[str, bool, str]]:
        """(label, bound, mode) per symmetry, for the layer in hand.

        Spelled out rather than a bare ``list`` so pyright checks the unpack at
        the other end. Dropping a field here and forgetting the consumer raises
        at runtime inside a swallowed except, which shows up as the switch row
        simply not appearing — with no error anywhere.

        The label carries the direction the switch would actually take, because
        the source side comes from the selection. Without it the three segments
        would be silent about which half of the glyph they overwrite. It reaches
        the user as the segment's tooltip. ``signs`` (``_source_signs``) when the
        caller has them already.
        """
        specs = []
        bound = _bound_modes(layer)
        if signs is None:
            unbound = [mode for mode, _title in SEGMENT_ITEMS if mode not in bound]
            signs = self._source_signs(layer, unbound) if layer is not None else {}
        for mode, title in SEGMENT_ITEMS:
            is_bound = mode in bound
            label = title
            # Only an UNBOUND switch names a direction. A lit one is about to go
            # OFF, so a "Left → Right" suffix would describe something that is
            # not about to happen.
            if layer is not None and not is_bound:
                label = f"{title}  ({self._source_label(layer, mode, signs[mode])})"
            specs.append((label, is_bound, mode))
        return specs

    @objc.python_method
    def _source_label(self, layer, mode: str, sign: int | None = None) -> str:
        """"Left → Right": the side the selection keeps, and the side it writes."""
        positive, negative = azsym_geometry.side_labels(mode, _split_standing(layer, mode))
        if sign is None:
            sign = self._source_sign(layer, mode)
        kept = positive if sign > 0 else negative
        written = negative if kept == positive else positive
        return f"{kept} → {written}"

    @objc.python_method
    def _install_window_menu(self) -> None:
        """Add ``Window ▸ AZ Toolkit ▸ Symmetrify``, which opens the plugin's window.

        The Window menu because that is what the item does — it opens a window —
        and the AZ Toolkit submenu because the rest of the suite lands there too,
        so eight plugins cost the Window menu one item instead of eight.

        Placement, dedupe on reload and the trailing site link all live in the
        shared ``_install_az_toolkit_item`` block above.
        """
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            WINDOW_ITEM_TITLE, "openWindow:", ""
        )
        # A REAL selector on a target that is retained. The plugin instance is
        # held by ``_PLUGIN`` for as long as Glyphs runs — an item whose target
        # is collected is an item that silently greys out.
        item.setTarget_(self)
        item.setToolTip_(
            "Browse the layers that already use Symmetrify, or analyze the file "
            "for layers that could."
        )
        # The plugin's own art now, not the left-right flip tile: every AZ item
        # in the submenu wears its plugin's face, and a mode tile named a MODE.
        # The tile stays as the fallback, at its own smaller box (it is
        # edge-to-edge art where az-icon.pdf carries its own padding).
        icon = _az_icon(16.0) or _symmetry_icon(MENU_ICON_SYMMETRY, MENU_ICON_BOX)
        if icon is not None:
            item.setImage_(icon)
        _install_az_toolkit_item(item)

    def openWindow_(self, sender) -> None:
        """Open the window, on Browse — what the file already uses."""
        try:
            self._open_window(PANE_BROWSE)
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _open_window(self, pane: str = PANE_BROWSE) -> bool:
        """Show the plugin's window on ``pane``, building it if it is not up.

        True once the window is up AND showing the pane that was asked for.

        ONE window per font, and the font is what decides whether the one in
        hand can be reused: a row and a cell both name their layer by glyph name
        and master id, and neither is unique to a document, so a window shown
        over the wrong file would act on it and report success. See
        ``SymmetrifyWindow.shows``.

        A CLOSED window cannot be reopened — vanilla drops its ``_window`` in
        ``windowWillClose_`` and says so in its own docstring — so ``reveal()``
        answering False is the signal to build another. The Analyze pane's rows
        and ticks come across when it does.
        """
        font = Glyphs.font
        if font is None:
            print("[AZ Symmetrify] no font is open, so there is nothing to show.")
            return False
        if vanilla is None:
            print(
                "[AZ Symmetrify] vanilla is not installed. Install it from "
                "Window > Plugin Manager > Modules to get the window."
            )
            self._without_a_window(font, pane)
            return False
        kept = self._window
        if kept is not None and kept.shows(font) and kept.reveal():
            return kept.set_pane(pane)
        # The ticks are the designer's own work, and closing a window is not a
        # decision to throw them away. Only from the same font: rows measured
        # in another document mean nothing here.
        previous = self._scan_window
        if previous is not None and not previous.surveys(font):
            previous = None
        try:
            window = SymmetrifyWindow(self, font, pane, previous)
        except Exception:
            print(traceback.format_exc())
            print("[AZ Symmetrify] the window could not be built.")
            # The window is what failed, not the answer. An older vanilla may
            # not accept every argument used here, and that must cost the
            # designer a window rather than the thing they asked for.
            self._without_a_window(font, pane)
            return False
        self._window = window
        window.open()
        return window.pane() == pane

    @objc.python_method
    def _without_a_window(self, font, pane: str) -> None:
        """What each pane falls back to when there is no window to put it in.

        TWO ways to end up here, and the caller has already said which: vanilla
        may be missing — it arrives through the Plugin Manager rather than with
        Glyphs, so its absence is a real state and not a defensive branch — or
        the window may have failed to build on a vanilla older than the
        arguments used here.

        Neither is allowed to cost the designer the answer they asked for, so
        each pane falls back to what that half of the feature shipped with
        before it had a window: the survey prints, and the bound layers open in
        a tab.
        """
        if pane == PANE_ANALYZE:
            if azsym_native.ERROR is not None:
                print(f"[AZ Symmetrify] {ENGINE_MISSING}: {azsym_native.ERROR}")
                return
            print("[AZ Symmetrify] the result is printed below rather than listed.")
            print("\n".join(azsym_scan.report_lines(self._surveyed(font))))
            return
        layers = _bound_layers(font, _current_master_id(font))
        if not _bound_layers(font, None):
            _nothing_bound_alert()
            print("[AZ Symmetrify] no layers in this file use Symmetrify.")
            return
        print("[AZ Symmetrify] the layers open in a tab rather than a grid.")
        _open_layers_tab(font, layers)

    # ------------------------------------------------------------------ tick

    @objc.python_method
    def tick(self, layer, info) -> None:
        """Per-frame draw callback. Fires on every frame of a node drag, which
        is the whole reason the work happens in a draw hook at all."""
        try:
            if azsym_native.ERROR is not None:
                # Without the engine a structural edit cannot be re-paired, and a
                # half-applied symmetry is worse than none: mirror nothing.
                return
            scale = 1.0
            try:
                scale = float(info["Scale"]) or 1.0
            except Exception:
                pass
            # Mirroring keeps running while the drawing is hidden. Only the
            # furniture goes away: a pan must not silently stop the symmetry,
            # which is the exact bug that made this a general plugin. Asked only
            # when there is something to draw: this runs for EVERY layer in the
            # tab, most have nothing, and the ask is ~0.1 ms.
            hidden = None
            # The layer's own symmetries, then each path symmetry on its own
            # paths: separate syncs, so one going stale freezes no other.
            for group in (layer, *self._scoped_views(layer)):
                # Each on its own: one that raises costs no other its frame.
                try:
                    axes = self._axes_for_layer(group)
                    if axes and _has_no_outline(group):
                        # The outline these axes describe has been deleted.
                        # Release them rather than mirror an empty layer — and
                        # before the sync, which would otherwise spend this
                        # frame reconciling a binding against nothing.
                        self._release_emptied(group)
                        axes = []
                    if not axes:
                        continue
                    if hidden is None:
                        hidden = _drawing_suppressed()
                    result, points = self._run_sync(group, axes)
                    if group is not layer:
                        self._keep_scope(group)
                    if not hidden:
                        # After the sync, so the shaded half reflects this
                        # frame's writes rather than lagging them by one. A
                        # bound half-turn's split as the Diagonal field previews it.
                        drawn = _with_field_split(group, axes)
                        warned = _stranded_points(group, axes, result.stale_axes)
                        self._draw_axes(group, drawn, result.stale_axes, scale, points, warned)
                except Exception:
                    _print_once(traceback.format_exc())
            if hidden is None and (_PREVIEW is not None or _FIELD_GHOST is not None):
                hidden = _drawing_suppressed()
            if not hidden:
                # Outside the ``axes`` branch: a scanned row is usually NOT
                # bound yet — that is the point of previewing it — so the ghost
                # has to be drawn for layers that have no axes at all.
                _draw_preview(layer, scale)
        except Exception:
            _print_once(traceback.format_exc())

    @objc.python_method
    def _keep_scope(self, view) -> None:
        """Store a path symmetry's path indices again when they moved: a path
        added or deleted elsewhere, or one its own sync removed. Guarded as
        ``_persist`` is, for the same reason."""
        scope = view.scope
        if scope == view.stored:
            return
        try:
            with _out_of_undo(view):
                if view.store(view.modes()):
                    view.stored = scope
        except Exception:
            _print_once(traceback.format_exc())

    @objc.python_method
    def _run_sync(self, layer, axes: list) -> tuple:
        """Reconcile the binding with any structural edit, then mirror positions.

        Two halves, in that order. ``reconcile`` says which nodes the MIRRORED
        side needs added, removed or moved so that it still describes the same
        shape the designer's side does, and hands back a pairing derived
        against the outline those edits produce. ``sync`` only ever moves
        nodes, and it can only do that through a pairing that already matches
        the layer — so it runs second, on paths re-read after the edits.

        Returns ``(result, points)``. The points are this layer's outline as it
        stands AFTER the write, flattened, which is exactly what ``_draw_axes``
        needs to span its axis line — handed over rather than re-read, because
        reading it again would be a third full walk of ``layer.paths`` on a
        frame that has already done two.
        """
        key = _snapshot_key(layer)
        structure = _structure_of(layer)
        # Computed ONCE and carried. The gate below and the cache write at the
        # end both want this frame's signature, and the cached entry carries the
        # PREVIOUS frame's alongside its structure — a signature that cannot
        # change while the structure it describes sits in a dict, and which was
        # nevertheless being recomputed on every frame of every drag.
        current = azsym_topology.signature(structure)
        cached = self._structures.get(key)

        # THE per-frame gate. classify() is O(n^2) and reconcile() re-derives
        # every pairing; neither may run on the sixty frames of a drag, where
        # positions change constantly and structure does not. signature() is
        # deliberately blind to position, so dragging a node never trips it, and
        # so is a renumbering that keeps the node types in order (renumbered).
        if cached is not None and (
            cached[1] != current or azsym_topology.renumbered(cached[0], structure)
        ):
            structure = self._reconcile(layer, axes, key, cached[0], structure, _open_step(layer))
            current = azsym_topology.signature(structure)

        self._structures[key] = (structure, current)

        result = azsym_sync.sync(
            structure.paths,
            axes,
            self._snapshots.get(key),
            guard_tol=azsym_sync.guard_tol(_upm_of_layer(layer)),
        )
        if result.translation is not None:
            # The outline moved as a whole — a sidebearing change, or everything
            # selected and nudged. The axis belongs to the SHAPE, not to the
            # layer's coordinates, so it travels with it. In place, and persisted
            # on the same frame, for the two reasons the reconcile path above
            # gives: tick hands this same list to _draw_axes, and _axes_for_layer
            # rebuilds from userData next frame.
            axes[:] = azsym_sync.translated_axes(axes, result.translation)
            self._persist(layer, axes)
        if result.recentred:
            # The designer moved a whole generated half, so its axis went
            # halfway (azsym_sync._recentred). Stored the same way, for the same
            # two reasons.
            axes[:] = [
                replace(axis, centre=result.recentred.get(index, axis.centre))
                for index, axis in enumerate(axes)
            ]
            self._persist(layer, axes)
        # Snapshot what the LAYER holds after the write, not what sync intended:
        # GSNode.position aligns to the layer grid, so the two differ by up to
        # hypot(0.5, 0.5) on a slanted axis — enough for the next frame to read
        # the mirrored node as user-moved and write back over the dragged one.
        actual = _apply(layer, result.writes) if result.writes else {}
        if actual:
            # This is the FOREGROUND callback: the outline on screen was drawn
            # before the write, and only the wash, drawn after it, shows the new
            # partner. A drag's next frame catches the outline up; an arrow-key
            # nudge or a Cmd-Z has none (Hugo, 2026-10-10).
            _request_repaint()
        # Also on a frame that writes nothing while a sharpened node is
        # remembered: Cmd-Z of the drag puts both handles back at once, so the
        # frame that could restore it has no write of its own.
        if actual or self._sharpened.get(key, (None, {}))[1]:
            structure = self._keep_connections_honest(layer, key, structure, actual)
        self._snapshots[key] = azsym_sync.next_snapshot(result, actual)
        # The outline AFTER the write, assembled from what is already in hand:
        # ``structure.paths`` is this frame's read and ``actual`` is what the
        # nodes kept of the mirrored positions, so overlaying one on the other
        # is the post-write outline without touching the layer again.
        points = [
            actual.get((path_index, node_index), point)
            for path_index, path in enumerate(structure.paths)
            for node_index, point in enumerate(path)
        ]
        return result, points

    def undoStepWillClose_(self, notification) -> None:
        """A glyph's undo step is closing: mirror a node the designer added or
        deleted on a bound layer NOW, while the step is open, so the mirror and
        its binding join it (``_reconcile``). Probed on Glyphs 4202: an edit
        made here lands in the closing step, and one Cmd-Z takes both back.
        Only the outermost step, and never one an undo or redo is closing."""
        try:
            manager = notification.object()
            if manager.groupingLevel() != 1 or manager.isUndoing() or manager.isRedoing():
                return
            for group in self._bound_groups(_glyph_of_undo(manager)):
                key = _snapshot_key(group)
                cached = self._structures.get(key)
                if cached is None:
                    continue
                axes = self._axes_for_layer(group)
                structure = _structure_of(group)
                current = azsym_topology.signature(structure)
                if not axes or (
                    current == cached[1] and not azsym_topology.renumbered(cached[0], structure)
                ):
                    continue
                structure = self._reconcile(group, axes, key, cached[0], structure, True)
                self._structures[key] = (structure, azsym_topology.signature(structure))
        except Exception:
            _print_once(traceback.format_exc())

    def undoStepReplayed_(self, notification) -> None:
        """An undo or redo of a step the mirror joined puts both halves AND the
        binding back together: take that outline as it stands, or the next
        frame reads the nodes coming back as an edit and mirrors them again.
        Only when the binding came back with it: one that did not (a mirror
        made out of the step) is left to the frame to repair, as before."""
        try:
            for group in self._bound_groups(_glyph_of_undo(notification.object())):
                key = _snapshot_key(group)
                cached = self._structures.get(key)
                if cached is None:
                    continue
                structure = _structure_of(group)
                current = azsym_topology.signature(structure)
                if current == cached[1] or any(
                    azsym_sync.axis_is_stale(axis, structure.paths)
                    for axis in self._axes_for_layer(group)
                ):
                    continue
                self._structures[key] = (structure, current)
                # Node indices moved under it.
                self._snapshots.pop(key, None)
        except Exception:
            _print_once(traceback.format_exc())

    @objc.python_method
    def _bound_groups(self, glyph) -> list:
        """Every layer of ``glyph`` and each path symmetry on it, as ``tick``
        syncs them."""
        if glyph is None:
            return []
        return [group for layer in glyph.layers for group in (layer, *self._scoped_views(layer))]

    @objc.python_method
    def _reconcile(
        self, layer, axes: list, key: tuple, previous, structure, undoable: bool
    ) -> azsym_topology.Structure:
        """Mirror a structural edit, ``previous`` to ``structure``, onto the
        generated half, and store the binding re-derived for it. Returns the
        structure as it now stands.

        ``undoable`` while the designer's undo step for the edit is still open:
        the mirror and the binding then join it, so one Cmd-Z takes both halves
        back (``undoStepWillClose_``). Out of it they could not: Glyphs undoes a
        node delete by putting the node back at its old INDEX (probed, Glyphs
        4202), and a partner deleted out of the step had shifted every index
        after it, so the node came back one place off and the next frame
        mirrored it there (Hugo, 2026-10-22: "completly broken layer").
        The step keeps the designer's name for it: the binding's write renames
        it "Set UserData" (probed, Glyphs 4202), so the name is put back.
        """
        undo = layer.parent.undoManager() if undoable else None
        name = undo.undoActionName() if undo is not None else ""
        change = azsym_topology.classify(previous, structure)
        if isinstance(layer, _ScopedLayer) and change.kind in (
            azsym_topology.CONTOUR_ADDED,
            azsym_topology.CONTOUR_REMOVED,
        ):
            # A path symmetry never adds or removes a contour: a deletion
            # or one put back by Cmd-Z is re-paired, never mirrored.
            change = azsym_topology.Change(kind=azsym_topology.UNCLASSIFIED)
        if change.kind == azsym_topology.RENUMBERED:
            # Keyed by the old numbering, even where the re-paired binding
            # comes out the same as the stored one.
            self._snapshots.pop(key, None)
        reconciled = azsym_sync.reconcile(structure, previous, axes, change)
        if _apply_structure(layer, reconciled.edits, undoable):
            # The edits moved node indices. Everything past this point —
            # the sync, the snapshot, the structure cached for the next
            # frame — has to read the outline that now exists rather than
            # the one this frame started with, or every mirrored position
            # is written to the wrong node.
            structure = _structure_of(layer)
            # And nothing else will invalidate the view: the write went
            # through the non-reporting update pair, so a node that has
            # just appeared or gone stays wrong on screen until some
            # unrelated event repaints. Only on the frame the edits landed,
            # which is why _apply_structure reports whether they did.
            _request_repaint()
        if reconciled.axes != tuple(axes):
            # In place, because tick hands this same list to _draw_axes
            # once the sync returns: drawn from the superseded binding, a
            # re-derived axis reports the wrong unpaired count.
            axes[:] = list(reconciled.axes)
            self._persist(layer, axes, undoable)
            # The snapshot is keyed by node index and those indices have
            # just moved. It is unusable on its own terms too: every pair
            # touching an inserted node is absent from it, and the pair
            # guard refuses anything it cannot see was symmetric last
            # frame, so the axis would report stale whatever the outline
            # looks like. Dropping it sends sync down its previous-is-None
            # path, which writes nothing this frame and rebuilds the
            # snapshot on the new indices. Mirroring resumes next frame.
            self._snapshots.pop(key, None)
        if undo is not None and name:
            undo.setActionName_(name)
        return structure

    @objc.python_method
    def _keep_connections_honest(self, layer, key: tuple, structure, actual: dict):
        """Make sharp the smooth nodes this frame's write kinked, and give back
        their connection to the ones whose handles line up again.

        Sharp: an on-axis node whose kept handle is not square to the axis would
        stay marked smooth over a kink, and Glyphs swings the mirrored handle
        back on the next drag frame — the two fight (see
        ``azsym_topology.kinked_smooth``).

        Back again: Hugo, 2026-10-10, "Cmd-Z does't restore smooth node". This
        retype cannot ride on the drag's own undo entry — it is made from the
        draw callback, where a registration lands as an entry of its own on top
        of the drag, so the first Cmd-Z would make the node smooth over the
        kink, this would sharpen it again, and Cmd-Z would look dead. So it is
        written with undo off, and REMEMBERED: once Cmd-Z puts the handles back
        (both of them — Glyphs swung the mirrored one on the drag's first frame,
        so the undo entry holds it too), or the designer drags it level, the
        node gets the connection it had. Only nodes this
        made sharp, and only while still sharp — a connection the designer set
        is theirs.

        Returns the structure as it now stands, also cached, so the next frame
        does not read the retype as the designer's edit and mirror it.
        """
        written = azsym_topology.Structure(
            paths=[
                [
                    actual.get((path_index, node_index), point)
                    for node_index, point in enumerate(path)
                ]
                for path_index, path in enumerate(structure.paths)
            ],
            types=structure.types,
        )
        remembered = self._sharpened.get(key)
        signature = azsym_topology.signature(structure)
        mine = dict(remembered[1]) if remembered and remembered[0] == signature else {}
        types = [list(node_types) for node_types in structure.types]
        changes: dict = {}
        for path_index, node_index in azsym_topology.kinked_smooth(written, actual):
            kind, connection = azsym_topology.decode_type(types[path_index][node_index])
            sharp = azsym_topology.encode_type(kind, azsym_topology.SHARP)
            changes[(path_index, node_index)] = sharp
            mine.setdefault((path_index, node_index), connection)
        if mine:
            trial = [list(node_types) for node_types in types]
            for (path_index, node_index), connection in mine.items():
                kind, _now = azsym_topology.decode_type(trial[path_index][node_index])
                trial[path_index][node_index] = azsym_topology.encode_type(kind, connection)
            still = set(
                azsym_topology.kinked_smooth(
                    azsym_topology.Structure(paths=written.paths, types=trial), list(mine)
                )
            )
            for ref, connection in list(mine.items()):
                if ref in changes or ref in still:
                    continue
                kind, now = azsym_topology.decode_type(types[ref[0]][ref[1]])
                if now == azsym_topology.SHARP:
                    changes[ref] = azsym_topology.encode_type(kind, connection)
                del mine[ref]
        if changes:
            _set_types(layer, changes, undoable=False)
            for (path_index, node_index), encoded in changes.items():
                types[path_index][node_index] = encoded
            structure = azsym_topology.Structure(paths=structure.paths, types=types)
            self._structures[key] = (structure, azsym_topology.signature(structure))
        self._sharpened[key] = (azsym_topology.signature(structure), mine)
        return structure

    @objc.python_method
    def _persist(self, layer, axes: list, undoable: bool = False) -> None:
        """Write a re-derived binding back to the layer.

        ``reconcile`` hands back a pairing built for the outline it has just
        fixed, and ``_axes_for_layer`` rebuilds the axes from userData on every
        frame. Leave the new pairing in memory only and it is gone at the end
        of this one: the next frame reloads the superseded pairing, and the
        structure cache has meanwhile moved on, so the signature gate never
        fires again. Measured on the pure modules, that leaves a binding whose
        node counts no longer match the layer — permanently stale, drawn red,
        mirroring nothing until the user binds it again by hand.

        The ``binding`` and the ``centre`` are rewritten; the shear and the
        source side are the user's own settings and nothing here touches them.
        The centre travels because a rigid move of the outline carries the axis
        with it, and reconcile leaves it alone, so writing it is a no-op on that
        path and the whole store stays in one function.

        Refuses a store it could not read, exactly as ``_write_userdata`` does.
        Writing back a store rebuilt from a failed read would release every
        symmetry on the layer — silently, from inside a draw callback.

        The write goes through ``_out_of_undo``: this runs inside the draw
        callback, where an interface report or an undo step of its own would
        both land on top of the user's edit. Unless ``undoable``: written while
        the designer's undo step is still open, it joins that step instead
        (``_reconcile``).
        """
        try:
            existing = _items_or_none(_read_userdata(layer, KEY))
            if not existing:
                return
            store = dict(existing)
            for axis in axes:
                key = azsym_geometry.switch_of(axis.mode)
                fields = _items_or_none(store.get(key))
                if not fields:
                    continue
                entry = dict(fields)
                entry["binding"] = azsym_binding.to_dict(axis.binding)
                entry["centre"] = [axis.centre[0], axis.centre[1]]
                store[key] = entry
            if undoable:
                _write_userdata(layer, KEY, store)
                return
            with _out_of_undo(layer):
                _write_userdata(layer, KEY, store)
        except Exception:
            _print_once(traceback.format_exc())

    # --------------------------------------------------------------- actions

    @objc.python_method
    def _toggle(self, mode: str, layer=None) -> None:
        """Each segment is a switch: bind if this symmetry is off, release if on.

        Re-binding a symmetry that is already on therefore takes two clicks
        (off, then on). That is the cost of the switch meaning one thing rather
        than two, and the lit segment says which state you are in.

        ``layer`` is the one the click was MADE over. The palette passes its own
        window's (``AZSymmetryPalette._switch_layer``): with two documents open,
        "the current layer" read from ``Glyphs.font`` is the FRONT document's,
        and a switch clicked in the other window bound a glyph in this one. With
        no layer given it is the front document's, which is what the tests and
        any one-window caller mean.
        """
        if layer is None:
            layer = _current_layer()
        if layer is None:
            return
        # The whole layer, or the path symmetry the selection names.
        target = self._target_of(layer)
        if isinstance(target, str):
            print(f"[AZ Symmetrify] {MODE_TITLES.get(mode, mode)} not bound: {target}.")
        elif mode in _bound_modes(target):
            self._release_layer(target, mode)
        else:
            guide = _guide_axis(target, mode)
            if isinstance(guide, str):
                # The row greys this switch; a click can still land on a row drawn
                # before the selection changed, and it binds nothing.
                print(f"[AZ Symmetrify] {MODE_TITLES.get(mode, mode)} not bound: {guide}.")
            else:
                # The typed angle's split (decision 8), whether or not a guide
                # places the centre — the one being typed, while the Diagonal
                # field previews it: the click binds what the ghost shows.
                split = _typed_split(target, mode)
                self._bind_layer(target, mode, guide=guide, split=split)
        _refresh_field_preview(layer)
        self._refresh()

    @objc.python_method
    def _refresh(self) -> None:
        """Ask for a redraw after a menu action.

        Without this, switching a symmetry on changes nothing on screen until
        the next edit happens to trigger a redraw — you have to nudge a node to
        see the axis appear.

        The cause is deliberate elsewhere: writes go through
        ``stopUpdateInterface``/``enableFutureUpdates``, the NON-reporting pair,
        so that a drag does not fire a plugin-originated interface update on
        every frame. A one-shot bind then reports nothing either. This is the
        trigger that would otherwise be missing.

        Deliberately ``redraw()`` and not a forced ``display()``: forcing a
        synchronous repaint here would be the mid-edit repaint the rest of this
        file is careful to avoid, and the ordinary run loop services this in
        time anyway.
        """
        try:
            Glyphs.redraw()
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _release_emptied(self, layer) -> None:
        """Drop every symmetry once the outline they described has gone.

        Deleting the last path — one contour at a time, or the whole layer at
        once — leaves a binding naming nodes that no longer exist, and nothing
        else takes it down: a symmetry is released from a menu opened over the
        glyph, and there is no outline left to open it over. So the layer goes
        on claiming to be mirrored. The switch row opens with its segment lit,
        the axis is drawn across an empty layer, and the first contour drawn
        next is classified against the deleted one.

        Every mode, not the one that happens to be first: a layer holding a
        vertical mirror AND a half-turn lost the outline under both of them.

        The writes carry the same guard as ``_persist`` and for the same
        reason — this runs inside the draw callback, and
        ``enableUpdateInterface`` REPORTS the collected changes, firing a
        plugin-originated interface update mid-edit. Undo registration is off
        for the reason ``_apply_structure`` gives: an entry pushed here does
        not join the user's own delete, it lands on top of it, so the first
        Cmd-Z would undo the release and appear to do nothing at all.
        """
        bound = _bound_modes(layer)
        if not bound:
            return
        try:
            with _out_of_undo(layer):
                for mode in sorted(bound):
                    self._release_layer(layer, mode)
        except Exception:
            _print_once(traceback.format_exc())

    @objc.python_method
    def _release(self, mode: str) -> None:
        """Drop one symmetry from the layer the Edit view is showing."""
        layer = _current_layer()
        if layer is None:
            return
        self._release_layer(layer, mode)

    @objc.python_method
    def _release_layer(self, layer, mode: str) -> None:
        """Drop one symmetry, leaving any other bound on this layer alone.

        Releasing never touches the outline: the shape it produced is the
        designer's now. Only the pairing goes.

        Takes the LAYER rather than reaching for the current one, because
        ``_bind_layer`` releases the modes a new binding excludes and it is
        handed a layer that may not be the one on screen — the scan window's
        Apply binds layers the designer is not looking at.
        """
        try:
            store = {
                name: value
                for name, value in _items(_read_userdata(layer, KEY) or {})
                if name != mode
            }
            # Checked, exactly as _bind checks its own store write. A failed
            # write leaves the symmetry BOUND, and the invariant this feature
            # rests on is bound-implies-key: restoring the key here would leave a
            # layer that is still mirroring without the key its binding asserts,
            # with nothing to say so.
            if not _write_userdata(layer, KEY, store or None):
                print(
                    "[AZ Symmetrify] the binding could not be removed from this layer, "
                    "so the symmetry is still bound and its right metrics key was "
                    "left in place."
                )
                return
            # What is still bound, so a layer holding a vertical mirror AND a
            # half-turn keeps the metrics key when one of the two goes.
            _release_metrics_key(layer, set(store))
            key = _snapshot_key(layer)
            self._snapshots.pop(key, None)
            # The structure baseline goes with it, for the reason spelled out
            # in _bind: it only describes the previous frame while a binding is
            # live, and nothing updates it while none is.
            self._structures.pop(key, None)
            # Released, its nodes are the designer's: a corner kept is no
            # longer one the sync made.
            self._sharpened.pop(key, None)
        except Exception:
            print(traceback.format_exc())

    @objc.python_method
    def _swap_side(self, mode: str, layer=None) -> bool:
        """Hand the designer the other half of ``mode``: the generated half
        becomes the one drawn (Hugo, 2026-10-21, "swap symmetry side").

        No node moves, because the generated half already mirrors the drawn one.
        Only the store changes: the sign, and each pair's two ends, since a pair
        names its drawn node first. One store write, so one Cmd-Z swaps back.
        ``layer`` and the path symmetry its selection names, as ``_toggle``.
        """
        try:
            if layer is None:
                layer = _current_layer()
            target = self._target_of(layer) if layer is not None else None
            if target is None or isinstance(target, str):
                return False
            axis = next(
                (
                    axis
                    for axis in self._axes_for_layer(target)
                    if azsym_geometry.switch_of(axis.mode) == mode
                ),
                None,
            )
            if axis is None:
                return False
            _sign, reason = self._swap_states(target)[mode]
            if reason:
                print(f"[AZ Symmetrify] {MODE_TITLES.get(mode, mode)} not swapped. {reason}.")
                return False
            pairs = tuple(
                (dst_path, dst_node, src_path, src_node)
                for src_path, src_node, dst_path, dst_node in axis.binding.pairs
            )
            store = dict(_items(_read_userdata(target, KEY) or {}))
            fields = dict(_items(store[mode]))
            fields["sign"] = -axis.sign
            fields["binding"] = azsym_binding.to_dict(replace(axis.binding, pairs=pairs))
            store[mode] = fields
            return _write_userdata(target, KEY, store)
        except Exception:
            print(traceback.format_exc())
            return False

    @objc.python_method
    def _swap_states(self, layer) -> dict[str, tuple[int, str]]:
        """Each symmetry bound on ``layer``: the side it has the designer draw,
        and why it cannot swap, or "". A swap hands a broken one's generated
        half the lead, so a symmetry that no longer mirrors, or leaves nodes
        without a partner, keeps its side."""
        axes = self._axes_for_layer(layer)
        dormant, _moved = _dormant_modes(layer, axes)
        states = {}
        for axis in axes:
            mode = azsym_geometry.switch_of(axis.mode)
            if axis.binding.unpaired:
                reason = SWAP_UNPAIRED
            elif mode in dormant:
                reason = SWAP_DORMANT
            else:
                reason = ""
            states[mode] = (axis.sign, reason)
        return states

    @objc.python_method
    def _bind(self, mode: str) -> None:
        """Bind ``mode`` on the layer the Edit view is showing.

        The palette's way in. Everything it does lives in
        ``_bind_layer``; this only decides which layer that is.
        """
        layer = _current_layer()
        if layer is None:
            return
        self._bind_layer(layer, mode)

    @objc.python_method
    def _bind_layer(
        self,
        layer,
        mode: str,
        sign: int | None = None,
        guide: tuple[float, float] | None = None,
        split: tuple[float, float] | None = None,
    ) -> bool:
        """Bind ``mode`` on ``layer``, whether or not it is the current one.

        Takes the layer as an argument because the scan window applies to rows,
        and a row names a layer that is very probably not open. Returns whether
        the binding landed, so a batch can count what it managed.

        ``sign`` names which half survives, and DEFAULTS to asking the
        selection through ``_source_sign``. The default is the contextual
        menu's answer and is deliberately kept: there the designer is standing
        in the glyph with nodes selected, and that selection IS the choice of
        side. A batch passes ``BATCH_SOURCE_SIGN`` instead, because the
        selection it would otherwise read belongs to whatever glyph was last
        opened rather than to the thirty rows being applied — and because the
        preview pane has already drawn that half.

        ``guide`` is the centre a selected guide gives (``_guide_axis``), and only
        the palette's click passes one (Hugo, 2026-10-15). It is bound about as
        given — never moved onto the on-axis nodes — and the batch never passes
        one, so a guide left selected cannot change what Analyze's Apply writes.

        ``split`` is a half-turn's typed split (``_typed_split``, decision 8). It
        decides which half survives in place of the box diagonal, and it is
        stored, so every later re-pair, re-apply and drawing splits the same way.
        """
        try:
            if not isinstance(layer, _ScopedLayer) and _scope_entries(layer):
                # Every path is already one path symmetry's or free to become
                # one; the whole layer would overlap them. Analyze's Apply too.
                print(
                    f"[AZ Symmetrify] {MODE_TITLES.get(mode, mode)} not bound: {PATHS_ELSEWHERE}."
                )
                return False
            paths = _paths_of(layer)
            if not paths:
                return False

            shear = _shear_of(layer)
            points = [point for path in paths for point in path]
            centre = guide if guide is not None else azsym_geometry.centre_of(points, shear)
            source_sign = self._source_sign(layer, mode) if sign is None else sign
            # On an italic a mirror switch may bind as its TURNED form — see
            # azsym_geometry.TURNED. Stored under the switch's own key, so the
            # switch pressed is the one that lights, with ``turn`` to say so.
            moved_as = azsym_binding.fitted(paths, centre, mode, shear, source_sign)
            binding = azsym_binding.pair_nodes(
                paths, centre, moved_as, shear, source_sign, split
            )
            if guide is None:
                # The axis goes through the on-axis nodes the kept half shares, not
                # wherever the box centre lands — see azsym_binding.model_centre.
                axis = azsym_binding.model_centre(paths, binding, centre, moved_as, shear)
                if axis != centre:
                    centre = axis
                    binding = azsym_binding.pair_nodes(
                        paths, centre, moved_as, shear, source_sign
                    )
            if binding.unpaired:
                # Hugo, 2026-10-10: a switch that cannot give a PURE symmetry is
                # not offered — the palette greys it (``_impure``) — and no way
                # in (scan Apply, dormant re-apply) binds one with nodes left over.
                print(
                    f"[AZ Symmetrify] {MODE_TITLES.get(mode, mode)} not bound: "
                    f"{len(binding.unpaired)} node(s) have no symmetric partner."
                )
                return False

            store = dict(_items(_read_userdata(layer, KEY) or {}))
            store[mode] = {
                "binding": azsym_binding.to_dict(binding),
                "centre": [centre[0], centre[1]],
                "shear": shear,
                "sign": source_sign,
            }
            if moved_as != mode:
                store[mode]["turn"] = True
            if split is not None:
                store[mode]["split"] = [split[0], split[1]]
            if not _write_userdata(layer, KEY, store):
                return False

            # The bind snap is the one write the user did not ask for by
            # dragging: it overwrites a whole side of the outline in one go, so
            # it has to be undoable. Only the per-frame writes suppress undo.
            writes = azsym_binding.mirror_source(paths, binding, centre, moved_as, shear)
            if writes:
                _apply_undoable(layer, writes, "Bind Symmetry")
            key = _snapshot_key(layer)
            self._snapshots.pop(key, None)
            # And the structure baseline with it. Nothing refreshes that cache
            # while no symmetry is bound, so a layer that was released, edited,
            # and bound again would be classified against the outline as it
            # stood BEFORE the release: the node the user added in between
            # reads as this frame's edit, and gets mirrored into an outline the
            # bind snap has already made symmetric.
            self._structures.pop(key, None)

            # Last, so a failure writing the key cannot cost the binding: the
            # store is already written and the snap has already landed.
            #
            # The unpaired count goes with it rather than being decided here,
            # so every reason the key can be refused lives in one function and
            # prints in one voice.
            if _bind_metrics_key(layer, mode, bool(binding.unpaired)) and _sync_metrics(layer):
                # The ink moved, and ``centre`` above is an absolute point in
                # the layer's own coordinates. Re-derived from the outline as it
                # now stands rather than nudged by a delta: the sync is a rigid
                # translation, so the new bounding-box centre IS the old one
                # moved, and deriving it cannot drift.
                moved = [point for path in _paths_of(layer) for point in path]
                store[mode]["centre"] = list(azsym_geometry.centre_of(moved, shear))
                _write_userdata(layer, KEY, store)
                self._snapshots.pop(key, None)
                self._structures.pop(key, None)

            # The mirrors and the half-turn are alternatives, never a set —
            # see azsym_geometry.excluded_by. Released AFTER the bind has
            # succeeded, deliberately: doing it first would mean a bind that
            # then failed had cost the designer the symmetry they already had,
            # and there is no repaint in between for the overlap to show in.
            #
            # The palette no longer reaches this: it GREYS OUT a segment
            # whose symmetry is excluded by one already on (``_segment_enabled``),
            # so the click cannot be made. This stays as the enforcement for the
            # other way in — the scan window's Apply, which binds layers the
            # designer is not looking at and cannot grey anything out for.
            for other in azsym_geometry.excluded_by(mode, italic=bool(shear)):
                if other in _bound_modes(layer):
                    self._release_layer(layer, other)
            return True
        except Exception:
            print(traceback.format_exc())
            return False

    @objc.python_method
    def _apply_batch(self, batch: dict) -> tuple[dict, int]:
        """Bind every mode in ``batch``, counting what landed and what did not.

        Each layer inside its own guard: one unreadable layer in a batch of
        thirty must not cost the other twenty-nine. Failures are counted and
        reported once by the caller rather than printing thirty tracebacks,
        which reads the same as none.

        Every bind is passed ``BATCH_SOURCE_SIGN`` EXPLICITLY, so the half a
        batch keeps is the half the preview pane drew and not whatever the Edit
        view happens to have selected. See the constant.

        Undo grouping: ``_apply_undoable`` — which ``_bind_layer`` already
        calls once per mode — groups on ``layer.parent.undoManager()``, reached
        through the GLYPH. Whether that manager is shared document-wide or
        handed out one per glyph is not something this file can settle without
        running Glyphs, so this is written to be correct either way: one outer
        group is opened per DISTINCT manager actually seen while walking the
        batch, named "Apply Symmetry", and closed in a ``finally``. Undo groups
        nest, so a shared manager collapses the whole batch under this one
        outer group; a per-glyph manager gets one such group per glyph, each
        still one Cmd-Z. A nil manager is skipped exactly as ``_apply_undoable``
        already tolerates one — the writes still happen, just not undoably.
        """
        applied: dict[tuple[str, str], set[str]] = {}
        failed = 0
        opened: list = []
        try:
            for (glyph_name, master_id), modes in batch.items():
                try:
                    layer = _layer_for(glyph_name, master_id)
                    if layer is None:
                        failed += 1
                        continue
                    manager = None
                    try:
                        manager = layer.parent.undoManager()
                    except Exception:
                        manager = None
                    if manager is not None and manager not in opened:
                        manager.beginUndoGrouping()
                        opened.append(manager)
                    landed = {
                        mode
                        for mode in azsym_geometry.MODES
                        if mode in modes and self._bind_layer(layer, mode, BATCH_SOURCE_SIGN)
                    }
                    if landed:
                        applied[(glyph_name, master_id)] = landed
                    if landed != set(modes):
                        failed += 1
                except Exception:
                    failed += 1
                    print(traceback.format_exc())
        finally:
            for manager in opened:
                # Two guards and not one. Sharing a ``try`` means a raise while
                # NAMING the group skips the call that closes it, and an undo
                # group left open on a live document breaks Cmd-Z for the rest
                # of the session. The name is a label; the balance is not.
                try:
                    manager.setActionName_("Apply Symmetry")
                except Exception:
                    print(traceback.format_exc())
                try:
                    manager.endUndoGrouping()
                except Exception:
                    print(traceback.format_exc())
        return applied, failed

    # ------------------------------------------------------------- symmetrise

    @objc.python_method
    def _symmetrise_layer(self, layer, modes) -> bool:
        """Make ``layer`` symmetric ONCE, and bind nothing.

        Everything ``_bind_layer`` does to the OUTLINE, and none of what it does
        to the layer: no store in ``layer.userData``, no metrics key, no
        generated half to shade, and nothing to switch off afterwards. The
        outline is settled and the plugin lets go of it — which is why this is
        the only write here that needs no counterpart to release it.

        The write comes from ``settlement`` and not ``proposal``: both halves
        move half-way towards each other rather than one being overwritten from
        the other. So there is no ``sign`` to pass and no side discarded, which
        is the whole reason this sits beside binding instead of being an option
        on it.

        Modes are settled in ``MODES`` order — the same order ``_apply_batch``
        binds in — because settling is no more commutative than binding: the
        second one meets an outline the first has already moved.

        Returns True for a layer that needed no writes at all. "Already
        symmetric" is a success, not a failure, and counting it as one would
        report a batch as broken for having nothing to do.
        """
        try:
            paths = _paths_of(layer)
            if not paths:
                return False
            ordered = tuple(mode for mode in azsym_geometry.MODES if mode in modes)
            if not ordered:
                return False

            settled, unpaired = azsym_scan.settlement(paths, ordered, _shear_of(layer))
            writes = {
                (path_index, node_index): settled[path_index][node_index]
                for path_index, path in enumerate(paths)
                for node_index, point in enumerate(path)
                if not azsym_geometry.close(point, settled[path_index][node_index])
            }
            if writes:
                # One undoable step, exactly as the bind snap is: it rewrites
                # both halves of an outline at once, and the designer asked for
                # it with a button rather than by dragging.
                _apply_undoable(layer, writes, "Symmetrise")
                # A layer that ALSO carries a binding has just had nodes moved
                # under it. Both caches are keyed on this layer and both now
                # describe the outline as it stood before the settle — the same
                # reason ``_bind_layer`` drops them after its own snap.
                key = _snapshot_key(layer)
                self._snapshots.pop(key, None)
                self._structures.pop(key, None)

            if unpaired:
                print(
                    f"[AZ Symmetrify] {unpaired} node(s) had no symmetric partner "
                    "and were left exactly where they are."
                )
            return True
        except Exception:
            print(traceback.format_exc())
            return False

    @objc.python_method
    # ponytail: no caller since the Symmetrise button was removed (Hugo,
    # 2026-09-10). Kept, with ``_symmetrise_layer`` and ``_confirm_symmetrise``,
    # because settling is a whole tested behaviour and the button is the only
    # part that was asked for — wire it to a menu item, or delete the three
    # together.
    def _symmetrise_batch(self, batch: dict) -> tuple[dict, int]:
        """Settle every layer in ``batch``, counting what landed and what did not.

        The same shape as ``_apply_batch``, for the same reasons: one guard per
        layer, so a single unreadable layer in a batch of thirty cannot cost the
        other twenty-nine; and one undo group per DISTINCT undo manager actually
        seen, closed in a ``finally``. See that method for why the grouping is
        written to be correct whether Glyphs hands out one manager per document
        or one per glyph.

        It differs in one way that matters. A layer's whole set of modes goes to
        ``_symmetrise_layer`` in ONE call, where ``_apply_batch`` binds mode by
        mode — the sequencing lives inside ``settlement``, so splitting it here
        would settle against a centre re-derived one step too early.
        """
        applied: dict[tuple[str, str], set[str]] = {}
        failed = 0
        opened: list = []
        try:
            for (glyph_name, master_id), modes in batch.items():
                try:
                    layer = _layer_for(glyph_name, master_id)
                    if layer is None:
                        failed += 1
                        continue
                    manager = None
                    try:
                        manager = layer.parent.undoManager()
                    except Exception:
                        manager = None
                    if manager is not None and manager not in opened:
                        manager.beginUndoGrouping()
                        opened.append(manager)
                    wanted = {mode for mode in azsym_geometry.MODES if mode in modes}
                    if wanted and self._symmetrise_layer(layer, wanted):
                        applied[(glyph_name, master_id)] = wanted
                    else:
                        failed += 1
                except Exception:
                    failed += 1
                    print(traceback.format_exc())
        finally:
            for manager in opened:
                # Two guards and not one, exactly as in ``_apply_batch``: a
                # raise while NAMING the group must not skip the call that
                # closes it, or Cmd-Z is broken for the rest of the session.
                try:
                    manager.setActionName_("Symmetrise")
                except Exception:
                    print(traceback.format_exc())
                try:
                    manager.endUndoGrouping()
                except Exception:
                    print(traceback.format_exc())
        return applied, failed

    # ------------------------------------------------------------------ read

    @objc.python_method
    def _source_sign(self, layer, mode: str) -> int:
        """Which side of the symmetry survives the bind.

        The selection decides: whichever side holds the selected nodes is the
        side kept. With nothing selected it falls back to the negative side —
        left for both mirrors, left for a half-turn, bottom for a horizontal
        one — because reading left-to-right and bottom-up is the drawing order
        most designers work in.
        """
        return self._source_signs(layer, (mode,))[mode]

    @objc.python_method
    def _source_signs(self, layer, modes, paths=None) -> dict[str, int]:
        """``_source_sign`` for each of ``modes``, off ONE read of the layer.

        The switch row asks for every mode on every interface update, and each
        ask walked the outline and the selection again: on a 70-node glyph that
        was most of an update. ``paths`` when the caller has just read them.
        """
        try:
            if paths is None:
                paths = _paths_of(layer)
            if not paths:
                return dict.fromkeys(modes, -1)
            shear = _shear_of(layer)
            points = [point for path in paths for point in path]
            centre = azsym_geometry.centre_of(points, shear)
            extent = azsym_geometry.extent_of(points, shear)
            selected = _selected_nodes(layer)
            signs = {}
            for mode in modes:
                # About the axis the click will use: a selected guide's when
                # there is one (``_guide_axis``), so the half kept is decided
                # about the line it is kept from.
                guide = _guide_axis(layer, mode, paths)
                about = guide if isinstance(guide, tuple) else centre
                # And across the typed line for a half-turn (decision 8).
                split = _typed_split(layer, mode)
                across = split if split is not None else extent
                total = 0.0
                for node in selected:
                    total += azsym_geometry.side(node, about, mode, shear, across)
                signs[mode] = 1 if total > 0 else -1
            return signs
        except Exception:
            return dict.fromkeys(modes, -1)

    @objc.python_method
    def _axes_for_layer(self, layer) -> list:
        """The axes this layer is bound to, rebuilt from its userData.

        Runs for EVERY layer the Edit view draws, on every frame — a text tab
        showing thirty glyphs asks thirty times a frame — so the parse is
        cached. ``from_dict`` rebuilds and revalidates every pair, every on-axis
        node and every unpaired node in the binding, which is the expensive
        half; the userData read itself is one trip and is kept.

        The cache is keyed on the layer AND on the store object it was built
        from, and hits only when the store is the SAME OBJECT as last time.
        That is what makes it safe rather than merely fast. A store this plugin
        replaces — ``_persist``, a bind, a release — is a new object, and so is
        one Cmd-Z puts back, so neither can be served a stale parse. Keying on
        the layer alone would have to be invalidated by hand at every write
        site, and would still be wrong for the undo, which goes through none of
        them.

        The consequence of the identity test is that it may simply never hit if
        the host hands back a fresh wrapper on every read. That costs nothing:
        the miss path is exactly the work this function did before.

        The list is COPIED on the way out. ``_run_sync`` assigns into the list
        it is given (``axes[:] = ...``) when a binding is re-derived, and a
        caller must not be able to edit the cache. The Axis objects inside are
        shared, which is safe because nothing ever mutates one in place.
        """
        store = _read_userdata(layer, KEY)
        if not store:
            return []
        cache_key = _snapshot_key(layer)
        cached = self._axes.get(cache_key)
        if cached is not None and cached[0] is store:
            return list(cached[1])
        axes = self._parse_axes(store)
        self._axes[cache_key] = (store, axes)
        return list(axes)

    @objc.python_method
    def _scoped_views(self, layer) -> list:
        """One view per path symmetry on ``layer``, its paths followed by identity.

        A path the designer deleted leaves the symmetry and is remembered, so
        Cmd-Z puts it back in. Paths that vanished while the count did not fall
        by as many were rebuilt as new objects (Remove Overlap, a filter, an
        undo): the stored indices are trusted instead, and the symmetry's frame
        caches dropped, so it writes nothing until it has seen its pairs
        symmetric again. Keyed by font, as two open copies of a file share
        glyph names, layer ids and entry ids.
        """
        entries = _scope_entries(layer)
        if not entries:
            return []
        key = _snapshot_key(layer)
        font = layer.parent.parent
        paths = list(layer.paths)
        live = {id(path) for path in paths}
        views = []
        for entry in entries:
            stored = [paths[index] for index in entry["paths"] if 0 <= index < len(paths)]
            held, count, gone = self._scope_paths.get((font, key, entry["id"]), (None, 0, []))
            if held is None:
                objects = stored
            else:
                missing = [path for path in held if id(path) not in live]
                back = [path for path in gone if id(path) in live]
                if missing and len(paths) != count - len(missing) + len(back):
                    objects, gone = stored, []
                    view_key = (*key, entry["id"])
                    for cache in (self._snapshots, self._structures, self._sharpened):
                        cache.pop(view_key, None)
                else:
                    objects = [path for path in held if id(path) in live] + back
                    gone = [path for path in gone if id(path) not in live] + missing
            self._scope_paths[(font, key, entry["id"])] = (objects, len(paths), gone)
            views.append(_ScopedLayer(layer, entry["id"], objects, entry["paths"]))
        return views

    @objc.python_method
    def _target_of(self, layer) -> Any:
        """What the switches act on, from the selection: the whole layer, one
        path symmetry (bound, or new over the complete paths selected), or a
        string saying why neither. See the path-symmetry design note."""
        if isinstance(layer, _ScopedLayer):
            return layer
        views = self._scoped_views(layer)
        complete, touched = _selection_by_path(layer)
        owner = {index: view for view in views for index in view.scope}
        # Only when every path touched is complete: a marquee over the half to
        # keep may catch a dot whole, and that still means the whole layer.
        if complete and complete == touched and len(complete) < len(list(layer.paths)):
            if _bound_modes(layer):
                return layer
            holders = {owner.get(index) for index in complete}
            if holders == {None}:
                return _new_scope(layer, complete)
            return holders.pop() if len(holders) == 1 else MIXED_PATHS
        holders = {owner.get(index) for index in touched}
        if len(holders) == 1 and None not in holders:
            return holders.pop()
        return PATHS_ELSEWHERE if views else layer

    @objc.python_method
    def _parse_axes(self, store) -> list:
        """``_axes_for_layer`` without the cache around it."""
        by_mode = dict(_items(store))

        axes = []
        for mode, _title in MODE_ITEMS:
            payload = by_mode.get(mode)
            if payload is None:
                continue
            fields = dict(_items(payload))
            binding = azsym_binding.from_dict(fields.get("binding"))
            if binding is None:
                continue
            centre = _point_of(fields.get("centre"))
            if centre is None:
                continue
            try:
                shear = float(fields.get("shear", 0.0))
            except (TypeError, ValueError):
                shear = 0.0
            try:
                sign = 1 if int(fields.get("sign", -1)) > 0 else -1
            except (TypeError, ValueError):
                sign = -1
            if fields.get("turn"):
                mode = azsym_geometry.TURNED.get(mode, mode)
            # A half-turn bound about a guide splits on its line (decision 7);
            # no key, or one that cannot be read, is the box diagonal.
            split = _point_of(fields.get("split"))
            axes.append(
                azsym_sync.Axis(
                    binding=binding, centre=centre, mode=mode, shear=shear, sign=sign, split=split
                )
            )
        return axes

    # ------------------------------------------------------------------ scan

    @objc.python_method
    def _scan_font(self) -> None:
        """Open the window on Analyze, which surveys the file if it has to.

        The SURVEY is read-only: it measures what each symmetry would cost and
        writes nothing. Binding in bulk is offered from the pane it fills, and
        is deliberately not automatic — a bind is a forcing operation that
        overwrites half an outline, so it is offered one preview and one
        confirmation at a time rather than on the strength of the heuristic.

        THE SURVEY RUNS ONCE PER FONT (Hugo, 2026-08-11), which is now a
        property of the pane rather than of this method: ``set_pane`` builds the
        Analyze pane the first time it is asked for and shows the one already
        built every time after. A scan is seconds of a frozen application, and
        re-running it every time the pane was switched to would make going back
        and forth cost as much as producing the answer.

        The consequence is that the list can be STALE — it describes the font as
        it was when it was measured, not as it is. That is only ever a stale
        DESCRIPTION: Apply re-derives everything from the layer in front of it
        (see ``_bind_layer``), so a bind is correct whatever the table says. The
        one thing a stale row can do is get past ``_risky`` without asking,
        because that gate reads the band the row was given at survey time.

        Nothing here writes, so nothing here needs undo, an update-interface
        pair, or a repaint.
        """
        if not self._open_window(PANE_ANALYZE):
            return
        window = self._window
        if window is not None and window.analyze is not None:
            window.analyze.run_scan_once()

    @objc.python_method
    def _surveyed(self, font, parent=None) -> azsym_scan.FontScan:
        """``_survey`` behind a progress sheet, which is how both callers want it.

        The sheet is taken down in a ``finally``: a survey that raised would
        otherwise leave the document window blocked behind a sheet with no way
        to dismiss it, which is a worse outcome than the failure itself.
        """
        progress = _ScanProgress(font, parent)
        try:
            return self._survey(font, progress.step)
        finally:
            progress.close()

    @objc.python_method
    def _survey(self, font, progress=None) -> azsym_scan.FontScan:
        """Walk the font's master layers and measure each one.

        Every layer is read behind its own guard and a layer that raises is
        COUNTED rather than skipped. A scan is a claim about a whole file, and a
        file where forty layers quietly failed to parse would otherwise report
        as a file with forty fewer layers in it.

        ``progress`` is called once per GLYPH — carrying how many master layers
        that glyph held, which is the unit the sheet counts in — and optional:
        the measurement is the thing being tested, and a survey that could only
        run with a window attached to it would have to be tested through one.
        """
        if azsym_native.ERROR is not None:
            # Every measurement runs in the engine. Without it the loop below
            # would count every layer unreadable, which reads as a broken font
            # rather than a missing engine, so the survey refuses outright.
            raise azsym_native.EngineUnavailable(f"{ENGINE_MISSING}: {azsym_native.ERROR}")
        findings: list[azsym_scan.LayerFinding] = []
        unreadable = 0
        glyphs = 0
        order = _master_order(font)
        # Read ONCE per survey and not per layer. It is a property of the file,
        # and asking three thousand times would be three thousand guarded
        # attribute walks for an answer that cannot change mid-scan.
        upm = _upm_of(font)
        # The native engine walks the font itself when it can — the whole
        # survey in well under a second. The loop below is its fallback and its
        # specification: _native_survey reads and counts exactly as it does.
        walked = _native_survey(font, order, upm, progress)
        if walked is not None:
            return walked
        for glyph in font.glyphs:
            glyphs += 1
            layers, unclassified = _master_layers(glyph, order)
            unreadable += unclassified
            for layer in layers:
                try:
                    findings.append(
                        azsym_scan.LayerFinding(
                            glyph=str(glyph.name),
                            master=_master_name(layer),
                            scan=_scan_of(layer, upm),
                            components=_holds_components(layer),
                            bound=tuple(sorted(_bound_modes(layer))),
                            master_id=_master_id(layer),
                            master_index=order.get(_master_id(layer), 0),
                        )
                    )
                except Exception:
                    unreadable += 1
            # AFTER the glyph rather than before it, so the count reports what
            # has been measured rather than what is about to be — and so it can
            # carry how many layers this glyph actually held.
            if progress is not None:
                progress(len(layers))
        return azsym_scan.FontScan(
            findings=tuple(findings), glyphs=glyphs, unreadable=unreadable, upm=upm
        )

    # ------------------------------------------------------------------ draw

    @objc.python_method
    def _fill_generated(self, outline, axes: list, extent, color=None) -> None:
        """Shade every half of the glyph that a binding generates — ONCE.

        Clipped rather than reconstructed: a generated side is not a closed
        shape on its own, so the glyph's own filled path is drawn through the
        half-planes. ``-axis.sign`` because the stored sign is the side the
        designer DRAWS, and the other one is the side being written.

        All the axes share one clip and one fill. Filling per axis composites
        the wash twice wherever two of them overlap, so with a vertical and a
        horizontal mirror bound together the shared quadrant came out visibly
        darker than either half alone — reading as a third state that means
        nothing.
        """
        _fill_generated_halves(outline, axes, extent, color)

    @objc.python_method
    def _draw_axes(
        self, layer, axes: list, stale_axes, scale: float, points=None, warned=()
    ) -> None:
        """Draw each bound axis in the colour of its actual state.

        ``stale_axes`` comes from the sync that just ran, so an axis that has
        stopped mirroring — because a contour was reordered or a node count
        changed — is drawn red rather than being recomputed here from node
        counts alone, which cannot see the first case.

        ``warned`` are the generated handles a selected node would strand
        (``_stranded_points``): the wash and every healthy axis go orange, and
        each handle gets an orange dot and ``STRANDED_SIGN`` over it.
        """
        # A stale axis is writing nothing, so shading a half of the glyph as
        # "generated" on its behalf would be a lie.
        shaded = [axis for index, axis in enumerate(axes) if index not in stale_axes]
        if points is None:
            points = [point for path in _paths_of(layer) for point in path]
        if shaded:
            # Fetched only when something is actually being shaded — this runs
            # on every frame of a drag. The box is the layer's as it stands,
            # which a half-turn's split runs corner to corner across.
            extent = azsym_geometry.extent_of(points, shaded[0].shear)
            wash = UNPAIRED_COLOR().colorWithAlphaComponent_(MIRRORED_FILL_ALPHA)
            self._fill_generated(_outline_of(layer), shaded, extent, wash if warned else None)

        # Every axis is held to the same glyph, so its extent is read once.
        # Live rather than frozen at bind time: the axis POSITION must not
        # follow the shape around (that is what would let a drag drag the axis
        # after it), but the drawn line's ends should, or the line stops
        # matching the glyph the moment it is edited.
        #
        # ``points`` normally arrives from ``_run_sync``, which has already read
        # the outline this frame. The fallback above re-reads it, and is what a
        # caller with no sync behind it — a test, or any future one-shot draw —
        # gets.

        # A path symmetry's line shows only while its paths are selected, or
        # several on one glyph cross it with lines. A warning shows regardless.
        quiet = isinstance(layer, _ScopedLayer) and not _selection_by_path(layer)[1]
        width = _span_width(layer)
        for index, axis in enumerate(axes):
            stale = index in stale_axes
            unpaired = len(axis.binding.unpaired)
            if quiet and not stale and not unpaired:
                continue
            label = azsym_sync.axis_label(stale, unpaired)
            if stale:
                color = STALE_COLOR()
            elif unpaired or warned:
                color = UNPAIRED_COLOR()
            else:
                color = BOUND_COLOR()
            color.set()

            origin = azsym_geometry.axis_origin(axis.centre, axis.shear)
            _stroke_axis_line(
                points, axis.centre, axis.mode, axis.shear, scale, axis.split, width
            )
            if label is not None:
                _draw_text(label, origin, color, scale)
        reach = STRANDED_DOT_RADIUS / scale
        for point in warned:
            UNPAIRED_COLOR().colorWithAlphaComponent_(STRANDED_DOT_ALPHA).set()
            NSBezierPath.bezierPathWithOvalInRect_(
                ((point[0] - reach, point[1] - reach), (2 * reach, 2 * reach))
            ).fill()
            _draw_text(STRANDED_SIGN, point, UNPAIRED_COLOR(), scale, 14.0, (-9.0, 5.0))


# ---------------------------------------------------------------- helpers


def _half_plane_path(axis, sign: int, extent, into=None):
    """``axis``'s half-plane on the ``sign`` side, as a closed NSBezierPath —
    appended to ``into`` when given. ``extent`` is the layer's box
    (``azsym_geometry.extent_of``), which the half-turn splits corner to corner.
    An axis bound about a guide splits on the guide's line instead
    (``azsym_sync.split_of``, decision 7)."""
    path = into if into is not None else NSBezierPath.bezierPath()
    corners = azsym_geometry.half_plane(
        axis.centre, axis.mode, axis.shear, sign, AXIS_REACH, azsym_sync.split_of(axis, extent)
    )
    path.moveToPoint_(NSPoint(corners[0][0], corners[0][1]))
    for point in corners[1:]:
        path.lineToPoint_(NSPoint(point[0], point[1]))
    path.closePath()
    return path


def _generated_clip(axes: list, extent):
    """The union of every half ``axes`` would generate, as one clip path."""
    clip = NSBezierPath.bezierPath()
    # Explicit, not inherited: the default winding rule is an app-wide setting
    # anyone can have changed, and even-odd would punch out the overlap the
    # union exists to keep.
    clip.setWindingRule_(NONZERO_WINDING)
    for axis in axes:
        _half_plane_path(axis, -axis.sign, extent, clip)
    return clip


def _fill_generated_halves(outline, axes: list, extent, color=None) -> None:
    """Wash every half of ``outline`` that ``axes`` would generate — ONCE.

    Module level and shared, not a method: a BOUND layer washes the outline it
    has, and the scan's ghost washes the outline binding would give it, and the
    ghost's whole claim is that it shows what binding would do. Two copies of
    this could drift into showing two different things and no test comparing
    strings would notice.

    Clipped rather than reconstructed: a generated side is not a closed shape on
    its own, so the filled path is drawn through the half-planes. ``-axis.sign``
    because the stored sign is the side the designer DRAWS, and the other one is
    the side being written.

    All the axes share one clip and one fill. Filling per axis composites the
    wash twice wherever two overlap, so with a vertical and a horizontal mirror
    bound together the shared quadrant came out visibly darker than either half
    alone — reading as a third state that means nothing.

    ``color`` overrides the wash outright, for the grid window: at 96 points
    there is nothing under the ink to see through, and a 22% tint over black
    would read as a smudge rather than as a half. It shares this function
    rather than clipping its own half-planes so the blue half in the window and
    the blue half on canvas cannot come to mean two different things.
    """
    if outline is None or not axes:
        return
    context = NSGraphicsContext.currentContext()
    if context is None:
        return
    context.saveGraphicsState()
    try:
        _generated_clip(axes, extent).addClip()
        (color if color is not None else MIRRORED_FILL()).set()
        outline.fill()
    except Exception:
        _print_once(traceback.format_exc())
    finally:
        context.restoreGraphicsState()


def _preview_axes(centre, shear: float, modes, signs, split=None) -> list:
    """The axes a bind would leave, for the ghost to draw.

    Carries an EMPTY ``Binding``: the wash and the axis line read only the
    centre, the mode, the shear and the sign, and the ghost has no pairing to
    hand over — nothing is bound. Building a real one would be re-deriving a
    quadratic solve on every frame for four fields nobody reads.

    ``signs`` is the ghost's own, one per mode — the same sides its outline was
    solved with, so the shaded half is the half that outline generated. It used
    to be ``BATCH_SOURCE_SIGN`` regardless, which was right for a scan-window row
    and wrong for the palette's hover: the outline followed the selection and
    the wash stayed on the batch side, shading the half the click would KEEP.
    """
    empty = azsym_binding.Binding(pairs=(), onaxis=(), counts=(), unpaired=())
    return [
        azsym_sync.Axis(
            binding=empty,
            centre=centre,
            mode=mode,
            shear=shear,
            sign=mode_sign,
            split=split,
        )
        for mode, mode_sign in zip(modes, signs, strict=True)
    ]


def _stroke_axis_line(
    points, centre, mode: str, shear: float, scale: float, split=None, width=None
) -> None:
    """The dashed axis line, in whatever colour is already set.

    Shared by the bound axes and the scan ghost so the two cannot end up with
    different dash patterns or a different idea of where the axis runs — the
    ghost's whole claim is that it shows what binding would do, and a line drawn
    a different way would undercut that.
    """
    start, end = azsym_geometry.axis_span(points, centre, mode, shear, split, width)
    path = NSBezierPath.bezierPath()
    path.setLineWidth_(AXIS_LINE_WIDTH / scale)
    # Divided by the zoom so the dash keeps its on-screen length instead of
    # turning into a solid line zoomed out and one dash zoomed in.
    path.setLineDash_count_phase_([step / scale for step in AXIS_DASH], len(AXIS_DASH), 0.0)
    path.moveToPoint_(NSPoint(start[0], start[1]))
    path.lineToPoint_(NSPoint(end[0], end[1]))
    path.stroke()


def _span_width(layer) -> float:
    """The width ``axis_span`` runs a line across: the layer's advance, or 0 for
    a path symmetry, whose line keeps to its own paths."""
    return 0.0 if isinstance(layer, _ScopedLayer) else _advance_of(layer)


def _draw_preview(layer, scale: float) -> None:
    """Ghost the selected scan row's proposal over this layer.

    Draws only — nothing is written, no undo entry is made, and the layer is
    never touched. The designer sees the outline they have and the outline
    binding would give them at the same time, so the gap between the two IS the
    "moves" number the row is reporting.

    The colour is the healthy-binding blue, so "blue means symmetry" holds
    across the whole plugin.
    """
    preview = _active_preview(layer)
    if preview is None or not preview.matches(layer):
        return
    if preview.scope is not None:
        layer = _new_scope(layer, preview.scope)
    if not preview.ghost:
        _draw_split_only(layer, preview, scale)
        return
    built = preview.outline(layer)
    if built is None:
        return
    bezier, centre, shear, paths, current = built
    axes = _preview_axes(centre, shear, preview.modes, preview.signs, preview.split)
    points = [point for path in paths for point in path]
    extent = azsym_geometry.extent_of(points, shear)
    bezier.setLineWidth_(PREVIEW_LINE_WIDTH / scale)
    # Hugo, 2026-10-10: "Don't draw anything on the model side". The half you
    # keep is the model — the bind leaves it as drawn — so the ghost leaves it
    # to Glyphs' own outline; only the half the bind would write is drawn.
    #
    # Hugo, 2026-10-10: "dim down the current side" — steady, so the ghost reads
    # clearly over it. The ghost is steady too ("Now we can remove the blinking
    # effect"): with the current outline dimmed, the two no longer need a blink
    # to be told apart.
    _dim_replaced(layer, current, axes, extent, scale)

    # The SAME three things a bound layer draws, in the same order and from the
    # same functions (Hugo, 2026-08-11): the wash over the half that would be
    # generated, then the dashed axis, then the outline. The ghost's whole claim
    # is that it shows what binding would do, so anything it drew differently —
    # or left out — would be a claim it cannot keep. The wash was the missing
    # one: a preview that showed only a blue outline said nothing about WHICH
    # half stops being yours to draw.
    #
    # Washed from the PROPOSED bezier rather than the layer's current outline,
    # so the shaded edge follows the shape binding would leave rather than the
    # one it is replacing.
    _fill_generated_halves(bezier, axes, extent)

    # Then the generated half's outline, and the axis lines, which draw in
    # "whatever colour is already set".
    color = BOUND_COLOR()
    _stroke_clipped(bezier, [lambda: _generated_clip(axes, extent)], color)
    color.set()
    # The coordinates ``outline()`` already read, not a fresh walk of the layer.
    for mode in preview.modes:
        _stroke_axis_line(points, centre, mode, shear, scale, preview.split, _span_width(layer))


def _draw_split_only(layer, preview, scale: float) -> None:
    """A preview with no ghost (``_ScanPreview.ghost``): the wash over the half
    that would be rebuilt and the dashed line, on the outline as it is."""
    paths = _structure_of(layer).paths
    if not paths:
        return
    shear = _shear_of(layer)
    points = [point for path in paths for point in path]
    centre = preview.centre
    if centre is None:
        centre = azsym_geometry.centre_of(points, shear)
    axes = _preview_axes(centre, shear, preview.modes, preview.signs, preview.split)
    _fill_generated_halves(
        _bezier_of_coords(layer, paths), axes, azsym_geometry.extent_of(points, shear)
    )
    BOUND_COLOR().set()
    for mode in preview.modes:
        _stroke_axis_line(points, centre, mode, shear, scale, preview.split, _span_width(layer))


def _dim_replaced(layer, structure, axes: list, extent, scale: float) -> None:
    """Fade the layer's current outline, handles and nodes toward the canvas on
    the half ``axes`` would replace, so the preview reads clearly over them.

    Re-drawn in the canvas colour, mark by mark, not a veil over the area: a
    filled box also covered the metric lines and alignment zones (Hugo,
    2026-10-10: "this also go above the metric line and alignment zones").

    The outline and the handle lines are clipped to the replaced half. The node
    marks are not — each is covered whole or not at all, by which side its node
    is on — so a node ON the axis, which belongs to the model, stays as drawn
    rather than being cut in half by the clip.

    ``structure`` is the layer's outline as ``_ScanPreview.outline`` already
    read it, types included.
    """
    outline = _outline_of(layer)
    context = NSGraphicsContext.currentContext()
    if outline is None or context is None or not axes:
        return
    handles = NSBezierPath.bezierPath()
    # One path per kind of mark, each stroked once at its own rim width.
    smooth, corner, offcurve = (NSBezierPath.bezierPath() for _ in range(3))
    for points, types in zip(structure.paths, structure.types, strict=True):
        for index, (point, encoded) in enumerate(zip(points, types, strict=True)):
            kind, connection = azsym_topology.decode_type(encoded)
            if kind == azsym_topology.OFFCURVE:
                # Its on-curve node: the one before it, unless that is the
                # curve's other handle — then the one after.
                before = types[index - 1] != azsym_topology.OFFCURVE
                anchor = points[index - 1] if before else points[(index + 1) % len(points)]
                handles.moveToPoint_(NSPoint(point[0], point[1]))
                handles.lineToPoint_(NSPoint(anchor[0], anchor[1]))
            if not _replaced(point, axes, extent):
                continue
            if kind == azsym_topology.OFFCURVE:
                marks, reach = offcurve, DIM_OFFCURVE_RADIUS / scale
            elif connection == azsym_topology.SMOOTH:
                marks, reach = smooth, DIM_SMOOTH_RADIUS / scale
            else:
                marks, reach = corner, DIM_CORNER_HALF / scale
            rect = ((point[0] - reach, point[1] - reach), (2 * reach, 2 * reach))
            if marks is corner:
                marks.appendBezierPathWithRect_(rect)
            else:
                marks.appendBezierPathWithOvalInRect_(rect)
    color = _canvas_color().colorWithAlphaComponent_(PREVIEW_DIM_ALPHA)
    view = NSView.focusView()
    window = view.window() if view is not None else None
    backing = window.backingScaleFactor() if window is not None else 1.0
    context.saveGraphicsState()
    try:
        nudge = NSAffineTransform.transform()
        nudge.translateXBy_yBy_(0.0, DIM_PIXEL_NUDGE / (scale * backing))
        nudge.concat()
        context.saveGraphicsState()
        try:
            _generated_clip(axes, extent).addClip()
            color.set()
            # A copy: ``bezierPath`` can be Glyphs' own cached path, and a width
            # set on it would leak into whatever else draws it.
            outline = outline.copy()
            outline.setLineWidth_(PREVIEW_DIM_WIDTH / scale)
            outline.stroke()
            handles.setLineWidth_(DIM_HANDLE_LINE_WIDTH / scale)
            handles.stroke()
        finally:
            context.restoreGraphicsState()
        color.set()
        for marks, width in (
            (smooth, DIM_SMOOTH_WIDTH),
            (corner, DIM_CORNER_WIDTH),
            (offcurve, DIM_OFFCURVE_WIDTH),
        ):
            marks.setLineWidth_(width / scale)
            marks.stroke()
    except Exception:
        _print_once(traceback.format_exc())
    finally:
        context.restoreGraphicsState()


def _replaced(point, axes: list, extent) -> bool:
    """Whether ``point`` is on a half ``axes`` would generate — beyond the axis,
    not on it."""
    return any(
        azsym_geometry.side(
            point, axis.centre, axis.mode, axis.shear, azsym_sync.split_of(axis, extent)
        )
        * axis.sign
        < -azsym_geometry.TOL
        for axis in axes
    )


def _canvas_color():
    """The Edit view's own background, not the app UI's.

    Glyphs gives the canvas its own appearance, so a dynamic colour such as
    ``textBackgroundColor`` resolves to the UI's — near black in a dark UI over
    a light canvas — and a veil meant to fade toward the canvas would darken
    it. The view being drawn is the focus view during a draw callback."""
    view = NSView.focusView()
    if view is not None and view.respondsToSelector_("canvasColor"):
        return view.canvasColor()
    return NSColor.textBackgroundColor()


def _stroke_clipped(bezier, clips: list, color) -> None:
    """Stroke ``bezier`` in ``color`` inside every clip ``clips`` builds — their
    INTERSECTION, each one narrowing the last — and leave the context as it was."""
    context = NSGraphicsContext.currentContext()
    if context is None:
        return
    context.saveGraphicsState()
    try:
        for build in clips:
            build().addClip()
        color.set()
        bezier.stroke()
    except Exception:
        _print_once(traceback.format_exc())
    finally:
        context.restoreGraphicsState()


def _preview_layer(layer, coords):
    """A throwaway copy of ``layer`` whose nodes sit at ``coords``, or None.

    THE POINT OF THE COPY: a preview must not touch the font. ``GSLayer.copy()``
    duplicates its paths and their nodes, so moving them cannot reach the layer
    the designer is looking at, and the copy is never added to ``glyph.layers``.

    ``copy()`` is real even though Glyphs 4's ``__init__.pyi`` does not declare
    it — GSLayer.h has ``@interface GSLayer : NSObject <NSCoding, NSCopying,
    ...>``, and PyObjC exposes ``-copyWithZone:`` as ``.copy()``. The type stub
    is incomplete, not the runtime. Worth stating because the failure would be
    silent: the ``except`` below would hand back None, the caller would fall
    back to the original layer, and the switch would show the same outline in
    both positions.

    ``parent`` is carried over because Glyphs 4's GlyphView asks the layer for
    its own ``glyphMetrics()``, and a layer with no parent glyph has no master
    to get an upm or an ascender from. Guarded: it is a back-pointer, not a
    registration, but a version that refuses the assignment must not cost the
    preview.

    Returns None when the copy does not line up node for node with ``coords`` —
    a layer edited between the survey and the click — because a partial
    re-point would draw a chimera of the old outline and the new one and label
    it the proposal.
    """
    try:
        twin = layer.copy()
        if twin is None:
            return None
        paths = list(twin.paths)
        if len(paths) != len(coords):
            return None
        for path, points in zip(paths, coords, strict=True):
            nodes = list(path.nodes)
            if len(nodes) != len(points):
                return None
            for node, point in zip(nodes, points, strict=True):
                node.position = NSPoint(point[0], point[1])
        try:
            twin.parent = layer.parent
        except Exception:
            pass
        return twin
    except Exception:
        _print_once(traceback.format_exc())
        return None


def _draw_text(
    text: str, point: tuple, color, scale: float, size: float = 11.0, offset=(6.0, 6.0)
) -> None:
    """Draw a label at a design-space point, at a fixed on-screen size.

    The canvas is scaled by the zoom, so the transform undoes that around the
    anchor point — otherwise the label would grow with the glyph. ``offset`` is
    in screen points from the anchor.
    """
    context = NSGraphicsContext.currentContext()
    if context is None:
        return
    context.saveGraphicsState()
    try:
        from AppKit import NSAffineTransform

        transform = NSAffineTransform.transform()
        transform.translateXBy_yBy_(point[0], point[1])
        transform.scaleBy_(1.0 / scale)
        transform.concat()
        attributes = {
            NSFontAttributeName: NSFont.systemFontOfSize_(size),
            NSForegroundColorAttributeName: color,
        }
        NSAttributedString.alloc().initWithString_attributes_(text, attributes).drawAtPoint_(
            NSPoint(*offset)
        )
    except Exception:
        _print_once(traceback.format_exc())
    finally:
        context.restoreGraphicsState()


def _drawing_suppressed() -> bool:
    """True while the furniture would be in the way, so the plugin draws nothing.

    Two signals, because they are two different situations:

    * ``Glyphs.spaceKeyDown()`` — space held down for a temporary pan. A BOOL
      property on GSApplication (``Glyphs`` IS the shared GSApplication), and
      the only thing that sees a pan that leaves the toolbar tool alone.
    * the current tool, read from ``toolDrawDelegate()`` and matched against
      ``SUPPRESSING_TOOLS`` — the Hand tool, because panning is navigation, and
      the Text tool, because setting text is not shaping an outline.

    ONE membership test rather than a check per tool, so adding a tool is a name
    in the tuple and never another branch that can fail differently.

    This hides the drawing and nothing else. The sync runs before the caller
    ever asks, so a pan cannot quietly stop the symmetry.

    Measured against a live Glyphs 4.0.1: 24 and 8 microseconds respectively,
    against a 16 ms frame — small enough to run per frame without thought.

    Every failure returns False, which shows the drawing. Furniture still on
    screen is a cosmetic annoyance; an exception here would take the whole draw
    callback down with it.
    """
    try:
        if Glyphs.spaceKeyDown():
            return True
    except Exception:
        pass
    try:
        document = Glyphs.currentDocument
        if document is None:
            return False
        tool = document.windowController().toolDrawDelegate()
        if type(tool).__name__ == TOOL_GROUP:
            # Name-compared and only then unwrapped, never probed: hasattr on a
            # bridged object scans the whole selector table, and this runs on
            # every frame. A leaf tool never takes this branch, so the Hand
            # path is exactly what it was.
            tool = tool.currentTool()
        return type(tool).__name__ in SUPPRESSING_TOOLS
    except Exception:
        return False


def _drag_in_progress() -> bool:
    """True while a mouse button is held down, so a drag may be running.

    NOT ``_drawing_suppressed()``: that reads ``Glyphs.spaceKeyDown()`` and the
    Hand tool, which is a PAN gesture. It is False throughout an ordinary node
    drag, so reusing it here would guard nothing.

    ``NSEvent.pressedMouseButtons()`` rather than the tool's own state.
    ``GlyphsToolEvent`` and ``GlyphsPathPlugin`` do declare ``@property
    (nonatomic) BOOL dragging`` — "Yes if in a dragging state" — but reaching it
    means choosing between GSWindowController's ``toolEventDelegate``,
    ``toolEventDelegateSelected`` and ``toolEventHandler`` without being able to
    check which is live mid-drag, and a third-party tool that never sets the
    flag reads as not dragging. The button state is a live hardware query
    instead: it cannot come back False while the user is dragging, and a false
    negative is the only direction that hurts here. Measured at 0.5 us, and
    only asked on a frame that changed the outline structurally.

    A failure returns False, letting the repaint through: a missed repaint is
    the defect this path exists to fix, while a repaint that should have been
    held back is one ugly frame.
    """
    try:
        return bool(NSEvent.pressedMouseButtons() & LEFT_MOUSE_BUTTON)
    except Exception:
        return False


def _request_repaint() -> None:
    """Ask the Edit views to repaint, unless a drag is running.

    Called from inside the draw callback, and only from a frame whose
    structural edits or mirrored positions actually landed. Those writes go
    through ``stopUpdateInterface``/``enableFutureUpdates``, the NON-reporting
    pair, so nothing invalidates the view once they land — and the callback is
    DRAWFOREGROUND, so the outline on screen was drawn before them. A drag hides
    that behind its next frame; a structural edit, an arrow-key nudge or a Cmd-Z
    has no next frame, and the partner stayed where it was until some unrelated
    event forced a repaint.

    This cannot loop. A structural caller sits inside the branch gated on the
    outline's signature differing from the cached one, and that cache is
    rewritten with the post-edit structure later in the same tick. A position
    caller snapshots what the layer holds after its write, so the next frame
    sees nothing moved and writes nothing. Either way the frame this schedules
    asks for nothing.

    Not repainted during a drag: a plugin-forced repaint mid-drag splits the
    canvas in Glyphs 4, and drag tracking starves the main queue for 0.5-5 s, so
    the request would land late anyway. Glyphs repaints at mouse-up itself.

    ``Glyphs.redraw()`` and never a forced ``display()``, and from a one-shot
    timer rather than at once. Asked for from inside the draw, the request was
    LOST: a view marked dirty while it draws is marked clean when the draw
    ends. Seen in Glyphs 4 on an arrow-key nudge — the wash, drawn after the
    write, moved; the outline stayed put — and the same redraw sent from
    outside a draw put it right at once. The timer fires on the next run-loop
    turn, after this draw has finished, and in the default mode only, so never
    during drag tracking either.
    """
    if _drag_in_progress():
        return
    try:
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.0, False, _redraw_now)
    except Exception:
        _print_once(traceback.format_exc())


def _redraw_now(_timer=None) -> None:
    """The deferred half of ``_request_repaint``. Never raises: it runs from a
    timer, where an exception has no caller to land in."""
    try:
        Glyphs.redraw()
    except Exception:
        _print_once(traceback.format_exc())


def _segment_enabled(mode: str, bound: set, italic: bool = False) -> bool:
    """Whether this segment may be clicked at all, given what is already on.

    Hugo, 2026-08-11: don't let rotate be used while a mirror is on, or a
    mirror while rotate is. The states are alternatives — see
    ``azsym_geometry.excluded_by`` for why the group makes them so — and a
    switch that silently turned two others off when clicked was doing something
    the designer could not see coming. Greyed out, they can see it.

    Derived from the SAME function that performs the release, so the control
    cannot offer a click the bind path would then undo.

    A bound segment is always enabled, so the only way out of a state stays
    available: also on an italic layer bound to both mirrors before only one
    was allowed there (``italic``, see ``excluded_by``). Switching the half-turn
    off re-enables both mirrors, and vice versa.

    A COMPOUND segment is asked about each symmetry it stands for, so the pair
    greys out exactly when either mirror would: with the half-turn on. Decomposed
    here rather than in ``excluded_by`` because a combination is this file's idea
    — azsym_geometry knows only the three symmetries themselves, and teaching it
    to split a name on a separator that lives in azsym_scan would tie the two
    pure modules together for one caller.
    """
    if mode in bound:
        return True
    return not any(
        other in bound
        for component in azsym_scan.combo_modes(mode)
        for other in azsym_geometry.excluded_by(component, italic)
    )


# How far a node of the model may move and still count as unmoved: the snap
# that puts an on-axis node exactly on a centre at x.5 moves it half a unit.
MODEL_DRIFT = 1.0

# How far the model may move before its switch greys, as a share of the em
# (Hugo, 2026-10-15: "the diagonal option is disable too often"). At MODEL_DRIFT
# every S, Z and z master of a test family was greyed; at 2% (20 units at
# 1000 upm) 108 of the 115 half-turn masters that pair are clickable. Between
# the two the switch is clickable and its tooltip says how far (``_axis_note``).
MODEL_DRIFT_PER_UPM = 0.02

# Points per segment when the outline in the kept half is compared before and
# after a bind. Lines are exact at any count; 24 keeps a cubic's chord error
# well under MODEL_DRIFT.
SEGMENT_SAMPLES = 24


def _segment_indices(node_types) -> list[list[int]]:
    """Each segment of one contour, as the indices of the nodes that draw it."""
    on_curve = [index for index, kind in enumerate(node_types) if kind != azsym_topology.OFFCURVE]
    count = len(node_types)
    segments = []
    for position, end in enumerate(on_curve):
        begin = on_curve[position - 1]
        span = (end - begin) % count or count
        segments.append([(begin + step) % count for step in range(span + 1)])
    return segments


def _sampled(controls: list) -> list:
    """The Bezier ``controls`` draw — a line, or a curve of any degree — as
    ``SEGMENT_SAMPLES + 1`` points, by de Casteljau."""
    # ponytail: a TrueType qcurve with several off-curves is read as one Bezier;
    # both sides of the comparison share the error.
    samples = []
    for step in range(SEGMENT_SAMPLES + 1):
        t = step / SEGMENT_SAMPLES
        points = controls
        while len(points) > 1:
            points = [
                (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
                for a, b in zip(points, points[1:], strict=False)
            ]
        samples.append(points[0])
    return samples


def _to_polyline(point, polyline: list) -> float:
    """Distance from ``point`` to the nearest piece of ``polyline``."""
    best = math.inf
    for a, b in zip(polyline, polyline[1:], strict=False):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = dx * dx + dy * dy
        along = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length if length else 0.0
        along = max(0.0, min(1.0, along))
        best = min(best, math.hypot(point[0] - a[0] - along * dx, point[1] - a[1] - along * dy))
    return best


def _outline_drift(before: list, after: list, types: Sequence, kept) -> float:
    """How far the outline drawn in the half you keep changes, segment by segment.

    Hugo, 2026-10-10, on a v bound bottom-kept: "the bottom part existing nodes
    aren't moving, but the path part in this zone change completely" — a
    segment from a kept node to a rewritten one swings through the kept half.
    Both ways round, so ink a bind ADDS to that half counts as much as ink it
    takes away. Segment against its own counterpart, because a bind only moves
    nodes: which nodes draw each segment is the same before and after.
    """
    worst = 0.0
    for path_index, node_types in enumerate(types):
        for indices in _segment_indices(node_types):
            old_controls = [before[path_index][index] for index in indices]
            new_controls = [after[path_index][index] for index in indices]
            if old_controls == new_controls:
                continue
            old, new = _sampled(old_controls), _sampled(new_controls)
            for here, there in ((old, new), (new, old)):
                for point, twin in zip(here, there, strict=True):
                    # ``twin``, the same t on the other curve, is a vertex of
                    # ``there``: no nearer than the polyline, so a point it
                    # already puts within ``worst`` cannot raise it. Skips most
                    # of the scan.
                    if kept(point) and math.dist(point, twin) > worst:
                        worst = max(worst, _to_polyline(point, there))
    return worst


@functools.lru_cache(maxsize=32)
def _impurity(
    outline: tuple,
    types: tuple,
    mode: str,
    shear: float,
    sign: int,
    guide: tuple | None = None,
    split: tuple | None = None,
) -> tuple[int, float]:
    """(nodes left unpaired, furthest the MODEL changes) if ``mode`` were bound.

    The model is the half ``sign`` keeps plus every on-axis node, which belongs
    to both halves — its nodes, and the outline drawn inside that half (see
    ``_outline_drift``). The same calls ``_bind_layer`` makes. Cached on the
    outline: the switch row asks on every interface update and pairing is
    quadratic, so only an edit pays for it.

    ``guide`` is the centre a selected guide gives (``_guide_axis``): paired
    about as given, never moved onto the on-axis nodes, exactly as the bind
    about it does. ``split`` is a typed angle's split for the half-turn
    (``_typed_split``): it decides the half kept, in place of the box diagonal.
    """
    paths = [list(path) for path in outline]
    centre = (
        guide
        if guide is not None
        else azsym_geometry.centre_of([point for path in paths for point in path], shear)
    )
    moved_as = azsym_binding.fitted(paths, centre, mode, shear, sign)
    binding = azsym_binding.pair_nodes(paths, centre, moved_as, shear, sign, split)
    if guide is None:
        axis = azsym_binding.model_centre(paths, binding, centre, moved_as, shear)
        if axis != centre:
            centre = axis
            binding = azsym_binding.pair_nodes(paths, centre, moved_as, shear, sign)
    writes = azsym_binding.mirror_source(paths, binding, centre, moved_as, shear)
    # The half-turn's split runs corner to corner across the layer's box (side),
    # or along a selected guide's line (``split``, decision 7).
    extent = (
        split
        if split is not None
        else azsym_geometry.extent_of([point for path in paths for point in path], shear)
    )
    model = set(binding.onaxis)
    for path_index, path in enumerate(paths):
        for node_index, point in enumerate(path):
            if azsym_geometry.side(point, centre, mode, shear, extent) * sign > 0:
                model.add((path_index, node_index))
    moved = max(
        (math.dist(paths[ref[0]][ref[1]], new) for ref, new in writes.items() if ref in model),
        default=0.0,
    )
    after = [list(path) for path in paths]
    for (path_index, node_index), point in writes.items():
        after[path_index][node_index] = point

    def kept(point) -> bool:
        return azsym_geometry.side(point, centre, mode, shear, extent) * sign > 0

    return len(binding.unpaired), max(moved, _outline_drift(paths, after, types, kept))


def _impure(plugin_instance, layer, mode: str, structure=None, sign: int | None = None) -> str:
    """Why binding ``mode`` on ``layer`` would not be a PURE symmetry, or "".

    Hugo, 2026-10-10: "check that, if a symmetry is applied, if the model part
    nodes pos change. If yes, that's not safe symmetry, if not let's go". So a
    switch is greyed BEFORE the click when nodes would be left unpaired, or when
    the bind would change the model — the half the selection keeps, and the
    on-axis nodes; its nodes AND the outline drawn inside it ("Don't only check
    node position") — instead of only rewriting the other half. Rewriting the
    generated half by any amount is fine: that IS the symmetry. (Greying on how
    far the generated half moves, Analyze's Safe line, was tried the same day
    and reverted: "options are disabled too easily".)

    "Change" means past ``MODEL_DRIFT_PER_UPM`` of the em, not past
    ``MODEL_DRIFT`` (Hugo, 2026-10-15: "the diagonal option is disable too
    often"). A smaller reshape is clickable and ``_axis_note`` names it.

    A selected guide (``_guide_axis``) lifts the drift gate (Hugo, 2026-10-15):
    placing it is the instruction to reshape, so only an unusable guide or an
    unpaired node greys the switch, and ``_axis_note`` puts the drift in the
    tooltip instead.

    A typed Diagonal angle lifts it the same way (decision 8): typing it is the
    instruction to reshape. ``_axis_note`` names it.

    ``structure`` and ``sign`` when the caller has read them already — the
    switch row reads the layer once for all its switches.
    """
    if structure is None:
        structure = _structure_of(layer)
    guide = _guide_axis(layer, mode, structure.paths)
    if isinstance(guide, str):
        return guide
    if sign is None:
        sign = plugin_instance._source_sign(layer, mode)
    outline = tuple(tuple(path) for path in structure.paths)
    types = tuple(tuple(node_types) for node_types in structure.types)
    split = _typed_split(layer, mode)
    unpaired, moved = _impurity(outline, types, mode, _shear_of(layer), sign, guide, split)
    if unpaired:
        return f"{unpaired} node(s) have no symmetric partner"
    if guide is None and split is None and moved > MODEL_DRIFT_PER_UPM * _upm_of_layer(layer):
        return f"the half you keep would move {moved:.0f} units"
    return ""


def _axis_note(layer, mode: str, structure, sign: int) -> str:
    """What a clickable switch's tooltip adds: the guide, the typed Diagonal
    angle, or both it would bind about, and how far the half you keep would move
    past ``MODEL_DRIFT``.

    The drift gate is lifted about either (decisions 1 and 8), and a reshape
    under ``MODEL_DRIFT_PER_UPM`` of the em passes it, so the switch can be
    clickable while the kept half moves — and that is what the designer has to
    read before clicking. Empty for a pure symmetry about the box.
    """
    guide = _guide_axis(layer, mode, structure.paths)
    centre = guide if isinstance(guide, tuple) else None
    split = _typed_split(layer, mode)
    angle = _unbound_angle(layer)
    about = []
    if centre is not None:
        about.append("the guide")
    if split is not None and angle is not None:
        about.append(f"a {_angle_text(angle)}° diagonal")
    outline = tuple(tuple(path) for path in structure.paths)
    types = tuple(tuple(node_types) for node_types in structure.types)
    # The same arguments ``_impure`` just passed, so a cache hit.
    _unpaired, moved = _impurity(outline, types, mode, _shear_of(layer), sign, centre, split)
    parts = [f"about {' and '.join(about)}"] if about else []
    if moved > MODEL_DRIFT:
        parts.append(f"kept half reshaped {moved:.0f} units")
    return f" — {', '.join(parts)}" if parts else ""


def _segment_tooltip(
    mode: str, label: str, bound: set, impure: str = "", italic: bool = False
) -> str:
    """What a segment says on hover, including why it cannot be clicked.

    A greyed control with no explanation reads as broken. Naming the symmetry
    that has to come off first turns it into an instruction.
    """
    # dict.fromkeys and not a set: both mirrors exclude the half-turn, so the
    # pair names it twice and "switch off Rotate 180° or Rotate 180° first" is
    # what a plain list gives. Order is kept because the sentence reads in the
    # order the symmetries are declared.
    blocking = list(
        dict.fromkeys(
            other
            for component in azsym_scan.combo_modes(mode)
            for other in azsym_geometry.excluded_by(component, italic)
            if other in bound and mode not in bound
        )
    )
    if blocking:
        names = " or ".join(MODE_TITLES.get(other, other) for other in blocking)
        return f"{label} — switch off {names} first"
    if impure:
        return f"{label} — {impure}"
    if mode in bound:
        return f"{label} — on, click to switch off"
    return label


def _segment_image(mode: str, label: str, side: str | None = None, enabled: bool = True):
    """One segment's icon, or None when the art cannot be loaded.

    None is what sends the caller to ``SEGMENT_FALLBACKS`` and its words. That
    used to be the ordinary path on a macOS without SF Symbols 6; now it means
    the bundle is missing its own resources, so it is a fallback that should
    never fire rather than one aimed at a host.

    ``label`` is the accessibility description — the symmetry's name, which is
    also the segment's tooltip. A bundled image has no name of its own the way
    a system symbol does, so it is set explicitly or VoiceOver reads the switch
    row as three unnamed buttons.

    ``side`` puts the swap chip beside the icon, pointing at that half, faded
    unless ``enabled`` (``_with_swap_chip``).
    """
    icon = _symmetry_icon(mode, SEGMENT_ICON_SIZE)
    if icon is None:
        return None
    image = icon if side is None else _with_swap_chip(icon, side, enabled)
    with contextlib.suppress(Exception):
        image.setAccessibilityDescription_(
            label if side is None else f"{label}: you draw the {side}"
        )
    return image


def _with_swap_chip(icon, side: str, enabled: bool):
    """``icon`` and, ``SWAP_GAP`` to its right, the chip with its triangle
    pointing at ``side``: one template, for the switch to centre and ink. Drawn
    rather than typed, because ◀ and ▶ have emoji forms."""
    left = SEGMENT_ICON_SIZE + SWAP_GAP
    margin = (TRIANGLE_HIT - TRIANGLE_SIZE) / 2.0
    corners = [NSPoint(left + margin + x, margin + y) for x, y in TRIANGLES[side]]
    share = 1.0 if enabled else TRIANGLE_DIMMED

    def draw(_rect) -> bool:
        icon.drawInRect_(((0.0, 0.0), (SEGMENT_ICON_SIZE, SEGMENT_ICON_SIZE)))
        chip = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            ((left, 0.0), (TRIANGLE_HIT, TRIANGLE_HIT)), TRIANGLE_CHIP_RADIUS, TRIANGLE_CHIP_RADIUS
        )
        NSColor.colorWithWhite_alpha_(0.0, TRIANGLE_CHIP_ALPHA * share).set()
        chip.fill()
        path = NSBezierPath.bezierPath()
        path.moveToPoint_(corners[0])
        for corner in corners[1:]:
            path.lineToPoint_(corner)
        path.closePath()
        NSColor.colorWithWhite_alpha_(0.0, share).set()
        path.fill()
        return True

    image = NSImage.imageWithSize_flipped_drawingHandler_(
        (SWAP_GROUP, SEGMENT_ICON_SIZE), False, draw
    )
    image.setTemplate_(True)
    return image


def _symmetry_icon(symmetry: str, size: float):
    """The bundled icon for one symmetry at ``size`` points square, or None.

    A FRESH NSImage every call, deliberately, and this is the one thing about
    this function worth knowing. An NSImage carries its size and its template
    flag as mutable state, so a cached instance shared between the 13pt switch
    row, the 15pt table and the 8pt grid corner would be resized by whichever
    of them drew last, and every other one would draw at the wrong size. The
    files are a few kilobytes and AppKit caches the decode behind
    ``initByReferencingFile_``, so the saving a cache would buy is not worth
    the class of bug it opens.

    SQUARE because the art is square — see SEGMENT_ICON_SIZE. ``setSize_`` does
    not preserve aspect, so a non-square box would distort a tile whose whole
    job is to be a picture of a square glyph area.

    TEMPLATE, which is what makes one white-on-alpha file work everywhere: the
    frame and the dashes are opaque and come out in the control's own colour,
    the generated half is drawn at partial alpha and stays washed back, and a
    highlighted menu row inverts the whole thing for free.

    Guarded whole. A symmetry with no art (a mode added without a file, or a
    combination this plugin does not offer) answers None rather than raising,
    and every caller has somewhere to go with that.
    """
    filename = SYMMETRY_ICON_FILES.get(symmetry)
    if filename is None:
        return None
    try:
        path = os.path.join(_ICONS_DIR, filename)
        if not os.path.isfile(path):
            return None
        image = NSImage.alloc().initByReferencingFile_(path)
        if image is None:
            return None
        image.setTemplate_(True)
        image.setSize_((size, size))
        return image
    except Exception:
        print(traceback.format_exc())
        return None


def _bound_modes(layer) -> set:
    """Which symmetries are currently bound on this layer."""
    if layer is None:
        return set()
    return {name for name, _value in _items(_read_userdata(layer, KEY) or {})}


# ------------------------------------------------------------ path symmetries


def _all_bound_modes(layer) -> set:
    """Every mode on the layer, its own and its path symmetries'."""
    return _bound_modes(layer) | {
        mode for entry in _scope_entries(layer) for mode in entry["modes"]
    }


def _scope_entries(layer) -> list[dict]:
    """The layer's path symmetries, ``{"id", "paths", "modes"}`` each, in
    stored order. An entry that cannot be read is left out."""
    entries: list[dict] = []
    try:
        raw = list(_read_userdata(layer, SCOPES_KEY) or [])
    except Exception:
        return entries
    for item in raw:
        try:
            entries.append(
                {
                    "id": str(item["id"]),
                    "paths": [int(index) for index in item["paths"]],
                    "modes": dict(_items(item["modes"])),
                }
            )
        except Exception:
            continue
    return entries


class _ScopedLayer:
    """A layer as one path symmetry sees it: only its paths, and its own store.

    Bind, release, sync and drawing read a layer through ``paths``,
    ``bezierPath``, ``userData[KEY]`` and ``copy()``; answering those for the
    subset lets them run on it unchanged. Anything else is the real layer's.

    Paths are held as objects and listed in layer order, so their indices
    follow a path added or deleted elsewhere (GSPath identity survives both,
    probed on Glyphs 3532).
    """

    def __init__(
        self, layer, entry_id: str | None, paths: list, stored: list[int] | None = None
    ) -> None:
        self.layer = layer
        self.entry_id = entry_id
        self._held = list(paths)
        # The indices last written to the store, so a move can be told.
        self.stored = stored

    def __getattr__(self, name: str) -> Any:
        return getattr(self.layer, name)

    @property
    def paths(self) -> list:
        held = {id(path) for path in self._held}
        return [path for path in self.layer.paths if id(path) in held]

    @property
    def scope(self) -> list[int]:
        """Its paths' indices in the layer."""
        held = {id(path) for path in self._held}
        return [index for index, path in enumerate(self.layer.paths) if id(path) in held]

    @property
    def scope_key(self) -> str:
        """Its id, or for one not bound yet the paths it would cover."""
        return self.entry_id or "+" + ",".join(str(index) for index in self.scope)

    @property
    def bezierPath(self) -> Any:  # noqa: N802 - GSLayer's name
        combined = NSBezierPath.bezierPath()
        for path in self.paths:
            combined.appendBezierPath_(path.bezierPath)
        return combined

    @property
    def userData(self) -> Any:  # noqa: N802 - GSLayer's name
        return _ScopeData(self)

    def copy(self) -> Any:
        twin = self.layer.copy()
        keep = set(self.scope)
        for index, path in reversed(list(enumerate(list(twin.paths)))):
            if index not in keep:
                twin.shapes.remove(path)
        return twin

    def modes(self) -> dict | None:
        for entry in _scope_entries(self.layer):
            if entry["id"] == self.entry_id:
                return entry["modes"]
        return None

    def store(self, modes) -> bool:
        """Write its modes and current paths; empty or None drops it."""
        modes = dict(_items(modes or {}))
        if modes and self.entry_id is None:
            self.entry_id = uuid.uuid4().hex[:8]
        entry = {"id": self.entry_id, "paths": self.scope, "modes": modes}
        entries = []
        found = False
        for other in _scope_entries(self.layer):
            if other["id"] == self.entry_id:
                found = True
                if modes:
                    entries.append(entry)
            else:
                entries.append(other)
        if modes and not found:
            entries.append(entry)
        return _write_userdata(self.layer, SCOPES_KEY, entries or None)


class _ScopeData:
    """``userData`` as a path symmetry sees it: ``KEY`` is its own modes, and
    nothing else is there — it owns no metrics key."""

    def __init__(self, view: _ScopedLayer) -> None:
        self._view = view

    def keys(self) -> list[str]:
        return [KEY] if self._view.modes() else []

    def __getitem__(self, key: str) -> Any:
        modes = self._view.modes() if key == KEY else None
        if not modes:
            raise KeyError(key)
        return modes

    def __setitem__(self, key: str, value) -> None:
        if key != KEY or not self._view.store(value):
            raise KeyError(key)

    def __delitem__(self, key: str) -> None:
        if key != KEY or not self._view.store(None):
            raise KeyError(key)


MIXED_PATHS = "the selected paths belong to different symmetries"
PATHS_ELSEWHERE = (
    "this layer has path symmetries: select the paths of one, or whole paths for a new one"
)


def _selection_by_path(layer) -> tuple[list[int], list[int]]:
    """(complete, touched): the paths whose every on-curve node is selected —
    handles do not count — and the paths holding any selected node."""
    complete: list[int] = []
    touched: list[int] = []
    try:
        for index, path in enumerate(layer.paths):
            nodes = list(path.nodes)
            if not any(node.selected for node in nodes):
                continue
            touched.append(index)
            if all(
                node.selected for node in nodes if _type_of(node) != azsym_topology.OFFCURVE
            ):
                complete.append(index)
    except Exception:
        return [], []
    return complete, touched


def _acting(layer) -> Any:
    """What the palette acts on for ``layer``: the path symmetry its selection
    names, else the layer itself — also when it names none, which the switch
    row greys and explains (``_sync_switches``)."""
    if layer is None or isinstance(layer, _ScopedLayer) or _PLUGIN is None:
        return layer
    target = _PLUGIN._target_of(layer)
    return layer if isinstance(target, str) else target


def _real_layer(layer) -> Any:
    return layer.layer if isinstance(layer, _ScopedLayer) else layer


def _scope_key_of(layer) -> str | None:
    return layer.scope_key if isinstance(layer, _ScopedLayer) else None


def _new_scope(layer, indices) -> _ScopedLayer:
    """A path symmetry not bound yet, over the paths at ``indices``."""
    paths = list(layer.paths)
    return _ScopedLayer(
        layer, None, [paths[index] for index in sorted(set(indices)) if 0 <= index < len(paths)]
    )


def _current_master_id(font) -> str | None:
    """The master the font is pointed at, or None if it cannot be read.

    None widens the caller to every master rather than narrowing it to nothing.
    A master id that cannot be read would otherwise match no layer at all, and
    "Show Layers using Symmetrify" would answer "none" on a file full of them —
    where showing one master too many is a longer tab and nothing worse. The
    caller says which it did, so the widening is never silent.
    """
    try:
        return str(font.masters[font.masterIndex].id)
    except Exception:
        print(traceback.format_exc())
        return None


def _bound_layers(font, master_id: str | None) -> list:
    """Every layer in ``font`` carrying a symmetry, in the font's own order.

    ``master_id`` of None takes all of them. Matched on ``_master_id``, which
    reads ``associatedMasterId``, so a brace or bracket layer counts under the
    master it belongs to rather than being dropped for having an id of its own.
    """
    found: list = []
    if font is None:
        return found
    try:
        for glyph in _bound_glyphs(font):
            for layer in glyph.layers:
                if not _all_bound_modes(layer):
                    continue
                if master_id is not None and _master_id(layer) != master_id:
                    continue
                found.append(layer)
    except Exception:
        print(traceback.format_exc())
    return found


def _bound_glyphs(font) -> list:
    """The glyphs with a symmetry stored on any of their layers, in font order.

    The whole-font walks read every layer through ``_all_layers`` here and only
    then walk ``glyph.layers`` for these few: 0.3 s through the proxy on an
    18-master file, ~0.03 s this way. A superset of what they keep — a store
    this lets through is still judged by the caller.
    """
    return [
        glyph
        for glyph in font.glyphs
        if any(
            _read_userdata(layer, KEY) or _read_userdata(layer, SCOPES_KEY)
            for layer in _all_layers(glyph)
        )
    ]


def _nothing_bound_alert() -> None:
    """Say so, rather than opening an empty window or appearing to do nothing.

    A menu command that answers with silence reads as broken. The alternative —
    validating the item so it greys itself out — would mean walking every layer
    in the file each time the Glyph menu opens, which is the same work this
    command does and is not worth doing to decide whether to offer it.

    It takes no argument any more, because it is only reached when the WHOLE
    FILE has nothing bound. A master with none of its own still opens the
    window: the master pop-up in it is the answer to "then where are they?",
    and an alert at that point would be refusing to show a window whose whole
    job is to be re-narrowed.
    """
    try:
        alert = NSAlert.alloc().init()
        alert.setMessageText_("No layers use Symmetrify")
        alert.setInformativeText_("Nothing in this file carries a symmetry.")
        alert.addButtonWithTitle_("OK")
        alert.runModal()
    except Exception:
        print(traceback.format_exc())


def _stored_sign(layer, mode: str) -> int:
    """Which half the designer chose as the drawn one when they bound ``mode``.

    Falls back to the same default ``_axes_for_layer`` uses, so a store written
    by an older build — or one that lost the field — binds the way the axes it
    draws already assume.
    """
    payload = dict(_items(_read_userdata(layer, KEY) or {})).get(mode)
    if payload is None:
        return -1
    try:
        return 1 if int(dict(_items(payload)).get("sign", -1)) > 0 else -1
    except (TypeError, ValueError):
        return -1


def _stored_split(layer, mode: str) -> tuple[float, float] | None:
    """The guide split ``mode`` was bound with (decision 7), or None for the
    box diagonal. Read back for the reason ``_stored_sign`` is: the stored sign
    names a half only on the split it was chosen on."""
    payload = dict(_items(_read_userdata(layer, KEY) or {})).get(mode)
    return None if payload is None else _point_of(dict(_items(payload)).get("split"))


# NSAlert numbers its buttons from 1000 in the order they were added. "Keep" is
# added first, so that Return — which fires the first button — is the answer
# that changes nothing. The two that DO something are behind a deliberate click.
KEEP_RESPONSE = 1000
APPLY_RESPONSE = 1001
RELEASE_RESPONSE = 1002

# How many glyphs the notice names before it stops listing and counts instead.
# A file that went through someone else's hands can come back with dozens, and
# an alert that lists them all is one nobody reads to the end of.
MAX_NAMED = 8


def _dormant_alert(dormant) -> int:
    """Put up the notice, and answer which button was pressed.

    Says what is TRUE, which is narrower than it first appears. Nothing is about
    to move: the sync writes nothing on the frame a document opens, and refuses
    every pair that is not already symmetric on the frames after it. What has
    actually happened is that these symmetries stopped being maintained, and the
    only sign of it is an axis drawn red in a glyph nobody may open.

    The three answers are the three honest things to do about that: leave it,
    force the symmetry back on and accept that outlines move, or drop the
    pairing. Only the middle one writes to an outline.

    Guarded by the caller. Returns KEEP on any host that will not show it, which
    is the answer that changes nothing.
    """
    names = sorted({glyph for glyph, _master, _modes in dormant})
    listed = ", ".join(names[:MAX_NAMED])
    if len(names) > MAX_NAMED:
        listed += f", and {len(names) - MAX_NAMED} more"
    layers = "layer" if len(dormant) == 1 else "layers"
    alert = NSAlert.alloc().init()
    alert.setAlertStyle_(NSWarningAlertStyle)
    alert.setMessageText_(f"{len(dormant)} {layers} stopped mirroring")
    alert.setInformativeText_(
        f"These glyphs carry a symmetry that no longer matches the outline, so "
        f"they were edited somewhere without AZ Symmetrify: {listed}.\n\n"
        "Opening the file changed nothing, and the nodes that fell out of step "
        "will not be pulled back — that edit is safe. They will not mirror "
        "again either, so one half of these glyphs has quietly stopped "
        "following the other. Their axes draw red. Nodes that are still in step "
        "DO go on mirroring, so a glyph can end up doing both.\n\n"
        "Keep Bindings leaves every outline and every pairing exactly as they "
        "are — the half-mirroring above is what you get.\n\n"
        "Apply Symmetries binds each of these layers again and snaps it back "
        "onto its axis. This MOVES outlines: whatever was drawn on the "
        "generated side is overwritten by the mirror of the other side. Each "
        "layer is its own undo step, so Cmd-Z takes them back one at a time.\n\n"
        "Release Them drops the pairings and leaves every outline alone."
    )
    alert.addButtonWithTitle_("Keep Bindings")
    alert.addButtonWithTitle_("Apply Symmetries")
    alert.addButtonWithTitle_("Release Them")
    return int(alert.runModal())


def _dormant_bindings(font) -> list[tuple[str, str, tuple[str, ...]]]:
    """Every layer in ``font`` carrying a binding that no longer mirrors.

    WHAT THIS IS NOT. It is not "was the plugin installed when this was last
    saved". A timestamp against the file's modification date cannot answer that
    — the userData is written before the file is, so the two never match
    exactly; a Dropbox sync, a git checkout or a plain copy rewrites the
    modification date with nobody having opened Glyphs at all; and a
    .glyphspackage is a directory with no single date to read. It also answers
    the wrong question in both directions: an edit that touched only spacing
    would warn, and an edit made with the plugin merely switched off would not.

    So the outline is asked instead of the clock. A pair that no longer mirrors
    its partner IS an edit made outside this plugin's watch, whatever tool made
    it and whenever.

    NOTHING IS ABOUT TO MOVE, and the warning must not say it is. Verified
    against the sync itself: on the first frame after a document opens there is
    no previous snapshot, so ``sync`` writes nothing and only records one; on
    the second, ``_was_symmetric`` refuses every pair that is not already
    symmetric and the axis is flagged stale instead of being "corrected". The
    structural half cannot fire either — ``reconcile`` is gated on
    ``self._structures``, which is in memory and empty until the plugin has seen
    a frame. What actually happens is quieter and worth saying: the symmetry
    stops being kept, and the only sign is an axis drawn red in a glyph the
    designer may never open.
    """
    dormant: list[tuple[str, str, tuple[str, ...]]] = []
    if font is None:
        return dormant
    try:
        for glyph in _bound_glyphs(font):
            for layer in glyph.layers:
                modes, shifted = _dormant_modes(layer)
                # Repaired on the spot rather than reported: a symmetry that
                # still holds, about an axis that only needs moving, is not
                # something to ask a designer about. See _recentre.
                for mode, centre in shifted:
                    _recentre(layer, mode, centre)
                if modes:
                    # Named by MASTER ID and not by master name, so the release
                    # can find the layer again through ``_layer_for`` — the same
                    # way the scan window re-finds the layer behind a row. Two
                    # masters may share a name; none share an id.
                    dormant.append((str(glyph.name), _master_id(layer), modes))
    except Exception:
        print(traceback.format_exc())
    return dormant


def _dormant_modes(
    layer, axes: list | None = None
) -> tuple[tuple[str, ...], tuple[tuple[str, tuple[float, float]], ...]]:
    """Sort this layer's bound symmetries into the broken and the merely moved.

    Returns ``(dormant modes, [(mode, where its axis should be)])``. The second
    list is the SAFE half of the question — the symmetries re-applying would not
    move a single node — and the caller repairs those instead of reporting them.
    ``axes`` when the caller has read them already.
    """
    if not _bound_modes(layer):
        return (), ()
    try:
        if axes is None:
            axes = _PLUGIN._axes_for_layer(layer) if _PLUGIN is not None else []
        if not axes:
            return (), ()
        paths = _paths_of(layer)
        points = [point for path in paths for point in path]
        tol = azsym_sync.guard_tol(_upm_of_layer(layer))
        dormant: list[str] = []
        shifted: list[tuple[str, tuple[float, float]]] = []
        for axis in axes:
            # Structurally stale counts as dormant whatever the geometry says:
            # node counts that no longer match make out_of_step meaningless,
            # because the stored indices point at whatever now occupies them.
            # Store keys, which a turned mirror's mode is not.
            key = azsym_geometry.switch_of(axis.mode)
            if azsym_sync.axis_is_stale(axis, paths):
                dormant.append(key)
            elif azsym_sync.out_of_step(axis, paths, tol):
                centre = azsym_geometry.centre_of(points, axis.shear)
                if azsym_sync.out_of_step(replace(axis, centre=centre), paths, tol):
                    dormant.append(key)
                else:
                    shifted.append((key, centre))
        return tuple(dormant), tuple(shifted)
    except Exception:
        print(traceback.format_exc())
        return (), ()


def _recentre(layer, mode: str, centre: tuple[float, float]) -> bool:
    """Move a stored axis onto an outline that was only ever TRANSLATED.

    THE CASE THIS EXISTS FOR IS SPACING (Hugo, 2026-08-12). Changing the left
    sidebearing rewrites every node's x, with no callback to say so — so a file
    that came back from a spacing pass made without the plugin has every
    vertical symmetry still perfectly mirrored, about an axis that stayed
    behind. ``sync`` judges pairs against the STORED centre, so it refuses all
    of them on the second frame and the symmetry dies red, having lost nothing
    but its bookkeeping. Every glyph in the file, from one spacing pass.

    Asking would be the wrong shape for it. Re-applying the symmetry here moves
    no node at all — that is precisely what distinguishes this from the dormant
    case — so there is no decision to put in front of anyone, and a notice
    listing three hundred glyphs that need nothing done to them buries the
    handful that do.

    Only the centre is rewritten. The pairing is untouched and correct: a
    translation moves every node the same way, so which node mirrors which is
    exactly what it was. The in-memory caches go with it because both are keyed
    on the layer and were built against the old axis — empty at document-open
    time, when this normally runs, and wrong rather than merely absent if this
    is ever reached later.
    """
    try:
        store = dict(_items(_read_userdata(layer, KEY) or {}))
        payload = store.get(mode)
        if payload is None:
            return False
        fields = dict(_items(payload))
        fields["centre"] = [centre[0], centre[1]]
        store[mode] = fields
        if not _write_userdata(layer, KEY, store):
            return False
        if _PLUGIN is not None:
            key = _snapshot_key(layer)
            _PLUGIN._snapshots.pop(key, None)
            _PLUGIN._structures.pop(key, None)
        return True
    except Exception:
        print(traceback.format_exc())
        return False


def _stored_shear(layer, mode: str) -> float:
    """The shear ``mode`` was bound with, or the layer's own when unreadable."""
    payload = dict(_items(_read_userdata(layer, KEY) or {})).get(mode)
    if payload is None:
        return _shear_of(layer)
    try:
        return float(dict(_items(payload)).get("shear", _shear_of(layer)))
    except (TypeError, ValueError):
        return _shear_of(layer)


def _typed_angle(layer) -> float | None:
    """The Diagonal field's angle for ``layer``, or None for Auto.

    A bound half-turn's own: read back from its stored split with the shear it
    was bound with, so an italic shows the angle that was typed. Otherwise the
    angle waiting for the glyph's next bind (``_PENDING_ANGLES``).
    """
    layer = _acting(layer)
    if azsym_geometry.ROTATE in _bound_modes(layer):
        split = _stored_split(layer, azsym_geometry.ROTATE)
        if split is None:
            return None
        # To the nanodegree: read back, 45° comes out as 44.99999999999999,
        # across the 45° rule (``guide_stands``) that chose which side is which.
        angle = azsym_geometry.split_angle(split, _stored_shear(layer, azsym_geometry.ROTATE))
        return round(angle, 9)
    return _pending_angle(layer)


def _diagonal_text(layer) -> str:
    """What the Diagonal field shows for ``layer``: its angle, or empty for Auto."""
    angle = _typed_angle(layer)
    return "" if angle is None else _angle_text(angle)


def _resplit(layer, split: tuple[float, float] | None) -> bool:
    """Give a bound half-turn a new split, or none for the box diagonal.

    Decision 8: the Diagonal field re-splits a glyph whose Rotate 180° is on.
    No node moves: the bound outline is point-symmetric about its centre, so
    every line through it pairs the same NODES. But which end of each pair
    leads changes, and the drag sync lets only the stored leading end hold an
    edit — kept as it was, the wash moved to the new line while the half it
    left clear stayed read-only (final review, 2026-10-15). So the pairing is
    re-derived about the stored centre and sign with the new split, exactly as
    the bind derived it, and stored with it. Written the way ``_recentre``
    writes a centre: userData only, the layer's caches dropped, and through
    ``_out_of_undo``, since Glyphs makes every userData write an undo step: each
    arrow press in the field was one (Hugo, 2026-10-22: "every steps are store
    in undo manager, this is anoying"). Out of undo, nothing calls the glyph's
    ``updateChangeCount:`` (GSUndoManager.h) either, so it is called here: the
    document still asks to save the new angle.
    """
    try:
        store = dict(_items(_read_userdata(layer, KEY) or {}))
        payload = store.get(azsym_geometry.ROTATE)
        if payload is None:
            return False
        fields = dict(_items(payload))
        centre = _point_of(fields.get("centre"))
        paths = _paths_of(layer)
        if centre is None or not paths:
            return False
        binding = azsym_binding.pair_nodes(
            paths,
            centre,
            azsym_geometry.ROTATE,
            _stored_shear(layer, azsym_geometry.ROTATE),
            _stored_sign(layer, azsym_geometry.ROTATE),
            split,
        )
        if binding.unpaired:
            print(
                f"[AZ Symmetrify] {MODE_TITLES.get(azsym_geometry.ROTATE)} not re-split: "
                f"{len(binding.unpaired)} node(s) have no symmetric partner."
            )
            return False
        fields["binding"] = azsym_binding.to_dict(binding)
        if split is None:
            fields.pop("split", None)
        else:
            fields["split"] = [split[0], split[1]]
        store[azsym_geometry.ROTATE] = fields
        with _out_of_undo(layer):
            written = _write_userdata(layer, KEY, store)
        if not written:
            return False
        if _PLUGIN is not None:
            key = _snapshot_key(layer)
            _PLUGIN._snapshots.pop(key, None)
            _PLUGIN._structures.pop(key, None)
        layer.parent.updateChangeCount_(NS_CHANGE_DONE)
        return True
    except Exception:
        print(traceback.format_exc())
        return False


def _enter_diagonal(layer, text: str) -> None:
    """Return in the Diagonal field: apply ``text`` and turn Rotate 180° on, as
    a click on its switch would (Hugo, 2026-10-15: "When pressing Enter in
    Diagonal textField, it should turn on Diagonal Symmetrify").

    Only when that switch is clickable (``_previewable``): with a mirror on, or
    nodes that would not pair at this angle, the angle is kept and nothing else
    happens — Enter does only what a click could. Already on, the commit
    re-splits it and it stays on. Text that is not an angle changes nothing.
    """
    if not _read_angle(text)[0]:
        return
    _commit_diagonal(layer, text)
    if _PLUGIN is not None and _previewable(azsym_geometry.ROTATE, layer):
        _PLUGIN._toggle(azsym_geometry.ROTATE, layer)


def _commit_diagonal(layer, text: str) -> str:
    """Take what was typed in the Diagonal field; return what it should show.

    Empty is Auto. A number is taken in whole degrees modulo 180
    (``_whole_degrees``), with a comma read as the point and a trailing °
    ignored. Anything else changes
    nothing, and the field goes back to what it showed. On a glyph whose Rotate
    180° is on, the angle re-splits that binding (``_resplit``); otherwise it
    waits in ``_PENDING_ANGLES`` for the glyph's next bind.
    """
    layer = _acting(layer)
    readable, angle = _read_angle(text)
    if not readable:
        return _diagonal_text(layer)
    if azsym_geometry.ROTATE in _bound_modes(layer):
        # The field commits whenever focus leaves it: clicking in and out again
        # must not rewrite the binding (final review, 2026-10-15). Compared as
        # shown, so an angle bound before whole degrees, 57.5 shown as 58, is
        # not moved to what it shows (review, 2026-10-22).
        if ("" if angle is None else _angle_text(angle)) != _diagonal_text(layer):
            shear = _stored_shear(layer, azsym_geometry.ROTATE)
            split = None if angle is None else azsym_geometry.axis_split(angle, shear)
            _resplit(layer, split)
    elif angle is None:
        _PENDING_ANGLES.pop(_glyph_key(layer), None)
    else:
        _PENDING_ANGLES[_glyph_key(layer)] = angle
    return _diagonal_text(layer)


def _read_angle(text: str) -> tuple[bool, int | None]:
    """(readable, angle) for text in the Diagonal field: empty is Auto (None),
    a number is taken in whole degrees modulo 180 (``_whole_degrees``), with a
    comma read as the point and a trailing ° ignored. Anything else is
    unreadable."""
    cleaned = text.strip().replace(",", ".").rstrip("°").strip()
    if not cleaned:
        return True, None
    try:
        value = float(cleaned)
    except ValueError:
        return False, None
    if not math.isfinite(value):
        return False, None
    return True, _whole_degrees(value)


def _whole_degrees(angle: float) -> int:
    """``angle`` in whole degrees modulo 180, half up (Hugo, 2026-10-22: "always
    round to int, don't use float"). Not ``round``, which takes 2.5 to 2."""
    return math.floor(angle + 0.5) % 180


def _auto_angle(layer) -> float | None:
    """The on-screen angle of the corner-to-corner diagonal Auto splits on:
    the box's own ``extent`` read as a split, as ``_diagonal`` reads it."""
    layer = _acting(layer)
    points = [point for path in _paths_of(layer) for point in path]
    if not points:
        return None
    shear = _shear_of(layer)
    return azsym_geometry.split_angle(azsym_geometry.extent_of(points, shear), shear)


def _step_diagonal(layer, text: str, step: float) -> str:
    """Up/Down in the Diagonal field: the angle ``step`` degrees on, applied at
    once as Glyphs' info box applies an arrow (Hugo, 2026-10-15: "adjust angle
    with up/down key by 1unit and 10unit whne shift is hold"). Steps from what
    the field reads; over unreadable text from the applied angle; from Auto,
    from this glyph's corner-to-corner angle. Returns what the field shows."""
    readable, angle = _read_angle(text)
    if not readable:
        angle = _typed_angle(layer)
    if angle is None:
        angle = _auto_angle(layer)
    if angle is None:
        return _diagonal_text(layer)
    return _commit_diagonal(layer, _angle_text((angle + step) % 180.0))



def _outline_of(layer):
    """The layer's filled shape, or None if it cannot be built.

    ``bezierPath`` rather than ``completeBezierPath``: the latter returns the
    PREVIEW shape with overlaps already unioned, which costs more and is not
    what is on screen. Nonzero winding fills overlapping contours as their union
    anyway, and reversed counters stay as holes.
    """
    try:
        return layer.bezierPath
    except Exception:
        return None


def _current_layer():
    try:
        return Glyphs.font.selectedLayers[0]
    except Exception:
        return None


def _shear_of(layer) -> float:
    """Shear factor from the master's italic angle, 0.0 when unavailable.

    A Glyphs 4 interpolation proxy can hand back a layer whose master is not
    reachable; an upright shear is the safe answer there, and it is also the
    right one for every non-italic master.
    """
    try:
        return azsym_geometry.shear_factor(float(layer.master.italicAngle))
    except Exception:
        return 0.0


def _upm_of(font) -> float:
    """The font's units per em, or the default when it cannot be asked.

    Two limits are expressed as a share of this rather than as raw unit counts —
    ``azsym_scan.safe_units`` and ``azsym_sync.guard_tol`` — so a 2048-upm file
    is judged on its own scale instead of on a 1000-upm one.

    Falls back rather than raising, and the fallback is deliberate in both
    directions: a font that cannot say how big its em is should be measured by
    the ordinary rule, not have every candidate refused (a zero cap) or every
    axis held red (a zero tolerance). A non-positive value is treated the same
    as an unreadable one — ``upm`` of 0 is not a font, it is a missing answer.
    """
    try:
        upm = float(font.upm)
    except Exception:
        return azsym_scan.DEFAULT_UPM
    return upm if upm > 0 else azsym_scan.DEFAULT_UPM


def _upm_of_layer(layer) -> float:
    """``_upm_of`` reached through a layer, for the per-frame sync.

    ``layer.parent`` is the glyph and ``glyph.parent`` is the font. Both are
    back-pointers that a Glyphs 4 interpolation proxy can leave unset, so the
    whole walk is guarded as one — a missing link at either step means the same
    thing, which is that this layer cannot name its font.
    """
    try:
        return _upm_of(layer.parent.parent)
    except Exception:
        return azsym_scan.DEFAULT_UPM


def _selected_nodes(layer) -> list:
    """Positions of the selected path nodes, ignoring anchors and components.

    Walks the paths rather than reading ``layer.selection`` directly, because
    that collection also holds anchors and components, whose positions would
    skew the which-side-is-the-source vote.
    """
    chosen: list = []
    try:
        for path in layer.paths:
            for node in path.nodes:
                if node.selected:
                    point = _read(node, _NODE_POSITION, "position")
                    chosen.append((point.x, point.y))
    except Exception:
        return []
    return chosen


def _selected_points(layer) -> set:
    """Positions of the selected path nodes, read off ``layer.selection`` once.

    Per frame, unlike ``_selected_nodes``: GlyphsApp's ``node.selected`` asks
    ``node in layer.selection``, which walks the selection for every node.
    Anything selected that is not a node (an anchor, a component, a guide) has
    no node type and is passed over.
    """
    chosen: set = set()
    try:
        for item in layer.selection or ():
            if str(getattr(item, "type", "")) in NODE_TYPES:
                point = _read(item, _NODE_POSITION, "position")
                chosen.add((point.x, point.y))
    except Exception:
        return set()
    return chosen


def _stranded_points(layer, axes: list, stale_axes) -> list:
    """Where the generated handles a selected node would strand sit
    (``azsym_sync.stranded_handles``), for every axis still mirroring."""
    selected = _selected_points(layer)
    if not selected:
        return []
    structure = _structure_of(layer)
    points = []
    for index, axis in enumerate(axes):
        if index in stale_axes:
            continue
        for path, node in azsym_sync.stranded_handles(axis, structure, selected):
            point = structure.paths[path][node]
            if point not in points:
                points.append(point)
    return points


def _selected_guides(layer) -> list:
    """The guides in the layer's selection, local and global alike.

    Read off ``layer.selection`` and never off ``guide.selected``, which reads
    False for a GLOBAL guide even while it is selected (Glyphs forum, "Global
    guide selection"). A selection that cannot be read holds no guide, which
    leaves every switch doing what it did before guides counted.
    """
    try:
        return [item for item in layer.selection if isinstance(item, GSGuide)]
    except Exception:
        return []


def _guide_axis(layer, mode: str, paths: list | None = None) -> tuple[float, float] | str | None:
    """The centre a selected guide gives ``mode``, why it cannot, or None.

    Hugo, 2026-10-15: "users could use a GSGuide, and then Symmetrify use this
    custom axis". None — no guide selected — is every switch exactly as before.
    A string is why the switch greys. A point is the centre, unsheared, from
    ``azsym_geometry.guide_centre``: the palette binds about it and lifts the
    pure-only drift gate, because placing the guide IS the instruction to
    reshape (spec 2026-10-15-guide-axis-design.md).

    Asked by the palette's own paths only — the switch row, its hover, its
    click. Analyze's batch never asks, so a guide left selected in the Edit view
    cannot move what Apply writes. No traceback when the guide cannot be read:
    this runs on every interface update, and the tooltip already says why.
    """
    # ponytail: reads layer.selection on every call, several calls per row
    # update; pass the guides through if a big selection ever shows in the time.
    guides = _selected_guides(layer)
    if not guides:
        return None
    if len(guides) > 1:
        return "select one guide"
    try:
        if paths is None:
            paths = _paths_of(layer)
        shear = _shear_of(layer)
        points = [point for path in paths for point in path]
        position = guides[0].position
        box = azsym_geometry.centre_of(points, shear)
        extent = azsym_geometry.extent_of(points, shear)
        centre = azsym_geometry.guide_centre(
            (float(position.x), float(position.y)), float(guides[0].angle), mode, shear, box
        )
    except Exception:
        return "the selected guide could not be read"
    if centre is None:
        flat = azsym_geometry.switch_of(mode) == azsym_geometry.HORIZONTAL
        return "select a horizontal guide" if flat else "select a vertical guide"
    # Final review, 2026-10-15: a half-turn sorts contours on x only, so a guide
    # 51 units above tildecomb still paired every node, and with the drift gate
    # lifted the click rewrote the lower half up to y 871. A point outside the
    # outline's box (unsheared) is never a symmetry of it. The coordinate a mirror
    # does not read is the box's own, so one test covers every switch.
    if any(abs(centre[i] - box[i]) > extent[i] / 2.0 + azsym_geometry.TOL for i in (0, 1)):
        return "the guide is outside the outline"
    return centre


# Decision 8 of the guide-axis spec: the Diagonal field's angle for each glyph
# whose Rotate 180° is off, for all its masters (``_glyph_key``). Memory only —
# never saved, because a typed angle lifts the drift gate and is meant for the
# glyph it was typed on. Cleared wholesale by ``documentClosed``.
_PENDING_ANGLES: dict[tuple, float] = {}


def _glyph_key(layer) -> tuple:
    """``_PENDING_ANGLES``' key for ``layer``'s glyph: its font and its name.

    The FONT too, unlike ``_snapshot_key``: keyed by name alone, an angle typed
    on the Roman S lifted the drift gate on the Italic S open beside it (final
    review, 2026-10-15). The font object itself is the key — a PyObjC proxy
    hashes by the object it wraps, and the dict holding it keeps it alive until
    ``documentClosed`` clears the lot.
    """
    glyph = layer.parent
    return (glyph.parent, str(glyph.name))


def _pending_angle(layer) -> float | None:
    """The angle typed in the Diagonal field for ``layer``'s glyph, or None."""
    try:
        return _PENDING_ANGLES.get(_glyph_key(layer))
    except Exception:
        return None


# The Diagonal field's LIVE preview (Hugo, 2026-10-15: "while editing value,
# show preview"): while the designer types in it, the layer it was filled for
# and the angle the text reads (None for Auto). Never written to the layer. An
# unbound half-turn reads it through ``_unbound_angle`` — so the ghost, the
# label, the tooltip and the vote all follow the text — and ``tick`` draws a
# bound one's split from it (``_with_field_split``).
#
# Its ghost has a slot of its OWN, ``_FIELD_GHOST``, and never borrows
# ``_PREVIEW``. It did, putting back what it found there when it ended — as the
# hover does — and the two were not nested: type, move onto the switches,
# Return, and each handed the other's ghost back, leaving one drawn for good
# (review, 2026-10-15). ``_active_preview`` picks which ghost is drawn.
_FIELD_PREVIEW: tuple | None = None
_FIELD_GHOST: Any = None


def _field_preview_angle(layer) -> tuple[bool, float | None]:
    """(previewing, angle): whether the Diagonal field is previewing on
    ``layer``, and the angle it reads (None for Auto)."""
    preview = _FIELD_PREVIEW
    if preview is None or not _same_layer(_real_layer(layer), preview[0]):
        return False, None
    if _scope_key_of(_acting(layer)) != preview[2]:
        return False, None
    return True, preview[1]


def _unbound_angle(layer) -> float | None:
    """The angle an unbound half-turn would bind at: the one being typed while
    the Diagonal field previews, else the one waiting (``_PENDING_ANGLES``)."""
    previewing, angle = _field_preview_angle(layer)
    return angle if previewing else _pending_angle(layer)


def _with_field_split(layer, axes: list) -> list:
    """``axes`` as the Diagonal field previews them: a bound half-turn's split
    swapped for the one being typed, for DRAWING only. The sync keeps the
    stored split until the angle is applied, so nothing pairs on a guess."""
    previewing, angle = _field_preview_angle(layer)
    if not previewing:
        return axes
    return [
        replace(
            axis,
            split=None if angle is None else azsym_geometry.axis_split(angle, axis.shear),
        )
        if axis.mode == azsym_geometry.ROTATE
        else axis
        for axis in axes
    ]


def _field_ghost(layer) -> _ScanPreview:
    """The hover's ghost for Rotate 180° at the previewed angle. Without an
    outline when that angle cannot bind (``_previewable``): the line and the
    wash still say where the split goes and which half would be rebuilt."""
    guide = _guide_axis(layer, azsym_geometry.ROTATE)
    sign = _PLUGIN._source_sign(layer, azsym_geometry.ROTATE) if _PLUGIN is not None else -1
    return _ScanPreview(
        str(layer.parent.name),
        _master_id(layer),
        (azsym_geometry.ROTATE,),
        (sign,),
        centre=guide if isinstance(guide, tuple) else None,
        split=_typed_split(layer, azsym_geometry.ROTATE),
        ghost=_previewable(azsym_geometry.ROTATE, layer),
        scope=layer.scope if isinstance(layer, _ScopedLayer) else None,
    )


def _show_field_preview(layer, angle: float | None) -> None:
    """Preview the Diagonal field's ``angle`` (None for Auto) on ``layer``."""
    global _FIELD_PREVIEW, _FIELD_GHOST
    target = _acting(layer)
    _FIELD_PREVIEW = (_real_layer(layer), angle, _scope_key_of(target))
    layer = target
    # Bound, the glyph's own wash and line show the split (``_with_field_split``),
    # and a ghost would dim the half that is no longer new.
    if azsym_geometry.ROTATE in _bound_modes(layer):
        _FIELD_GHOST = None
    else:
        _FIELD_GHOST = _field_ghost(layer)
    _rearm_hover()
    Glyphs.redraw()


def _rearm_hover() -> None:
    """Make the hover re-solve its ghost on its next poll. A switch ghost armed
    before the angle changed shows the old one, and the poll only reconsiders
    when the cursor reaches a DIFFERENT switch."""
    global _HOVER_SEGMENT
    if _HOVER_TIMER is not None:
        _HOVER_SEGMENT = -1


def _active_preview(layer):
    """The ghost to draw over ``layer``: the hover's while the cursor rests on
    a switch that ghosts, then the Diagonal field's for its glyph, then
    whatever ``_PREVIEW`` holds (a scan row's, or none)."""
    hovering = _HOVER_TIMER is not None and _PREVIEW is not _HOVER_RESTORE
    ghost = _FIELD_GHOST
    if not hovering and ghost is not None and ghost.matches(layer):
        return ghost
    return _PREVIEW


def _refresh_field_preview(layer) -> None:
    """Re-arm the Diagonal field's preview after a switch changed ``layer``: a
    click while typing binds at the previewed angle, or releases, and the
    preview has to show the glyph as it now is."""
    previewing, angle = _field_preview_angle(layer)
    if previewing:
        _show_field_preview(layer, angle)


def _clear_field_preview() -> None:
    """End the Diagonal field's preview. Nothing is put back: its ghost never
    took anyone else's slot."""
    global _FIELD_PREVIEW, _FIELD_GHOST
    if _FIELD_PREVIEW is None and _FIELD_GHOST is None:
        return
    _FIELD_PREVIEW = None
    _FIELD_GHOST = None
    _rearm_hover()
    Glyphs.redraw()


def _typed_split(layer, mode: str) -> tuple[float, float] | None:
    """The split a typed angle gives the half-turn switch, or None for Auto.

    Decision 8 (Hugo, 2026-10-15): "It should work as the diagonal symmetry,
    but user could choose themself the angle of the axis". Only for Rotate 180°
    while it is off: a bound half-turn keeps the split it stored, and a mirror
    splits on its axis. A selected guide never gives one; it places the centre
    only (``_guide_axis``).
    """
    if azsym_geometry.switch_of(mode) != azsym_geometry.ROTATE:
        return None
    if mode in _bound_modes(layer):
        return None
    angle = _unbound_angle(layer)
    if angle is None:
        return None
    try:
        return azsym_geometry.axis_split(angle, _shear_of(layer))
    except Exception:
        return None


def _split_standing(layer, mode: str) -> bool | None:
    """Whether a typed half-turn line stands, for its label; None on Auto.

    Read off the typed angle, as ``axis_split`` reads it. Re-derived from the
    unsheared split, a 42° line on a 12° italic was labelled "Left → Right"
    while its bottom was kept (final review, 2026-10-15).
    """
    angle = _unbound_angle(layer)
    if angle is None or _typed_split(layer, mode) is None:
        return None
    return azsym_geometry.guide_stands(angle)


def _angle_text(angle: float) -> str:
    """An angle as the Diagonal field and the tooltip show it: whole degrees,
    modulo 180."""
    return str(_whole_degrees(angle))


def _point_of(value) -> tuple | None:
    """A stored ``[x, y]`` back into a tuple, or None when unusable."""
    try:
        x, y = (float(value[0]), float(value[1]))
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return (x, y)


def _snapshot_key(layer) -> tuple:
    """Cache key unique to this glyph's copy of this layer: the glyph OBJECT and
    the layer's id.

    ``layer.layerId`` for a master layer is the font MASTER's id, identical
    across every glyph on that master, so the glyph has to tell them apart. The
    glyph itself, not its name: two open copies of a file hold the same name on
    the same master id, and so does an alternate renamed to the name of a glyph
    just deleted. Each read the other's last frame as its own and rewrote its
    outline and axis from it, out of undo (edge-case hunt, 2026-10-22). GSGlyph
    compares and hashes by identity (GlyphsCore 3532 and 4202 override neither
    isEqual: nor hash), as the font in ``_glyph_key`` does. A renamed glyph
    keeps its key, and its caches.
    """
    key = (layer.parent, str(layer.layerId))
    if isinstance(layer, _ScopedLayer):
        key += (layer.scope_key,)
    return key


def _raw_getter(name: str):
    """GSNode's own ObjC getter ``name``, unbound, or None where there is none.

    GlyphsApp's ``node.position``, ``.type`` and ``.connection`` properties each
    reach it through ``self.pyobjc_instanceMethods``, which builds a proxy on
    every read: 3.5 us a node, against 0.1 us for the getter itself, read live in
    Glyphs 4 (2026-10-10). ``_structure_of`` asks all three of every node on
    every frame of a drag.
    """
    try:
        return getattr(GSNode.pyobjc_instanceMethods, name)
    except Exception:
        return None


_NODE_POSITION = _raw_getter("position")
_NODE_TYPE = _raw_getter("type")
_NODE_CONNECTION = _raw_getter("connection")
# GlyphsApp's name for each raw GSNodeType, learnt from ``node.type`` itself.
_TYPE_NAMES: dict = {}


def _read(node, getter, name: str):
    """``getattr(node, name)``, through ``getter`` (``_raw_getter``) when that can
    read this node — anything it cannot, a proxy or a test's fake, takes the
    property."""
    if getter is not None:
        try:
            return getter(node)
        except Exception:
            pass
    return getattr(node, name)


def _type_of(node) -> str:
    """``str(node.type)``, read raw. Each raw value is named by GlyphsApp's own
    ``node.type`` the first time it is seen, so the two cannot disagree."""
    raw = _read(node, _NODE_TYPE, "type")
    if isinstance(raw, str):
        return str(raw)
    name = _TYPE_NAMES.get(raw)
    if name is None:
        name = _TYPE_NAMES[raw] = str(node.type)
    return name


def _paths_of(layer) -> list:
    """Node coordinates per path.

    ``node.position`` is bound to a local before its two fields are read. The
    obvious comprehension — ``(node.position.x, node.position.y)`` — asks the
    node for its position TWICE, and on a real GSNode that is two trips across
    the bridge per node rather than one, on a function the draw callback runs
    on every frame of a drag.
    """
    out = []
    for path in layer.paths:
        points = []
        for node in path.nodes:
            point = _read(node, _NODE_POSITION, "position")
            points.append((point.x, point.y))
        out.append(points)
    return out


def _structure_of(layer) -> azsym_topology.Structure:
    """The layer's outline as the pure modules see it, read in ONE walk.

    ``_paths_of`` and ``_types_of`` each cross the bridge for every path and
    every node, and ``_run_sync`` wants both halves on every frame of a drag —
    so the outline was being read twice per frame to build the two halves of one
    object. Walking once and filling both together halves that. The two
    functions stay for the callers that genuinely want only one of them.
    """
    paths = []
    types = []
    for path in layer.paths:
        # Materialised once: ``path.nodes`` is fetched across the bridge, and
        # the two loops below would otherwise fetch it twice.
        nodes = list(path.nodes)
        points = []
        encoded = []
        for node in nodes:
            point = _read(node, _NODE_POSITION, "position")
            points.append((point.x, point.y))
            encoded.append(azsym_topology.encode_type(_type_of(node), _connection_of(node)))
        paths.append(points)
        types.append(encoded)
    return azsym_topology.Structure(paths=paths, types=types)


def _scan_of(layer, upm: float) -> azsym_scan.LayerScan:
    """Analyze's measure of one layer, with its node types: a handle counts by
    how far it moves the curve, not by its own move (issue #3)."""
    structure = _structure_of(layer)
    offcurve = [[kind == azsym_topology.OFFCURVE for kind in path] for path in structure.types]
    return azsym_scan.scan_layer(structure.paths, _shear_of(layer), upm=upm, offcurve=offcurve)


def _holds_components(layer) -> bool:
    """True when the layer has at least one component — or cannot be asked.

    ``layer.components`` is a ``GSProxyShapes``, declared in GSProxyShapes.h as
    ``NSObject <NSFastEnumeration>``. It is NOT an NSArray and its interface
    declares no ``count``; GlyphsApp's Python layer then installs ``__len__`` as
    a PROPERTY rather than a method, so ``len()`` on it RAISES instead of
    counting. Iterated and short-circuited on the first element instead — the
    same way ``_paths_of`` already walks ``layer.paths``, which is the identical
    class.

    A proxy that cannot be read answers True, and that direction is deliberate.
    The caller uses this to decide whether to assert a centring on bounds that
    include components; not knowing what the layer holds is not a licence to
    claim anything about it.
    """
    try:
        for _component in layer.components:
            return True
    except Exception:
        return True
    return False


def _has_no_outline(layer) -> bool:
    """True when the layer holds no path at all — nothing left to mirror.

    Iterated and short-circuited rather than counted, for the reason
    ``_holds_components`` spells out: ``layer.paths`` is the same GSProxyShapes
    class, and ``len()`` on it RAISES.

    A proxy that cannot be read answers FALSE, which is the opposite direction
    to ``_holds_components`` and deliberately so. The caller releases every
    symmetry on a True, so a failed read answering True would throw the
    designer's binding away on the strength of an exception. Refusing to act is
    the safe half of this one.

    COMPONENTS ARE NOT COUNTED. A composite whose parts were decomposed and
    then deleted is as empty as any other layer, and a layer that still holds
    components holds nothing this plugin ever paired: a binding pairs NODES.
    """
    try:
        for _path in layer.paths:
            return False
    except Exception:
        return False
    return True


def _master_layers(glyph, order: dict[str, int]) -> tuple[list, int]:
    """This glyph's master layers, and how many layers could not be classified.

    ``glyph.layers`` also yields brace and bracket layers, which are alternates
    of a master rather than a master, and whose symmetry is the master's
    question rather than their own. A master layer is the one whose ``layerId``
    IS its ``associatedMasterId``; every other layer carries a UUID of its own
    and points at the master it belongs to.

    A layer that cannot be classified is COUNTED, not guessed at in either
    direction. Including it would put a brace layer in the report under the same
    name as its master, and dropping it silently would shrink the file the scan
    claims to have covered.

    In Font Info order, from ``order`` (``_master_order``, read once by the
    caller): ``_all_layers`` hands them over in the order they are stored.
    """
    found = []
    unclassified = 0
    try:
        candidates = _all_layers(glyph)
    except Exception:
        return [], 1
    for layer in candidates:
        try:
            layer_id = str(layer.layerId)
            if layer_id == str(layer.associatedMasterId):
                found.append((order.get(layer_id, len(order)), layer))
        except Exception:
            unclassified += 1
    found.sort(key=lambda entry: entry[0])
    return [layer for _position, layer in found], unclassified


def _all_layers(glyph) -> list:
    """Every layer ``glyph`` holds, masters and alternates alike, as stored.

    ``values()`` is the array behind GlyphsApp's layer proxy. Iterating the
    proxy instead asks the font for each master afresh: ~20 us a layer, 0.3 s
    of every whole-font walk on an 18-master file, against a tenth of that.
    """
    try:
        return list(glyph.layers.values())
    except Exception:
        return list(glyph.layers)


# Glyphs per call into the native walk: small enough that the progress sheet
# moves several times a second, large enough that the calls cost nothing.
WALK_CHUNK = 50


def _master_table(font, order: dict[str, int]) -> list[tuple[str, str | None, float]]:
    """``(id, name, shear)`` per master in Font Info order — what the native walk
    needs to answer ``_master_name`` and ``_shear_of`` for every layer.

    Read once per survey, with the guards those two put around each read: a
    name that cannot be read is None (the walk then falls back to the layer's own
    name, as ``_master_name`` does) and an angle that cannot be read is no slant.
    """
    by_id = {}
    try:
        for master in font.masters:
            by_id[str(master.id)] = master
    except Exception:
        pass
    table = []
    for master_id in sorted(order, key=order.__getitem__):
        # None for an id the masters did not answer: the reads below then fail into
        # their guards, as _master_name's and _shear_of's do.
        master: Any = by_id.get(master_id)
        try:
            name = str(master.name)
        except Exception:
            name = None
        try:
            shear = azsym_geometry.shear_factor(float(master.italicAngle))
        except Exception:
            shear = 0.0
        table.append((master_id, name, shear))
    return table


def _walked_master_name(masters: list, walked) -> str:
    """``_master_name`` for a layer the native walk read: its master's name, then
    the layer's own, then its id — the first neither empty nor "None", in the
    order and by the test ``_master_name`` applies."""
    first = masters[walked.master][1] if walked.master >= 0 else None
    for name in (first, walked.layer_name, walked.layer_id):
        if name and name != "None":
            return name
    return "?"


def _native_survey(font, order: dict[str, int], upm: float, progress) -> azsym_scan.FontScan | None:
    """The whole survey in the native engine, or None when it cannot take this font.

    None for anything that is not a real Glyphs font (the tests' fakes have no
    ObjC pointer) and for a Glyphs whose selectors the engine does not
    recognise; ``_survey`` then reads the outlines itself, which is correct and
    about four times slower. Reads and counts exactly as ``_survey`` does — the
    parity macro (native/macros/check_survey.py) compares the two, finding by
    finding.
    """
    pointer = getattr(font, "__c_void_p__", None)
    if pointer is None or azsym_native.ERROR is not None:
        return None
    masters = _master_table(font, order)
    walk = azsym_native.walk_begin(
        pointer().value,
        [(master_id, shear) for master_id, _name, shear in masters],
        upm,
        KEY,
        azsym_scan.NODE_CEILING,
        azsym_scan.COMBOS,
    )
    if walk is None:
        return None
    findings: list[azsym_scan.LayerFinding] = []
    unreadable = glyphs = 0
    try:
        for start in range(0, walk.glyph_count, WALK_CHUNK):
            for glyph in walk.glyphs(start, WALK_CHUNK):
                glyphs += 1
                unreadable += glyph.unclassified
                for layer in glyph.layers:
                    if layer is None:
                        unreadable += 1
                        continue
                    findings.append(
                        azsym_scan.LayerFinding(
                            glyph=glyph.name,
                            master=_walked_master_name(masters, layer),
                            scan=azsym_scan.scan_from_native(layer.scan),
                            components=layer.components,
                            bound=tuple(sorted(set(layer.bound))),
                            master_id=layer.layer_id,
                            master_index=order.get(layer.layer_id, 0),
                        )
                    )
                if progress is not None:
                    progress(len(glyph.layers))
    finally:
        walk.close()
    return azsym_scan.FontScan(
        findings=tuple(findings), glyphs=glyphs, unreadable=unreadable, upm=upm
    )


def _master_id(layer) -> str:
    """The id of the master this layer belongs to, or "" if it cannot be read.

    ``associatedMasterId`` and not ``layerId``: they are the same string on a
    master layer, which is all this scan visits, but the associated id is the
    one that means "the master" rather than "this layer" — so a row still points
    at the right master if the scan is ever widened to brace layers.
    """
    try:
        return str(layer.associatedMasterId)
    except Exception:
        return ""


def _master_name(layer) -> str:
    """What the designer calls this layer's master.

    Falls back through the layer's own name to its id, because the report is
    a list of places to go and look — a row the designer cannot find in the font
    is worse than a row with an ugly name.
    """
    for read in (
        lambda: layer.master.name,
        lambda: layer.name,
        lambda: layer.layerId,
    ):
        try:
            name = str(read())
        except Exception:
            continue
        if name and name != "None":
            return name
    return "?"


def _connection_of(node) -> str:
    """One node's connection as an ``azsym_topology`` name.

    ``GSNode.connection`` rather than ``GSNode.smooth``: the ObjC property is
    the GSNodeConnection value itself, so super-smooth survives the trip.
    ``smooth`` is a BOOL that cannot tell smooth from super-smooth, and whose
    setter writes plain smooth — a lossy pair in both directions.

    The int check is not defensive padding. ``GSNode.connection`` is a Python
    PROPERTY that GlyphsApp installs over the ObjC one; on a host that lacks it,
    PyObjC bridges the ObjC getter instead and ``node.connection`` is a bound
    METHOD. That raises nothing and hashes fine, so a dict lookup on it would
    quietly return sharp for every node in the font — the exact silent failure
    this task exists to remove. Anything that is not the enum value falls
    through to the boolean instead.

    Falls back to ``smooth`` for a host that does not expose ``connection``, and
    to sharp when neither can be read. Sharp is what a fresh GSNode already is,
    so a failure here costs the connection and nothing else; raising instead
    would take down the draw callback this runs inside, and with it the
    mirroring.
    """
    try:
        value = _read(node, _NODE_CONNECTION, "connection")
        if isinstance(value, int):
            return CONNECTION_NAMES.get(value, azsym_topology.SHARP)
    except Exception:
        pass
    try:
        return azsym_topology.SMOOTH if node.smooth else azsym_topology.SHARP
    except Exception:
        return azsym_topology.SHARP


def _apply_type(node, encoded: str) -> None:
    """Assign one encoded type string — type AND connection — onto a GSNode.

    Both halves, always. A fresh ``GSNode()`` is sharp, so a mirrored node given
    only its type lands sharp whatever the node it copies: that is the bug this
    exists to fix, and splitting the two assignments between call sites is how
    it would come back.

    ``node.connection`` takes the GSNodeConnection value. The fallback assigns a
    real ``bool`` and not a truthy value, because GlyphsApp's ``smooth`` setter
    tests ``value is True`` and would read anything else as sharp.

    The type is written only when it actually differs, because the write can
    only LOSE information on an existing node. ``__GSNode_get_type__`` has no
    branch for GSNodeTypeQuarticCurve (38) or GSNodeTypeSpecial (101), both
    declared in GSBase.h, so either reads back as ``"line"`` and assigning that
    string writes GSLINE_. A RETYPE_NODE edit re-encodes the type it just read
    off the very node it names, so on a quartic partner the round trip would
    silently convert it to a line. Skipping the no-op write is the whole fix:
    the connection, which is what a retype is for, is still assigned every time.
    """
    node_type, connection = azsym_topology.decode_type(encoded)
    if str(node.type) != node_type:
        node.type = node_type
    value = CONNECTION_VALUES.get(connection, GSSHARP)
    try:
        node.connection = value
    except Exception:
        node.smooth = bool(value != GSSHARP)


def _types_of(layer) -> list[list[str]]:
    """Node types per path, as plain strings the pure modules can compare.

    ``GSNode.type`` is a bridged constant; ``str()`` makes it comparable and
    keeps azsym_topology free of any Glyphs import.

    The node's CONNECTION is encoded into the same string rather than collected
    into a list of its own, so ``Structure`` keeps its two parallel sequences and
    every function that walks types alongside points carries the connection for
    free. Two things follow: a mirrored node can be given the connection of the
    node it copies, and toggling a node between smooth and sharp — an edit that
    moves nothing, and which position comparison therefore cannot see at all —
    changes the signature and reaches the classifier as a TYPE_CHANGED.
    """
    return [
        [azsym_topology.encode_type(_type_of(node), _connection_of(node)) for node in path.nodes]
        for path in layer.paths
    ]


@contextlib.contextmanager
def _out_of_undo(layer) -> Iterator[None]:
    """A userData write from the draw callback: no interface report and no undo
    step. Glyphs 3532 makes every userData write an undo step of its own
    (probed 2026-10-20), and one pushed here lands on top of the user's edit:
    Cmd-Z took back the axis a move had carried and left the outline moved,
    so the axis read "re-bind" (Hugo, 2026-10-20)."""
    font = layer.parent.parent
    undo = layer.parent.undoManager()
    font.stopUpdateInterface()
    try:
        if undo is not None:
            undo.disableUndoRegistration()
        try:
            yield
        finally:
            if undo is not None:
                undo.enableUndoRegistration()
    finally:
        font.enableFutureUpdates()


def _apply(layer, writes: dict, undoable: bool = False) -> dict:
    """Write mirrored positions back, and report what the layer actually kept.

    Returns ``{node ref: position}`` read back from the nodes AFTER assignment.
    ``GSNode.position`` is not stored verbatim — it aligns to the layer grid —
    so the caller must snapshot the read-back value, never the requested one.

    Each guard is nested inside its own try/finally so a mid-write exception —
    or a nil undo manager, which GSGlyph.undoManager is declared to allow in
    both Glyphs 3 and 4 — cannot skip the matching re-enable and leave the
    update-interface counter sinking a little further on every dragged frame.
    ``_apply`` runs inside the draw callback on every frame of a drag, so the
    pair used is ``stopUpdateInterface``/``enableFutureUpdates`` — the
    non-reporting pair — rather than ``disableUpdateInterface``/
    ``enableUpdateInterface``, whose enable call REPORTS the collected changes
    and would fire a plugin-originated interface update mid-drag.

    ``undoable`` is False for those per-frame writes: a drag would otherwise
    push one undo record per frame. The bind-time snap passes True, because it
    moves a whole side of the outline without the user dragging anything and
    Cmd-Z has to bring the artwork back.
    """
    font = layer.parent.parent
    undo = layer.parent.undoManager()
    actual: dict = {}
    font.stopUpdateInterface()
    try:
        if undo is not None and not undoable:
            undo.disableUndoRegistration()
        try:
            # ``list()`` and not the proxy itself. ``layer.paths`` is a
            # GSProxyShapes, and ``_has_no_outline`` records that ``len()`` on
            # that class RAISES — GlyphsApp installs ``__len__`` as a property
            # rather than a method. Materialising it first makes the bounds
            # check below a plain list index, which is what ``_preview_layer``
            # already does for the same reason.
            paths = list(layer.paths)
            # Node lists are fetched per PATH, not per write. Each ``.nodes`` is
            # a fresh proxy across the bridge, and a mirrored half asks for the
            # same path once per node it writes — sixty times a second, on the
            # frame budget this whole file is arranged around.
            nodes_by_path: dict[int, list] = {}
            for (path_index, node_index), point in writes.items():
                if path_index < len(paths):
                    nodes = nodes_by_path.get(path_index)
                    if nodes is None:
                        nodes = list(paths[path_index].nodes)
                        nodes_by_path[path_index] = nodes
                    if node_index < len(nodes):
                        nodes[node_index].position = NSPoint(point[0], point[1])
                        stored = nodes[node_index].position
                        actual[(path_index, node_index)] = (stored.x, stored.y)
        finally:
            if undo is not None and not undoable:
                undo.enableUndoRegistration()
    finally:
        font.enableFutureUpdates()
    return actual


def _new_contour(edit: azsym_sync.StructuralEdit) -> GSPath:
    """The twin contour an ADD_CONTOUR edit describes, as a real ``GSPath``.

    Closed, because every contour the pure modules reason about is treated as
    closed — ``pair_nodes`` says so in as many words — and an open twin of a
    closed source would not draw the same shape.

    Nodes are built empty and then assigned, exactly as the mirrored-node path
    does: ``GSNode()`` takes positional arguments on build 4004.0, but assigning
    ``.position`` and ``.type`` works on every build.

    Types go through ``_apply_type``, which sets the connection as well — a twin
    of a smooth contour drawn entirely of sharp corners is a different shape.
    """
    path = GSPath()
    path.closed = True
    for point, node_type in zip(edit.points, edit.node_types, strict=True):
        node = GSNode()
        node.position = NSPoint(point[0], point[1])
        _apply_type(node, node_type)
        path.nodes.append(node)
    return path


def _open_step(layer) -> bool:
    """Whether the designer's undo step for the edit in hand is still open on
    ``layer``'s glyph, so an edit made now joins it (``AZSymmetry._reconcile``)."""
    undo = layer.parent.undoManager()
    return (
        undo is not None
        and undo.groupingLevel() > 0
        and not undo.isUndoing()
        and not undo.isRedoing()
    )


def _glyph_of_undo(manager):
    """The glyph a GSUndoManager keeps the steps of; None for any other undo
    manager, a text field's or a window's, which has no ``glyph``."""
    try:
        return manager.glyph()
    except Exception:
        return None


def _apply_structure(
    layer, edits: tuple[azsym_sync.StructuralEdit, ...], undoable: bool = False
) -> bool:
    """Apply ``reconcile()``'s structural edits to the real outline.

    Mirrors ``azsym_sync._with_edits`` exactly: the edits are applied
    SEQUENTIALLY, in the order given, one after the other. Each edit's index
    is read against the outline as it stands once every edit before it has
    already landed — not against the outline this function started with, not
    sorted, not grouped by kind, deletes never hoisted ahead of inserts. A
    curve split's three inserts arrive at consecutive ascending indices for
    exactly this reason; applying them any other way is a curve that doubles
    back. Diverging from ``_with_edits`` here derives every later frame's
    pairing against an outline that this function never actually produces.

    An unrecognised kind is skipped, never treated as an insert — the same
    rule ``_with_edits`` follows, for the same reason: guessing the wrong
    thing is worse than doing nothing.

    RETYPE_NODE is the one kind that moves nothing: it carries a node's whole
    encoded type, connection included, and is how a smooth/sharp toggle reaches
    the generated half at all.

    The two contour kinds go through ``layer.shapes``, not ``layer.paths``:
    ``layer.paths`` is a read-only ``GSProxyShapes`` in Glyphs 4 and appending
    to it raises. ``LayerShapesProxy.append`` calls ``-[GSLayer addShape:]``
    and ``.remove`` calls ``-[GSLayer removeShape:]``, both declared in
    GlyphsCore's GSLayer.h. Removal names the path OBJECT rather than an index,
    because a layer's shapes can hold components too and the shape index of a
    path is not its path index. ``layer.paths`` is re-read for EVERY edit rather
    than cached once, because adding or removing a contour changes the very list
    a cached proxy would be describing.

    Same non-reporting update guard as ``_apply``, in a try/finally so a
    mid-write exception cannot skip ``enableFutureUpdates`` and leave the
    counter sunk.

    Undo registration is off, through the same two calls ``_apply`` uses, once
    the designer's step has closed, for a reason measured in Glyphs 4.0.1.
    Leaving it on then does NOT enrol these edits in the user's own undo
    record: it pushes a SECOND, later entry. The first Cmd-Z then popped the
    plugin's edit — restoring the partner node while the user's own node stayed
    deleted — and since that partner sits on the generated half, the next tick
    removed it again, so the undo appeared to do nothing at all. With
    registration off, Cmd-Z brings the user's node back and the next tick
    mirrors it again. That holds for an insert, but not for a delete: Glyphs
    puts a deleted node back by INDEX (probed, Glyphs 4202), and a partner
    removed before it on the path has shifted the indices.

    ``undoable`` while the designer's undo step is still open
    (``AZSymmetry._reconcile``): registration stays on and the edits join that
    step, which is the only way Cmd-Z can put a deleted node back where it was.

    The manager is allowed to be nil, exactly as in ``_apply``:
    ``GSGlyph.undoManager`` is declared nullable in both Glyphs 3 and 4. The
    re-enable sits in its own finally, because leaving registration disabled
    after a raising edit would silently make every LATER edit the user makes
    un-undoable, for the rest of the session.
    """
    if not edits:
        return False
    font = layer.parent.parent
    undo = layer.parent.undoManager()
    changed = False
    font.stopUpdateInterface()
    try:
        if undo is not None and not undoable:
            undo.disableUndoRegistration()
        try:
            for edit in edits:
                if edit.kind == azsym_sync.ADD_CONTOUR:
                    layer.shapes.append(_new_contour(edit))
                    changed = True
                    continue
                if edit.kind == azsym_sync.REMOVE_CONTOUR:
                    # Materialised for the reason ``_apply`` gives: ``len()`` on
                    # the raw GSProxyShapes raises.
                    paths = list(layer.paths)
                    if edit.path < len(paths):
                        layer.shapes.remove(paths[edit.path])
                        changed = True
                    continue
                nodes = layer.paths[edit.path].nodes
                if edit.kind == azsym_sync.DELETE_NODE:
                    if edit.index < len(nodes):
                        del nodes[edit.index]
                        changed = True
                elif edit.kind == azsym_sync.MOVE_NODE:
                    if edit.index < len(nodes):
                        nodes[edit.index].position = NSPoint(edit.position[0], edit.position[1])
                        changed = True
                elif edit.kind == azsym_sync.INSERT_NODE:
                    node = GSNode()
                    node.position = NSPoint(edit.position[0], edit.position[1])
                    _apply_type(node, edit.node_type)
                    nodes.insert(edit.index, node)
                    changed = True
                elif edit.kind == azsym_sync.RETYPE_NODE:
                    if edit.index < len(nodes):
                        _apply_type(nodes[edit.index], edit.node_type)
                        changed = True
        finally:
            if undo is not None and not undoable:
                undo.enableUndoRegistration()
    finally:
        font.enableFutureUpdates()
    return changed


def _apply_undoable(layer, writes: dict, action_name: str) -> dict:
    """``_apply`` inside one named undo group, so Cmd-Z restores the artwork.

    Guarded for a nil undoManager exactly as ``_apply`` is: GSGlyph.undoManager
    is declared to allow nil in both Glyphs 3 and 4. Without a manager the
    writes still happen — they are simply not undoable, which is the same
    outcome the previous code had unconditionally.

    Naming the group is guarded SEPARATELY from closing it: sharing one ``try``
    means a raise while naming skips the close, and a group left open on a live
    document breaks Cmd-Z for the rest of the session. Losing the label costs
    the user a nameless undo entry; losing the close costs them undo.
    """
    undo = layer.parent.undoManager()
    if undo is None:
        return _apply_and_sharpen(layer, writes)
    undo.beginUndoGrouping()
    try:
        return _apply_and_sharpen(layer, writes)
    finally:
        try:
            undo.setActionName_(action_name)
        except Exception:
            print(traceback.format_exc())
        undo.endUndoGrouping()


def _apply_and_sharpen(layer, writes: dict) -> dict:
    """``_apply``, then make sharp any smooth node the writes left kinked — in
    the caller's undo step, so Cmd-Z restores both (see ``_sharpen_kinks``)."""
    actual = _apply(layer, writes, undoable=True)
    if actual:
        _sharpen_kinks(layer, _structure_of(layer), actual, undoable=True)
    return actual


def _sharpen_kinks(layer, structure, near, undoable: bool) -> list | None:
    """Make sharp every smooth node at or next to ``near`` whose handles no
    longer line up (``azsym_topology.kinked_smooth``). Returns ``structure``'s
    types with those nodes sharp, or None when there were none.

    Never the other way: a sharp node whose handles happen to line up stays a
    corner — that is the designer's call, and nothing here broke it.

    ``undoable`` False for the per-frame sync, for the reason ``_apply`` gives.
    """
    kinked = azsym_topology.kinked_smooth(structure, near)
    if not kinked:
        return None
    types = [list(node_types) for node_types in structure.types]
    changes = {}
    for path_index, node_index in kinked:
        kind, _connection = azsym_topology.decode_type(types[path_index][node_index])
        changes[(path_index, node_index)] = azsym_topology.encode_type(kind, azsym_topology.SHARP)
        types[path_index][node_index] = changes[(path_index, node_index)]
    _set_types(layer, changes, undoable)
    return types


def _set_types(layer, changes: dict, undoable: bool) -> None:
    """Write encoded node types onto the layer's nodes, by ``(path, node)``.

    ``undoable`` False for the per-frame sync, for the reason ``_apply`` gives.
    Glyphs records a connection change for undo on its own (read from the
    running Glyphs 4: one set inside a named group came back off the stack with
    Cmd-Z), so True needs no registration of ours — only an open group.
    """
    font = layer.parent.parent
    undo = None if undoable else layer.parent.undoManager()
    font.stopUpdateInterface()
    try:
        if undo is not None:
            undo.disableUndoRegistration()
        try:
            paths = list(layer.paths)
            for (path_index, node_index), encoded in changes.items():
                _apply_type(list(paths[path_index].nodes)[node_index], encoded)
        finally:
            if undo is not None:
                undo.enableUndoRegistration()
    finally:
        font.enableFutureUpdates()


def _write_right_metrics_key(layer, value: str | None) -> None:
    """Set or clear the LAYER's own right metrics key.

    ``layer.rightMetricsKey``, never ``glyph.rightMetricsKey``: the glyph
    property applies to every master, and a binding belongs to one layer. The
    layer key overrides the glyph one while it is set, and the glyph's own key
    re-emerges by itself the moment it is cleared — so this never has to touch
    the glyph at all.

    ``None`` clears it. GlyphsApp's property setter runs the value through
    ``NSStr``, which maps a falsy string to nil, so the empty string clears it
    too; None is the honest spelling.

    Setting a key does NOT move anything. Nothing in Glyphs syncs metrics keys
    on its own — the only callers are Glyph > Update Metrics, the inspector's
    per-side buttons and component auto-alignment — so this writes an intent and
    the designer decides when to apply it. That is on purpose: syncing would
    make the plugin own ``layer.width``, and one branch of ``syncRightMetrics``
    moves the OUTLINE, which would slide the layer out from under the frozen
    axis and drag the whole shape back on the next tick.

    No undo guard of its own: the key goes wherever the binding's store goes.
    That store is an undo step (probed on Glyphs 3532, 2026-10-20), so a click's
    bind or release is one Cmd-Z, key included, and a write from the draw
    callback is kept out of undo by ``_out_of_undo``. With the key out of undo,
    Cmd-Z of a bind left "==|" on the released layer and undid the saved key.
    """
    layer.rightMetricsKey = value


def _bind_metrics_key(layer, mode: str, unpaired: bool) -> bool:
    """Point the right sidebearing at the left one, for a symmetry that says so.

    Runs from the MENU action and not from the draw callback, so it carries no
    ``stopUpdateInterface`` guard — the rest of ``_bind`` does not either.

    "==|" is a CLAIM: that the middle of the advance sits on the drawn axis.
    Four separate things can make that claim false, and all four are refused
    here rather than guessed at, because a wrong key silently re-spaces the
    glyph on the designer's next Update Metrics and nothing on screen says why.

    Refused on an ITALIC master, and this is the one part of the feature that is
    not a typographic judgement. Glyphs measures LSB and RSB off the plain
    upright bounding box, while this plugin's axis is the bounding-box centre in
    the UNSHEARED frame. Upright those are the same point — measured identical
    on every real outline in the test suite — so "==|" puts the middle of the
    advance exactly on the drawn axis. Sheared they are not: on the same
    fixtures at 12 degrees the two are 73 units apart on A and 64 on T. The key
    would assert a centring the binding does not have, and the first Update
    Metrics after that would move the width by -121 and +172 units.

    Refused on a layer holding COMPONENTS, for the same reason one step over.
    The sidebearings are measured off ``layer.bounds``, and GSLayer.h says of it
    "The bounds include paths and components" — corroborated by
    ``-[GSLayer LSBPlain]``, which aligns the components before it measures.
    This plugin's axis comes from ``_paths_of``, which reads ``layer.paths`` and
    nothing else. On a mixed layer the two are different measurements of
    different shapes: a path spanning x 100..400 gives an axis at 250 while a
    component out to x 900 puts the bounding-box centre at 500, and the next
    Update Metrics moves the width by 500 units.

    Refused when the binding left nodes UNPAIRED. Those nodes are never
    written, so the outline is not symmetric about the stored centre; an
    unpaired node at an x-extreme puts the ink's two edges at different
    distances from the axis, which is precisely what the key says they are not.

    Refused on a ZERO-WIDTH layer (Hugo, 2026-10-15, tildecomb). A combining
    mark is placed by its anchors and keeps no advance; "==|" sets RSB := LSB,
    and the sync after a keyed bind would give tildecomb, inked from x 155 to
    453, a width of 608.

    A key already set is left exactly as it is (issue #2, schriftgestalt: "Why
    not just leave it as it is?"): the layer's own, the designer's own "==|"
    included, and the glyph's, which a layer key would override on this master.
    Ours goes only where there is no key. The empty string is saved beside it,
    so the release knows the key is ours to clear, and a "==|" with that entry
    is ours already and kept for a second mode. A non-empty saved value comes
    only from an older build, which overwrote keys; the release still gives it
    back.

    The mode is then recorded in METRICS_KEY_OWNERS, so a later release can ask
    which bindings actually earned the key rather than which ones merely could
    have.

    RETURNS whether the key was set, so the caller knows whether there is
    anything for ``_sync_metrics`` to apply. Every ``return False`` below is a
    guard that has just explained itself on the console.
    """
    # A path symmetry says nothing about the glyph as a whole.
    if mode not in METRICS_KEY_MODES or isinstance(layer, _ScopedLayer):
        return False
    try:
        if unpaired:
            print(
                "[AZ Symmetrify] unpaired nodes: the right metrics key was left alone. "
                "An unpaired node is never mirrored, so it can hold the ink's left and "
                'right edges at different distances — which is what "==|" says they '
                "are not."
            )
            return False
        if _shear_of(layer) != 0.0:
            print(
                "[AZ Symmetrify] italic master: the right metrics key was left alone. "
                '"==|" centres the plain upright bounding box, which is not where a '
                "sheared symmetry axis sits."
            )
            return False
        if _holds_components(layer):
            print(
                "[AZ Symmetrify] this layer holds components: the right metrics key was "
                "left alone. Glyphs measures the sidebearings off bounds that include "
                "components, while the symmetry axis is the centre of the drawn paths "
                "only."
            )
            return False
        if not float(layer.width):
            print(
                "[AZ Symmetrify] zero-width layer: the right metrics key was left alone. "
                'A mark is placed by its anchors and keeps no advance, and "==|" would '
                "give it one."
            )
            return False
        previous = layer.rightMetricsKey
        current = str(previous) if previous else ""
        ours = _read_userdata(layer, METRICS_KEY_STORE) is not None
        if current and not (ours and current == RIGHT_METRICS_KEY):
            print(
                f'[AZ Symmetrify] this layer already has a right metrics key, "{current}", '
                "so it was left as it is."
            )
            return False
        if not current:
            glyph_key = layer.parent.rightMetricsKey
            if glyph_key:
                print(
                    f'[AZ Symmetrify] this glyph already has a right metrics key, "{glyph_key}", '
                    "so the layer was given none of its own."
                )
                return False
            if not _write_userdata(layer, METRICS_KEY_STORE, ""):
                return False
            _write_right_metrics_key(layer, RIGHT_METRICS_KEY)
        owners = _key_owners(layer) or set()
        owners.add(mode)
        if not _write_userdata(layer, METRICS_KEY_OWNERS, sorted(owners)):
            print(
                "[AZ Symmetrify] the right metrics key was set, but which symmetry "
                "earned it could not be recorded on this layer — so releasing a "
                "different symmetry may hand the key back sooner than it should."
            )
        return True
    except Exception:
        print(traceback.format_exc())
        return False


def _sync_metrics(layer) -> bool:
    """Apply the layer's metrics keys, so the sidebearing the bind just claimed
    is actually taken. Returns whether the outline MOVED.

    Called only when ``_bind_metrics_key`` actually set "==|" (Hugo,
    2026-08-11). Binding used to write the key and stop there, leaving the
    designer to run Glyph > Update Metrics — the key was an intent, and the
    spacing did not change until they asked. Now the bind finishes the job.

    Deliberately NOT called when the key was refused. All five refusals —
    another mode, unpaired nodes, an italic master, components on the layer, a
    zero width — mean this plugin has no claim on the spacing, and
    ``syncMetrics`` applies whatever keys the layer already has. Running it
    there would re-space a glyph for reasons that have nothing to do with the
    symmetry just bound, from a menu item that does not mention metrics.

    THE OUTLINE CAN MOVE. ``syncMetrics`` runs both sides, and the LEFT one sets
    the LSB — which translates every node, the concrete case in
    ``azsym_sync``'s translation branch. The binding's stored centre is
    ABSOLUTE, so the caller has to re-derive it; left stale, the generated half
    is written back by twice the shift on the next frame and the glyph folds.

    Its undo lands in the bind's own step: a click is one event, one undo
    group, and Cmd-Z of a keyed bind takes back the snap and the spacing at
    once (probed on Glyphs 3532, 2026-10-20).
    """
    before = _paths_of(layer)
    try:
        layer.syncMetrics()
    except Exception:
        print(traceback.format_exc())
        return False
    return _paths_of(layer) != before


def _key_owners(layer):
    """The modes recorded as having earned the right metrics key, or None.

    None means NOTHING is recorded, which is not the same as an empty set: a
    layer bound by a build older than METRICS_KEY_OWNERS has no record at all,
    and the caller falls back to that build's behaviour there rather than
    guessing.

    A value that cannot be enumerated is treated as unrecorded for the same
    reason ``_items`` duck-types: a reopened file hands back NSArray, and
    ``isinstance(x, list)`` is False for it.
    """
    stored = _read_userdata(layer, METRICS_KEY_OWNERS)
    if stored is None:
        return None
    try:
        return {str(name) for name in stored}
    except Exception:
        return None


def _keeps_the_key(layer, remaining: set) -> bool:
    """True while a symmetry that actually EARNED "==|" is still bound.

    Not "a keyed mode is still bound": the key is refused on an unpaired
    binding, on an italic master and on a layer holding components, so a bound
    VERTICAL that was refused the key must not hold a key a released ROTATE
    earned. The recorded owners are what tells the two apart.

    The record is pruned down to ``remaining`` and written back, so a mode that
    is released and later re-bound — and REFUSED the key that second time — does
    not go on counting as an owner of a key it no longer earns. The prune is
    skipped when nothing survives, because the caller deletes the whole entry
    one line later.

    An unrecorded layer answers with the OLD rule: the key stays while any keyed
    mode is bound. That is the one direction that cannot strip the key off a
    layer that is still mirroring, which is the invariant the feature rests on.
    """
    keyed = {mode for mode in METRICS_KEY_MODES if mode in remaining}
    owners = _key_owners(layer)
    if owners is None:
        return bool(keyed)
    surviving = owners & keyed
    if not surviving:
        return False
    if surviving != owners:
        _write_userdata(layer, METRICS_KEY_OWNERS, sorted(surviving))
    return True


def _release_metrics_key(layer, remaining: set) -> None:
    """Give the layer its own right metrics key back, once nothing wants ours.

    ``remaining`` is what is STILL bound once the release has been written.
    Several symmetries compose, and two of the three imply the key, so releasing
    one of them while the other is still on has to leave the key exactly where
    it is — see ``_keeps_the_key`` for why the mode alone is not the question.

    Keyed on the SAVED entry rather than on the mode or on the shear: whichever
    bind wrote the key, that entry is the only record of what was there before,
    and the restore has to work even if the master's italic angle changed in
    between. No entry means we never wrote a key, so there is nothing to undo.

    ``str(saved) or None`` — the empty string is how "there was no key" is
    stored, and None is what clears ours.

    The restore is CONDITIONAL on the layer's key still being ours. Nothing
    stops the designer retyping it — or deleting it — in the inspector while the
    symmetry is bound, and putting the saved value back over theirs would
    destroy their work.
    A key that is no longer "==|" means the designer has taken the key back, so
    the marker is dropped and their value is left exactly as it stands. The
    deletion case reads the same way: an empty key is not "==|" either, so a key
    they just removed is never resurrected — and it gets its OWN wording, because
    interpolating a cleared key into the retype message renders it as
    ``it reads "None"``, which reads as a key containing the word None.

    Both notices name the SAVED key as well as the current one. It is deleted on
    the next line and there is nowhere else it is written down, so printing it is
    the difference between unrecoverable loss and something the designer can
    retype.

    The comparison is sound because "==|" round-trips.
    ``-[GSLayer setRightMetricsKey:]`` normalises a value to a "==" prefix, and
    a value that already carries one is stored verbatim (read out of the arm64
    binary at 0x88664) — so what comes back out is the same three characters
    that went in.

    Clearing does not move the sidebearing back: the key and the value are
    separate stores, and the width is left wherever the designer's last Update
    Metrics put it. Releasing has never touched the designer's work, and this
    keeps to that.
    """
    if isinstance(layer, _ScopedLayer):
        return
    try:
        if _keeps_the_key(layer, remaining):
            return
        _write_userdata(layer, METRICS_KEY_OWNERS, None)
        saved = _read_userdata(layer, METRICS_KEY_STORE)
        if saved is None:
            return
        current = layer.rightMetricsKey
        # str(...) because a reopened file hands back NSString, and the falsy
        # branch because a cleared key reads as None rather than "".
        note = (
            f' The key this layer carried before the bind was "{saved}", and it '
            "has now been dropped — retype it if you want it back."
            if saved
            else " This layer carried no key of its own before the bind."
        )
        if (str(current) if current else "") == RIGHT_METRICS_KEY:
            _write_right_metrics_key(layer, str(saved) or None)
        elif current:
            print(
                "[AZ Symmetrify] the right metrics key is no longer ours — it reads "
                f'"{current}" rather than "{RIGHT_METRICS_KEY}" — so it was left '
                "exactly as it stands." + note
            )
        else:
            print(
                "[AZ Symmetrify] the right metrics key has been cleared by hand, so "
                "it was left cleared rather than having ours put back." + note
            )
        _write_userdata(layer, METRICS_KEY_STORE, None)
    except Exception:
        print(traceback.format_exc())


def _items(mapping) -> list:
    """(key, value) pairs from a dict or an NSDictionary.

    A reopened Glyphs file hands back NSDictionary, for which ``.items()`` exists
    but ``isinstance(x, dict)`` is False — so duck-type, never type-check.
    """
    try:
        return [(str(key), mapping[key]) for key in mapping.keys()]
    except Exception:
        return []


def _items_or_none(mapping):
    """``_items`` that distinguishes "empty" from "could not be read".

    ``_items`` returns [] for both, which is fine for reading — an unreadable
    store has nothing to offer either way — but fatal before a wholesale
    reassignment: writing back a dict rebuilt from [] would erase every other
    key on that layer, including other plugins', in the saved font.
    """
    try:
        return [(str(key), mapping[key]) for key in mapping.keys()]
    except Exception:
        return None


def _read_userdata(owner, key):
    try:
        return owner.userData[key]
    except Exception:
        return None


def _write_userdata(owner, key, value) -> bool:
    """Set or clear a userData entry. Returns True on success, False on failure.

    ``owner.userData`` is a ``UserDataProxy`` (a plain ``DictProxy``, not a
    dict): ``__setitem__`` routes to ``setUserData_forKey_``, an ObjC mutator on
    the owner, so a failed direct write is not the plain-dict immutability
    problem it looks like — and the proxy has no ``mutableCopy()`` to fall back
    on anyway. The fallback instead rebuilds a plain dict from the owner's
    current keys and reassigns it wholesale, which goes through the same setter
    path and does the real work.

    That fallback is only safe when the current keys can actually be read. If
    enumeration fails it REFUSES to write, because reassigning a dict rebuilt
    from a failed read would silently delete every other key on this layer —
    other plugins' data included — in the user's saved font file.
    """
    try:
        if value is None:
            existing = _items_or_none(owner.userData)
            if existing is None:
                return False
            if all(name != key for name, _ in existing):
                return True  # already absent; nothing to do
            try:
                del owner.userData[key]
            except Exception:
                owner.userData = {name: item for name, item in existing if name != key}
            return True
        try:
            owner.userData[key] = value
            return True
        except Exception:
            existing = _items_or_none(owner.userData)
            if existing is None:
                return False
            data = dict(existing)
            data[key] = value
            owner.userData = data
            return True
    except Exception:
        print(traceback.format_exc())
        return False


# ------------------------------------------------------------ the palette


def _build_switches(frame):
    """The row of switches, one per entry in ``SEGMENT_ITEMS``, not yet wired.

    The icons are set once, here, and never again: they are the same tile
    whether a symmetry is on or off — the segment's own selected state is what
    says which — so re-imaging on every update would build four images to set
    them to what they already are.
    """
    control = NSSegmentedControl.alloc().initWithFrame_(frame)
    control.setSegmentCount_(len(SEGMENT_ITEMS))
    control.setSegmentStyle_(ROUNDED_SEGMENTS)
    control.setTrackingMode_(SELECT_ANY)
    # Small, to match the frame. Without it the control keeps its regular
    # metrics and CLIPS inside a 21pt frame: AppKit sizes a segmented control's
    # chrome from its control size, not from its frame.
    control.setControlSize_(SMALL_CONTROL)
    control.setSegmentDistribution_(FILL_EQUALLY)
    control.setAutoresizingMask_(WIDTH_SIZABLE)
    for index, (mode, title) in enumerate(SEGMENT_ITEMS):
        image = _segment_image(mode, title)
        if image is not None:
            control.setImage_forSegment_(image, index)
        else:
            control.setLabel_forSegment_(SEGMENT_FALLBACKS[mode], index)
        control.setToolTip_forSegment_(title, index)
    return control


class AZSymmetryPassThroughView(NSView):
    """A view that takes no click of its own: one that misses its buttons goes
    to whatever lies underneath, here the switch row."""

    def hitTest_(self, point) -> Any:  # noqa: N802 - AppKit selector
        hit = objc.super(AZSymmetryPassThroughView, self).hitTest_(point)
        return None if hit is self else hit


def _build_triangles(owner, frame) -> tuple:
    """A transparent swap button over the right half of each switch, wired to
    ``owner``, laid over the switch row at ``frame``. Its margins and width all
    stretch, so a resize keeps it on that half."""
    row = AZSymmetryPassThroughView.alloc().initWithFrame_(frame)
    row.setAutoresizingMask_(WIDTH_SIZABLE)
    _origin, (width, height) = frame
    third = width / len(SEGMENT_ITEMS)
    buttons = []
    for index in range(len(SEGMENT_ITEMS)):
        half = (((index + 0.5) * third, 0.0), (third / 2.0, height))
        button = NSButton.alloc().initWithFrame_(half)
        button.setAutoresizingMask_(THIRD_RESIZING)
        button.setTransparent_(True)
        button.setHidden_(True)
        button.setTag_(index)
        button.setTarget_(owner)
        button.setAction_("triangleClicked:")
        row.addSubview_(button)
        buttons.append(button)
    return row, buttons


def _build_diagonal(owner) -> tuple:
    """The Diagonal row — caption, angle field, degree sign — wired to ``owner``.

    The field sends its action on Return AND when it loses focus, so an angle
    typed and then left by clicking the canvas still counts.
    """
    font = NSFont.systemFontOfSize_(DIAGONAL_FONT_SIZE)
    y = PALETTE_MARGIN_BOTTOM
    label = NSTextField.labelWithString_(DIAGONAL_LABEL)
    label.setFont_(font)
    label.setFrame_(((PALETTE_MARGIN_X, y), (DIAGONAL_LABEL_WIDTH, DIAGONAL_ROW_HEIGHT)))
    left = PALETTE_MARGIN_X + DIAGONAL_LABEL_WIDTH + DIAGONAL_GAP
    field = NSTextField.alloc().initWithFrame_(
        ((left, y), (DIAGONAL_FIELD_WIDTH, DIAGONAL_ROW_HEIGHT))
    )
    field.setFont_(font)
    field.setControlSize_(SMALL_CONTROL)
    field.setPlaceholderString_(DIAGONAL_PLACEHOLDER)
    field.cell().setSendsActionOnEndEditing_(True)
    field.setTarget_(owner)
    field.setAction_("diagonalChanged:")
    # The arrows, Esc and every keystroke reach ``owner`` as the field's
    # delegate: the steps and the live preview.
    field.setDelegate_(owner)
    degree = NSTextField.labelWithString_("°")
    degree.setFont_(font)
    right = left + DIAGONAL_FIELD_WIDTH + DIAGONAL_GAP
    degree.setFrame_(((right, y), (DIAGONAL_DEGREE_WIDTH, DIAGONAL_ROW_HEIGHT)))
    return label, field, degree


def _same_layer(first, second) -> bool:
    """Whether two layer references name one layer. By ``==``, which PyObjC
    sends to ``isEqual:``, and not ``is``: two proxies of one GSLayer are not
    guaranteed to be one Python object, and a false "changed" would stop the
    Diagonal field from ever committing."""
    if first is None or second is None:
        return first is second
    try:
        return bool(first == second)
    except Exception:
        return False


def _replace_typed(text_view, text: str) -> None:
    """Put ``text`` in the Diagonal field's editor through the text system, so
    it is an undoable edit like typing. With ``setString_`` it registered no
    undo, and Cmd-Z after an arrow replayed the editor's old typing over the new
    text ("9", ↑ "10", Cmd-Z "0"), which leaving the field then applied
    (review, 2026-10-15)."""
    whole = (0, len(str(text_view.string())))
    if text_view.shouldChangeTextInRange_replacementString_(whole, text):
        text_view.replaceCharactersInRange_withString_(whole, text)
        text_view.didChangeText()


def _fill_diagonal(field, layer, filled_for) -> None:
    """Show ``layer``'s Diagonal angle, unless the designer is typing in it.

    A field being edited holds half-typed text, and an interface update can
    arrive mid-typing (a selection change, another window), so it is left alone
    until committed — but only while ``layer`` is still the one it was filled
    for (``filled_for``). The field keeps focus after Return, so ``currentEditor``
    alone stayed set and the old glyph's angle stayed on show after the glyph in
    front changed (final review, 2026-10-15). Text typed for a glyph that is no
    longer in front is abandoned, never committed to the next one.

    Empty is Auto, and its grey text is the angle Auto splits on, the glyph's
    corner to corner (Hugo, 2026-10-16: "auto value based on bbox").
    """
    if field.currentEditor() is not None:
        if _same_layer(layer, filled_for):
            return
        field.abortEditing()
        _clear_field_preview()
    usable = layer is not None and not _has_no_outline(layer)
    field.setEnabled_(usable)
    field.setStringValue_(_diagonal_text(layer) if usable else "")
    auto = _auto_angle(layer) if usable else None
    field.setPlaceholderString_(DIAGONAL_PLACEHOLDER if auto is None else _angle_text(auto))


def _sync_switches(control, plugin_instance, layer) -> list[bool]:
    """Put every segment in step with ``layer``: lit, clickable, and its tooltip.

    Hands back what it LIT, per segment — the palette keeps it, because that is
    how ``_clicked_segment`` tells which switch a click flipped.

    The ONE place the row's state is written — when the palette is built, on
    every interface update, and after a click. After a click matters most: the
    control has already flipped the segment it was clicked on, a bind that found
    nothing to pair must not leave it lit over an unbound layer, and the mirrors
    and the half-turn exclude each other, so one click can change three segments.

    Every segment is read from ``_item_specs``, the pair included. The contextual
    menu's click path resynced by walking ``MODE_ITEMS`` — three modes over a row
    of four — so after any click the Both segment showed Rotate's state and
    Rotate's own segment was never touched.

    No layer, or a layer with no outline, greys the whole row: every switch
    would refuse the click anyway, because ``_bind_layer`` has nothing to pair.
    Components are not an outline here either; a composite is mirrored by binding
    the glyph it is built from.
    """
    if plugin_instance is None or layer is None or _has_no_outline(layer):
        control.setEnabled_(False)
        for index in range(len(SEGMENT_ITEMS)):
            control.setSelected_forSegment_(False, index)
        return [False] * len(SEGMENT_ITEMS)
    if azsym_native.ERROR is not None:
        # The engine is missing, so every bind would fail. Grey the whole row,
        # but keep showing what the layer IS bound to (that is userData, read
        # without the engine), and say why on every switch.
        bound = _bound_modes(layer)
        reason = f"{ENGINE_MISSING}: {azsym_native.ERROR}"
        control.setEnabled_(False)
        lit = []
        for index, (mode, _title) in enumerate(SEGMENT_ITEMS):
            control.setSelected_forSegment_(mode in bound, index)
            control.setToolTip_forSegment_(reason, index)
            lit.append(mode in bound)
        return lit
    target = plugin_instance._target_of(layer)
    if isinstance(target, str):
        control.setEnabled_(False)
        for index in range(len(SEGMENT_ITEMS)):
            control.setSelected_forSegment_(False, index)
            control.setEnabled_forSegment_(False, index)
            control.setToolTip_forSegment_(target[0].upper() + target[1:], index)
        return [False] * len(SEGMENT_ITEMS)
    scoped = isinstance(target, _ScopedLayer)
    layer = target
    control.setEnabled_(True)
    # ONE read of the layer for the whole row. Each switch asked for the outline
    # and the selection again, twice: 9.7 ms an update on a 73-node g, read live.
    structure = _structure_of(layer)
    signs = plugin_instance._source_signs(
        layer, [mode for mode, _title in SEGMENT_ITEMS], structure.paths
    )
    specs = plugin_instance._item_specs(layer, signs)
    held = {mode for _label, is_bound, mode in specs if is_bound}
    # One symmetry at a time on an italic master (``excluded_by``).
    italic = bool(_shear_of(layer))
    lit = []
    for index, (label, bound, mode) in enumerate(specs):
        control.setSelected_forSegment_(bound, index)
        lit.append(bound)
        # Greyed out when what it excludes is already on — see
        # _segment_enabled. Disabling is how the designer finds that out BEFORE
        # clicking rather than by watching two switches go off. Likewise when
        # it cannot give a pure symmetry (``_impure``).
        enabled = _segment_enabled(mode, held, italic)
        impure = (
            _impure(plugin_instance, layer, mode, structure, signs[mode])
            if enabled and not bound
            else ""
        )
        # About a selected guide or a typed angle the drift gate is lifted, and
        # a reshape under 2% of the em passes it, so the tooltip says how far
        # the half you keep would move instead (``_axis_note``).
        note = (
            _axis_note(layer, mode, structure, signs[mode])
            if enabled and not bound and not impure
            else ""
        )
        control.setEnabled_forSegment_(enabled and not impure, index)
        if scoped:
            label += " on these paths"
        control.setToolTip_forSegment_(
            _segment_tooltip(mode, label + note, held, impure, italic), index
        )
    return lit


def _sync_triangles(buttons, plugin_instance, layer) -> list[tuple[str, bool] | None]:
    """In each lit switch, a swap button over a chip pointing at the half its
    symmetry has the designer draw (``triangleClicked_``). None in an unlit one,
    or in a row ``_sync_switches`` greys. Faded, with the reason, in one that
    cannot swap (``_swap_states``), its button still enabled: a disabled one
    lets the click through to the switch underneath, which switched the
    symmetry off (real AppKit, 2026-10-22), so ``_swap_side`` refuses it
    instead. Hands back each switch's chip as ``(side, enabled)``, or None,
    for its icon to draw."""
    target = None
    states = {}
    if (
        plugin_instance is not None
        and layer is not None
        and azsym_native.ERROR is None
        and not _has_no_outline(layer)
    ):
        target = plugin_instance._target_of(layer)
        if isinstance(target, str):
            target = None
        else:
            states = plugin_instance._swap_states(target)
    chips: list[tuple[str, bool] | None] = []
    for button, (mode, _title) in zip(buttons, SEGMENT_ITEMS, strict=True):
        state = states.get(mode)
        button.setHidden_(state is None)
        if state is None:
            chips.append(None)
            continue
        sign, reason = state
        drawn, written = _side_names(target, mode, sign)
        button.setToolTip_(reason or f"You draw the {drawn}. Click to draw the {written}")
        chips.append((drawn, not reason))
    return chips


def _side_names(layer, mode: str, sign: int) -> tuple[str, str]:
    """(the half bound ``mode`` has the designer draw, the half it generates).
    A half-turn about a typed angle names its halves by that line."""
    angle = _typed_angle(layer) if mode == azsym_geometry.ROTATE else None
    standing = None if angle is None else azsym_geometry.guide_stands(angle)
    positive, negative = azsym_geometry.side_labels(mode, standing)
    return (positive, negative) if sign > 0 else (negative, positive)


def _clicked_segment(control, drawn: list) -> int:
    """The segment a click just flipped, or -1 if that cannot be told.

    NOT ``selectedSegment()``. With several segments allowed on at once, AppKit
    answers that with the segment most recently turned ON, and goes on answering
    it after that segment is turned off again — read from the running Glyphs 4,
    a row with nothing lit still reporting segment 1. So a click that turned a
    switch OFF was read as whichever switch had last been turned on, and the
    plugin released that one or nothing (Hugo, 2026-10-10: "I can't click again
    on buttons to disable it").

    The palette knows what it last drew, and a click flips exactly one segment,
    so the clicked one is the one that no longer matches.
    """
    changed = [
        index
        for index, was in enumerate(drawn)
        if bool(control.isSelectedForSegment_(index)) != bool(was)
    ]
    return changed[0] if len(changed) == 1 else -1


def _shown_layer(controller):
    """The layer the designer is looking at in ``controller``'s window, or None.

    The ACTIVE layer when there is one. Glyphs 4 has none while the caret sits
    AFTER the last glyph — clicking the empty canvas to its right, or typing,
    puts it there — although that glyph is still on screen, drawn inactive. Read
    live: ``activeLayer()`` None and ``font.selectedLayers`` empty, so the row
    greyed out and a click did nothing (Hugo, 2026-10-10: "After clicking a
    symmetrify option sometimes nothing happen, all buttons become disable").
    The glyph just before the caret stands in, as the one being looked at.

    Only from a tab that is SHOWING. A tab not in front — every Edit tab, while
    the Font view is — has no window (read live: ten tabs, nine windowless), and
    acting on a glyph nobody can see would be worse than a greyed row.
    """
    try:
        layer = controller.activeLayer()
        if layer is not None:
            return layer
        tab = controller.activeEditViewController()
        if tab is None or tab.graphicView().window() is None:
            return None
        layers = list(tab.layers)
        index = int(tab.textCursor) - 1
        return layers[index] if 0 <= index < len(layers) else None
    except Exception:
        return None


def _track_hover(view, owner) -> None:
    """Tell ``owner`` when the cursor enters and leaves ``view``.

    Over the VISIBLE rect, so AppKit keeps the area matched to the view as the
    sidebar resizes it and nothing has to re-install it. Enter and exit only —
    which segment the cursor is on comes from the poll they start and stop.

    A failure costs the hover ghost and nothing else: the switches work without
    it, and they are the plugin's only way to bind.
    """
    try:
        area = NSTrackingArea.alloc().initWithRect_options_owner_userInfo_(
            ((0.0, 0.0), (0.0, 0.0)),
            TRACK_ENTER_EXIT | TRACK_IN_ACTIVE_APP | TRACK_VISIBLE_RECT,
            owner,
            None,
        )
        view.addTrackingArea_(area)
    except Exception:
        print(traceback.format_exc())


class AZSymmetryPalette(PalettePlugin):
    """The switch row, in the palette sidebar beside the Edit view.

    MOVED HERE FROM THE CONTEXTUAL MENU (Hugo, 2026-10-10). The row, its icons,
    its greying, its tooltips and its hover ghost are the same; what changed is
    that the state of the layer in front of the designer is now on screen while
    they draw instead of for as long as a menu is open.

    DEFINED IN plugin.py and not in a sibling module. It is listed beside
    ``AZSymmetry`` in Info.plist's ``Principal Classes``, which the loader
    resolves AFTER running plugin.py, so a class defined here is found the same
    way the principal class is. A sibling would need a module name unique across
    every installed plugin — they share one ``sys.modules``, and AZ Dynamic
    Anchors already ships one called ``palette`` — and a back-reference to reach
    ``_PLUGIN``, which a class in this module simply reads.

    Defined unconditionally for a sharper reason: if the loader cannot find a
    class that list names, it drops the WHOLE bundle, mirroring included. A test
    reads the plist and holds every name in it to a class in this module.
    """

    @objc.python_method
    def settings(self) -> None:
        self.name = PALETTE_NAME
        self.sortId = PALETTE_SORT_ID
        # Two rows: the switches, and under them the Diagonal angle (decision 8).
        above = PALETTE_MARGIN_BOTTOM + DIAGONAL_ROW_HEIGHT + PALETTE_ROW_GAP
        height = above + SEGMENT_HEIGHT + PALETTE_MARGIN_TOP
        self.dialog = NSView.alloc().initWithFrame_(((0.0, 0.0), (PALETTE_WIDTH, height)))
        row = ((PALETTE_MARGIN_X, above), (PALETTE_WIDTH - PALETTE_MARGIN_X * 2.0, SEGMENT_HEIGHT))
        self._switches = _build_switches(row)
        self._triangle_row, self._triangles = _build_triangles(self, row)
        label, self._diagonal, degree = _build_diagonal(self)
        # The layer the field was last filled for — see ``_fill_diagonal``.
        self._diagonal_layer = None
        for view in (label, self._diagonal, degree):
            self.dialog.addSubview_(view)
        # The palette is the target, and it is retained: Glyphs holds every
        # palette it builds for as long as the window it sits in.
        self._switches.setTarget_(self)
        self._switches.setAction_("segmentToggled:")
        # What the row last showed, per segment — see ``_clicked_segment``.
        self._drawn = [False] * len(SEGMENT_ITEMS)
        # The chip each switch's icon was last drawn with — see ``_refresh_triangles``.
        self._chips = [None] * len(SEGMENT_ITEMS)
        self.dialog.addSubview_(self._switches)
        # After the switches, so the triangles are drawn over them.
        self.dialog.addSubview_(self._triangle_row)
        _track_hover(self._switches, self)
        # Registered here and not in start(): a SECONDARY class in a multi-class
        # bundle is not reliably sent start(), while settings() always runs when
        # Glyphs builds the palette. AZ Dynamic Anchors' palette found that out.
        Glyphs.addCallback(self.update, UPDATEINTERFACE)
        # An undo posts no interface update, and Cmd-Z of a bind takes the
        # binding back with the artwork (probed, Glyphs 3532).
        center = NSNotificationCenter.defaultCenter()
        for name in UNDO_NOTIFICATIONS:
            center.addObserver_selector_name_object_(self, "undoChanged:", name, None)
        self.update(None)

    @objc.python_method
    def start(self) -> None:
        """Nothing: see ``settings`` for why the callback is registered there."""
        return

    @objc.python_method
    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            Glyphs.removeCallback(self.update)
        with contextlib.suppress(Exception):
            NSNotificationCenter.defaultCenter().removeObserver_(self)

    @objc.python_method
    def _switch_layer(self):
        """The layer this palette's switches act on: the one in ITS window.

        Glyphs builds one palette per document window and hands each its window
        controller. Read through that rather than ``Glyphs.font``, which answers
        for the FRONT document — read live with two files open, one palette was
        showing the other window's glyph, and a click in it went there too.
        ``_current_layer()`` only before Glyphs has handed the palette a window.
        """
        try:
            controller = self.windowController()
        except Exception:
            controller = None
        if controller is None:
            return _current_layer()
        return _shown_layer(controller)

    @objc.python_method
    def update(self, sender=None) -> None:
        """Follow the layer in front of the designer.

        ``UPDATEINTERFACE`` is coalesced — measured on Glyphs 4 at one per
        selection change and none while idle — so there is no storm to throttle.

        NO DRAG CHECK. There was one, and it made the palette lie: a selection
        changes on mouse-DOWN — clicking a node, clicking another glyph — so the
        one update that change produces arrives with the button still held, and
        a guard on the button threw away the only notice the palette was going
        to get. Read from the running app: a row showing one mirror lit over a
        layer bound to both.
        """
        try:
            layer = self._switch_layer()
            self._drawn = _sync_switches(self._switches, _PLUGIN, layer)
            self._refresh_triangles(_PLUGIN, layer)
            _fill_diagonal(self._diagonal, layer, self._diagonal_layer)
            self._diagonal_layer = layer
        except Exception:
            # Throttled: this runs on every interface update, and a fault that
            # repeats should cost one traceback, not a console full of them.
            _print_once(traceback.format_exc())

    @objc.python_method
    def _refresh_triangles(self, plugin_instance, layer) -> None:
        """The swap buttons, and each switch's icon with its chip or without:
        redrawn only when that changes, since this runs on every update."""
        chips = _sync_triangles(self._triangles, plugin_instance, layer)
        for index, (mode, title) in enumerate(SEGMENT_ITEMS):
            if chips[index] != self._chips[index]:
                side, enabled = chips[index] or (None, True)
                image = _segment_image(mode, title, side, enabled)
                if image is not None:
                    self._switches.setImage_forSegment_(image, index)
        self._chips = chips

    def undoChanged_(self, notification) -> None:
        """Cmd-Z or Cmd-Shift-Z, anywhere: the row reads the layer again."""
        self.update(None)

    def segmentToggled_(self, sender) -> None:
        """A segment was clicked: bind or release the symmetry it stands for."""
        try:
            plugin_instance = _PLUGIN
            if plugin_instance is None:
                return
            layer = self._switch_layer()
            index = _clicked_segment(sender, self._drawn)
            if 0 <= index < len(SEGMENT_ITEMS):
                plugin_instance._toggle(SEGMENT_ITEMS[index][0], layer)
                _drop_hover_ghost()
            # The whole row, from what actually happened rather than from what
            # the click asked for — see ``_sync_switches``. Also when the click
            # could not be read: the control has flipped a segment on its own,
            # and leaving it flipped would show a state the layer has not got.
            self._drawn = _sync_switches(sender, plugin_instance, layer)
            self._refresh_triangles(plugin_instance, layer)
        except Exception:
            print(traceback.format_exc())

    def triangleClicked_(self, sender) -> None:
        """A switch's triangle was clicked: swap the half its symmetry has you draw."""
        try:
            plugin_instance = _PLUGIN
            if plugin_instance is None:
                return
            layer = self._switch_layer()
            index = int(sender.tag())
            if layer is not None and 0 <= index < len(SEGMENT_ITEMS):
                plugin_instance._swap_side(SEGMENT_ITEMS[index][0], layer)
                # The wash moves and no node does, so nothing else repaints it.
                plugin_instance._refresh()
            self._refresh_triangles(plugin_instance, layer)
        except Exception:
            print(traceback.format_exc())

    def diagonalChanged_(self, sender) -> None:
        """The Diagonal field was committed — Return, or leaving it."""
        try:
            layer = self._switch_layer()
            if layer is None:
                return
            if not _same_layer(layer, self._diagonal_layer):
                # Filled for a glyph no longer in front, with no interface update
                # in between: its text is that glyph's, so it is shown afresh
                # rather than committed here, where it would lift the drift gate
                # on a glyph nobody typed on (final review, 2026-10-15).
                _clear_field_preview()
                _fill_diagonal(sender, layer, None)
                self._diagonal_layer = layer
                return
            sender.setStringValue_(_commit_diagonal(layer, str(sender.stringValue())))
            # Applied: the glyph shows what it now has, not a preview of it.
            _clear_field_preview()
            _drop_hover_ghost()
            self._drawn = _sync_switches(self._switches, _PLUGIN, layer)
            # A re-split can turn a half-turn's triangle.
            self._refresh_triangles(_PLUGIN, layer)
            if _PLUGIN is not None:
                # A re-split changes the wash and the dashed line, not a node,
                # so nothing else would repaint them.
                _PLUGIN._refresh()
        except Exception:
            print(traceback.format_exc())

    def control_textView_doCommandBySelector_(self, control, text_view, command) -> bool:
        """↑/↓ step the Diagonal angle by 1°, ⇧ by 10°, applied at once; Esc
        puts back the applied angle; Return applies it and turns Rotate 180° on
        (``_enter_diagonal``). Anything else is AppKit's to handle.

        PyObjC gives this the delegate signature on its own — BOOL back, the
        command as a str (probed on Glyphs 3532 and 4202) — so it needs no
        decorator. The text is the field editor's: while typing, the field
        itself does not hold it yet.
        """
        try:
            name = str(command)
            step = DIAGONAL_STEPS.get(name)
            if step is None and name not in (DIAGONAL_CANCEL, DIAGONAL_RETURN):
                return False
            layer = self._switch_layer()
            if layer is None:
                return False
            if not _same_layer(layer, self._diagonal_layer):
                # Filled for a glyph no longer in front: its text is that
                # glyph's, so it is shown afresh, never stepped onto this one
                # (review, 2026-10-15) — the guard ``diagonalChanged_`` has.
                _clear_field_preview()
                _replace_typed(text_view, _diagonal_text(layer))
                self._diagonal_layer = layer
                return True
            if name == DIAGONAL_RETURN:
                _enter_diagonal(layer, str(text_view.string()))
                # Not taken: AppKit ends editing as for any Return, and the
                # field's action, ``diagonalChanged_``, applies the same text
                # again (a no-op now), clears the preview and redraws.
                return False
            if step is None:
                # Esc cancels: the applied angle, and no preview of it.
                _replace_typed(text_view, _diagonal_text(layer))
                _clear_field_preview()
            else:
                shown = _step_diagonal(layer, str(text_view.string()), step)
                _replace_typed(text_view, shown)
                _show_field_preview(layer, _read_angle(shown)[1])
            self._drawn = _sync_switches(self._switches, _PLUGIN, layer)
            # A step re-splits at once, which can turn the half-turn's chip.
            self._refresh_triangles(_PLUGIN, layer)
            return True
        except Exception:
            print(traceback.format_exc())
            return False

    def controlTextDidChange_(self, notification) -> None:
        """Every keystroke in the Diagonal field: preview what it reads. Text
        that does not read as an angle keeps the last preview."""
        try:
            layer = self._switch_layer()
            if layer is None:
                return
            if not _same_layer(layer, self._diagonal_layer):
                # Typing into text filled for another glyph: preview nothing on
                # this one; ``diagonalChanged_`` refuses the commit the same way.
                _clear_field_preview()
                return
            # The field editor's text when AppKit hands it over; the field does
            # not hold what is being typed until editing ends.
            editor = None
            with contextlib.suppress(Exception):
                editor = notification.userInfo()["NSFieldEditor"]
            text = editor.string() if editor is not None else self._diagonal.stringValue()
            readable, angle = _read_angle(str(text))
            if readable:
                _show_field_preview(layer, angle)
                self._drawn = _sync_switches(self._switches, _PLUGIN, layer)
        except Exception:
            print(traceback.format_exc())

    def controlTextDidEndEditing_(self, notification) -> None:
        """Focus left the Diagonal field: the preview goes with it."""
        try:
            _clear_field_preview()
        except Exception:
            print(traceback.format_exc())

    def mouseEntered_(self, event) -> None:
        """Start ghosting whichever switch the cursor comes to rest on."""
        try:
            plugin_instance = _PLUGIN
            layer = self._switch_layer()
            if plugin_instance is None or layer is None or _has_no_outline(layer):
                return
            specs = plugin_instance._item_specs(layer)
            _watch_segment_hover(plugin_instance, self._switches, layer, specs)
        except Exception:
            print(traceback.format_exc())

    def mouseExited_(self, event) -> None:
        """Hand back whatever ghost was up before the cursor arrived."""
        try:
            _stop_hover()
        except Exception:
            print(traceback.format_exc())
