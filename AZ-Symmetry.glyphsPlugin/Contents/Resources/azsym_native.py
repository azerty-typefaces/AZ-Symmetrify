"""The native engine: ``libazsym.dylib`` beside this file, loaded with ctypes.

Analyze measured every master layer in pure Python and crossed the PyObjC
bridge for every node — 14.8 s of CPU on a large family. The pairing and scan rules
now run in the library (``native/`` in the repository), which also walks the
font itself.

This module speaks plain Python values only: lists of ``(x, y)`` in; tuples,
lists and small records out. The types the rest of the plugin uses
(``Binding``, ``Candidate``, ``LayerScan``) are built by the modules that own
them, ``azsym_binding`` and ``azsym_scan``, so this module imports neither and
both can import it.

The library defines no Objective-C classes. That is what lets ``reload`` put a
fresh copy into a running Glyphs: two images defining the same class clash,
two images of plain C functions do not.
"""

from __future__ import annotations

import ctypes
import hashlib
import shutil
import struct
import tempfile
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any, NamedTuple

LIBRARY = Path(__file__).with_name("libazsym.dylib")

# Kinds for ``names``. Each table's order is the library's code for it, pinned
# against the Python constants by the test suite.
MODE, VERDICT, SKIPPED = 0, 1, 2
# Any mode string the Python modules do not define. Python let such a mode fall
# through every branch, and the library does the same with this code.
OTHER = 5
# A candidate for ``azsym_scan.COMBOS[i]`` carries mode ``COMBO_BASE + i``.
COMBO_BASE = 100

_I32P = ctypes.POINTER(ctypes.c_int32)
_F64P = ctypes.POINTER(ctypes.c_double)

_SIGNATURES: dict[str, tuple[Any, list[Any]]] = {
    "azs_source_hash": (ctypes.c_char_p, []),
    "azs_last_error": (ctypes.c_char_p, []),
    "azs_enum_name": (ctypes.c_char_p, [ctypes.c_int32, ctypes.c_int32]),
}

_LIB: Any = None
ERROR: str | None = None
MODE_NAMES: tuple[str, ...] = ()
MODE_CODES: dict[str, int] = {}


class EngineUnavailable(RuntimeError):
    """The native engine did not load, so nothing can be paired or measured."""


def load(path: Path) -> str | None:
    """Load the library at ``path``: the reason it failed, or None. Never raises.

    A plugin that cannot load its engine must still load — the switch row greys
    out and says why (``plugin._sync_switches``) — which it can only do if
    importing this module succeeded.
    """
    global _LIB, ERROR, MODE_NAMES, MODE_CODES
    try:
        library = ctypes.CDLL(str(path))
        for name, (restype, argtypes) in _SIGNATURES.items():
            function = getattr(library, name)
            function.restype = restype
            function.argtypes = argtypes
    except Exception as error:
        _LIB, ERROR = None, f"{type(error).__name__}: {error}"
        traceback.print_exc()
        return ERROR
    _LIB, ERROR = library, None
    MODE_NAMES = names(MODE)
    MODE_CODES = {name: code for code, name in enumerate(MODE_NAMES)}
    return None


def reload(path: Path | None = None) -> str:
    """Load a FRESH copy of the library into this process; its source hash.

    Development only — the hot-swap. dlopen hands back the image it already has
    for a path it has seen, so the copy goes to a name derived from its bytes: a
    changed library is a new name and a new image, an unchanged one is the image
    already loaded. The old image stays mapped, and nothing calls into it once
    ``_LIB`` is replaced, because every call reads ``_LIB`` when it runs.
    """
    source = Path(path) if path else LIBRARY
    digest = hashlib.sha1(source.read_bytes()).hexdigest()[:12]
    copy = Path(tempfile.gettempdir()) / f"azsym-{digest}.dylib"
    if not copy.exists():
        shutil.copy2(source, copy)
    error = load(copy)
    if error is not None:
        raise EngineUnavailable(error)
    return source_hash()


def lib() -> Any:
    """The loaded library, or ``EngineUnavailable`` saying why there is none."""
    if _LIB is None:
        raise EngineUnavailable(ERROR or "the native engine is not loaded")
    return _LIB


def source_hash() -> str:
    """SHA-1 of the ``native/`` sources the loaded library was built from."""
    return lib().azs_source_hash().decode()


def names(kind: int) -> tuple[str, ...]:
    """The library's name for each code of ``kind``, in code order."""
    found: list[str] = []
    while (name := lib().azs_enum_name(kind, len(found))) is not None:
        found.append(name.decode())
    return tuple(found)


def mode_code(mode: Any) -> int:
    """The library's code for ``mode``; ``OTHER`` for a string it does not know."""
    return MODE_CODES.get(mode, OTHER)


def _check(status: int) -> None:
    """Raise what the Python kernel raised where the library refused a call."""
    if status == 0:
        return
    message = lib().azs_last_error().decode()
    if status == 2:
        raise IndexError(message)
    raise ValueError(message)


def _ints(values: Sequence[int]) -> Any:
    return (ctypes.c_int32 * len(values))(*values)


def _flat(points: Sequence[Sequence[float]]) -> Any:
    """One contour as ``x0, y0, x1, y1, ...`` doubles."""
    values = [float(value) for point in points for value in (point[0], point[1])]
    return (ctypes.c_double * len(values))(*values)


def _pack(paths: Sequence[Sequence[Sequence[float]]]) -> tuple[int, Any, Any, int]:
    """``(contours, lengths, xy, nodes)``: the layout every kernel call takes."""
    lengths = [len(path) for path in paths]
    values = [float(value) for path in paths for point in path for value in (point[0], point[1])]
    return (
        len(lengths),
        _ints(lengths),
        (ctypes.c_double * len(values))(*values),
        sum(lengths),
    )


def _upm(upm: Any) -> float:
    """``upm`` as the library takes it. A falsy one (None, 0) is 0.0, which
    ``safe_units`` turns into the default exactly as Python's ``upm and upm > 0``
    did."""
    return float(upm) if upm else 0.0


# ---- later tasks add their _SIGNATURES.update(...) blocks and wrappers here ----


_SIGNATURES.update(
    {
        "azs_hypot": (ctypes.c_double, [ctypes.c_double, ctypes.c_double]),
        "azs_sum": (ctypes.c_double, [_F64P, ctypes.c_int32]),
    }
)


def hypot(x: float, y: float) -> float:
    """``math.hypot`` as Glyphs 4's Python computes it."""
    return lib().azs_hypot(x, y)


def builtin_sum(values: Sequence[float]) -> float:
    """Builtin ``sum`` of floats as Glyphs 4's Python (>= 3.12) computes it."""
    array = (ctypes.c_double * len(values))(*values)
    return lib().azs_sum(array, len(values))


_SIGNATURES.update(
    {
        "azs_mode_op": (ctypes.c_int32, [ctypes.c_int32, ctypes.c_int32, _I32P]),
        "azs_point_op": (
            ctypes.c_int32,
            [ctypes.c_int32, *[ctypes.c_double] * 4, ctypes.c_int32, ctypes.c_double, _F64P],
        ),
        "azs_side": (
            ctypes.c_int32,
            [*[ctypes.c_double] * 4, ctypes.c_int32, *[ctypes.c_double] * 3, _F64P],
        ),
        "azs_contour_side": (
            ctypes.c_int32,
            [*[ctypes.c_double] * 4, ctypes.c_int32, ctypes.c_double, _F64P],
        ),
        "azs_contour_sides": (
            ctypes.c_int32,
            [ctypes.c_int32, _F64P, ctypes.c_double, ctypes.c_double, ctypes.c_int32,
             ctypes.c_double, _F64P],
        ),
        "azs_frame": (
            ctypes.c_int32,
            [ctypes.c_int32, ctypes.c_int32, _F64P, ctypes.c_double, _F64P],
        ),
        "azs_diagonal": (ctypes.c_int32, [ctypes.c_double, ctypes.c_double, _F64P]),
        "azs_close": (ctypes.c_int32, [*[ctypes.c_double] * 5, _I32P]),
    }
)


def mode_op(op: int, mode: str) -> int:
    """0 ``switch_of``, 1 ``moves_as`` (both as codes), 2 ``reverses_traversal``."""
    out = ctypes.c_int32()
    _check(lib().azs_mode_op(op, mode_code(mode), ctypes.byref(out)))
    return out.value


def point_op(op: int, point: Any, centre: Any, mode: str, shear: float) -> tuple[float, float]:
    """0 ``unshear``, 1 ``reshear``, 2 ``flip``, 3 ``partner``, 4 ``clamp``."""
    out = (ctypes.c_double * 2)()
    _check(
        lib().azs_point_op(
            op, point[0], point[1], centre[0], centre[1], mode_code(mode), shear, out
        )
    )
    return (out[0], out[1])


def side(point: Any, centre: Any, mode: str, shear: float, extent: Any = (1.0, 1.0)) -> float:
    out = ctypes.c_double()
    _check(
        lib().azs_side(
            point[0], point[1], centre[0], centre[1], mode_code(mode), shear,
            extent[0], extent[1], ctypes.byref(out),
        )
    )
    return out.value


def contour_side(point: Any, centre: Any, mode: str, shear: float) -> float:
    out = ctypes.c_double()
    _check(
        lib().azs_contour_side(
            point[0], point[1], centre[0], centre[1], mode_code(mode), shear, ctypes.byref(out)
        )
    )
    return out.value


def contour_sides(points: Sequence[Any], centre: Any, mode: str, shear: float) -> list[float]:
    out = (ctypes.c_double * len(points))()
    _check(
        lib().azs_contour_sides(
            len(points), _flat(points), centre[0], centre[1], mode_code(mode), shear, out
        )
    )
    return list(out)


def frame(op: int, points: Sequence[Any], shear: float) -> tuple[float, float]:
    """0 ``centre_of``, 1 ``extent_of``."""
    out = (ctypes.c_double * 2)()
    _check(lib().azs_frame(op, len(points), _flat(points), shear, out))
    return (out[0], out[1])


def diagonal(extent: Any) -> tuple[tuple[float, float], tuple[float, float]]:
    out = (ctypes.c_double * 4)()
    _check(lib().azs_diagonal(extent[0], extent[1], out))
    return ((out[0], out[1]), (out[2], out[3]))


def close(a: Any, b: Any, tol: float = 0.5) -> bool:
    out = ctypes.c_int32()
    _check(lib().azs_close(a[0], a[1], b[0], b[1], tol, ctypes.byref(out)))
    return bool(out.value)


_SIGNATURES.update(
    {
        "azs_offsets": (
            ctypes.c_int32,
            [ctypes.c_int32, ctypes.c_int32, ctypes.c_int32, _I32P, _I32P],
        ),
        "azs_alignment": (
            ctypes.c_int32,
            [ctypes.c_int32, _F64P, ctypes.c_int32, _F64P, ctypes.c_int32, ctypes.c_double,
             ctypes.c_double, ctypes.c_int32, ctypes.c_double, _I32P, _F64P],
        ),
        "azs_pair_nodes": (
            ctypes.c_int32,
            [ctypes.c_int32, _I32P, _F64P, ctypes.c_double, ctypes.c_double, ctypes.c_int32,
             ctypes.c_double, ctypes.c_int32, ctypes.c_int32, ctypes.c_double, ctypes.c_double,
             _I32P, _I32P, _I32P, _I32P],
        ),
    }
)


def _tuples(array: Any, width: int, count: int) -> tuple[tuple[int, ...], ...]:
    return tuple(tuple(array[i * width : (i + 1) * width]) for i in range(count))


def offsets(count: int, mode: str, self_paired: bool) -> list[int]:
    out = (ctypes.c_int32 * max(count, 1))()
    found = ctypes.c_int32()
    _check(lib().azs_offsets(count, mode_code(mode), int(self_paired), out, ctypes.byref(found)))
    return list(out[: found.value])


def alignment(
    src: Sequence[Any], dst: Sequence[Any], self_paired: bool, centre: Any, mode: str, shear: float
) -> tuple[int, float, float, bool] | None:
    ints = (ctypes.c_int32 * 3)()
    doubles = (ctypes.c_double * 2)()
    _check(
        lib().azs_alignment(
            len(src), _flat(src), len(dst), _flat(dst), int(self_paired), centre[0], centre[1],
            mode_code(mode), shear, ints, doubles,
        )
    )
    if not ints[0]:
        return None
    return (ints[1], doubles[0], doubles[1], bool(ints[2]))


def pair_nodes(
    paths: Sequence[Sequence[Any]],
    centre: Any,
    mode: str,
    shear: float,
    source_sign: int,
    split: Any = None,
) -> Any:
    """``(pairs, onaxis, unpaired)`` — every node lands in exactly one of them,
    so buffers sized to the outline always hold the answer. ``split`` is a
    half-turn's guide split (``azsym_geometry.axis_split``), or None for the
    box diagonal."""
    contours, lengths, xy, nodes = _pack(paths)
    room = max(nodes, 1)
    pairs = (ctypes.c_int32 * (4 * room))()
    onaxis = (ctypes.c_int32 * (2 * room))()
    unpaired = (ctypes.c_int32 * (2 * room))()
    counts = (ctypes.c_int32 * 3)()
    sx, sy = split if split is not None else (0.0, 0.0)
    _check(
        lib().azs_pair_nodes(
            contours, lengths, xy, centre[0], centre[1], mode_code(mode), shear, source_sign,
            int(split is not None), sx, sy, pairs, onaxis, unpaired, counts,
        )
    )
    return (
        _tuples(pairs, 4, counts[0]),
        _tuples(onaxis, 2, counts[1]),
        _tuples(unpaired, 2, counts[2]),
    )


_SIGNATURES.update(
    {
        "azs_sources": (
            ctypes.c_int32,
            [ctypes.c_int32, ctypes.c_int32, _I32P, _F64P, _I32P, ctypes.c_int32, _I32P,
             ctypes.c_int32, ctypes.c_double, ctypes.c_double, ctypes.c_int32, ctypes.c_double,
             _I32P, _F64P, _I32P],
        ),
        "azs_model_centre": (
            ctypes.c_int32,
            [ctypes.c_int32, _I32P, _F64P, _I32P, ctypes.c_int32, ctypes.c_double,
             ctypes.c_double, ctypes.c_int32, ctypes.c_double, _F64P, _I32P],
        ),
        "azs_fitted": (
            ctypes.c_int32,
            [ctypes.c_int32, _I32P, _F64P, ctypes.c_double, ctypes.c_double, ctypes.c_int32,
             ctypes.c_double, ctypes.c_int32, _I32P],
        ),
    }
)


def sources(
    meet: bool,
    paths: Sequence[Sequence[Any]],
    pairs: Sequence[Sequence[int]],
    onaxis: Sequence[Sequence[int]],
    centre: Any,
    mode: str,
    shear: float,
) -> list[tuple[tuple[int, int], tuple[float, float]]]:
    """``mirror_source`` (or ``meet_source``) as ``(node, point)`` in dict order."""
    contours, lengths, xy, nodes = _pack(paths)
    room = max(nodes, 1)
    refs = (ctypes.c_int32 * (2 * room))()
    points = (ctypes.c_double * (2 * room))()
    count = ctypes.c_int32()
    _check(
        lib().azs_sources(
            int(meet), contours, lengths, xy,
            _ints([value for pair in pairs for value in pair]), len(pairs),
            _ints([value for ref in onaxis for value in ref]), len(onaxis),
            centre[0], centre[1], mode_code(mode), shear, refs, points, ctypes.byref(count),
        )
    )
    return [
        ((refs[2 * i], refs[2 * i + 1]), (points[2 * i], points[2 * i + 1]))
        for i in range(count.value)
    ]


def model_centre(
    paths: Sequence[Sequence[Any]], onaxis: Sequence[Sequence[int]], centre: Any, mode: str,
    shear: float,
) -> tuple[float, float] | None:
    """The model's centre, or None where Python handed back ``centre`` itself."""
    contours, lengths, xy, _nodes = _pack(paths)
    out = (ctypes.c_double * 2)()
    unchanged = ctypes.c_int32()
    _check(
        lib().azs_model_centre(
            contours, lengths, xy, _ints([value for ref in onaxis for value in ref]), len(onaxis),
            centre[0], centre[1], mode_code(mode), shear, out, ctypes.byref(unchanged),
        )
    )
    return None if unchanged.value else (out[0], out[1])


def fitted(
    paths: Sequence[Sequence[Any]], centre: Any, mode: str, shear: float, source_sign: int
) -> int:
    contours, lengths, xy, _nodes = _pack(paths)
    out = ctypes.c_int32()
    _check(
        lib().azs_fitted(
            contours, lengths, xy, centre[0], centre[1], mode_code(mode), shear, source_sign,
            ctypes.byref(out),
        )
    )
    return out.value


class RawCandidate(NamedTuple):
    """``azsym_scan.Candidate`` as codes; ``azsym_scan._candidate`` names them."""

    mode: int
    verdict: int
    shift: float
    share: float
    unpaired: int
    nodes: int


class _Candidate(ctypes.Structure):
    _fields_ = [
        ("mode", ctypes.c_int32),
        ("verdict", ctypes.c_int32),
        ("shift", ctypes.c_double),
        ("share", ctypes.c_double),
        ("unpaired", ctypes.c_int32),
        ("nodes", ctypes.c_int32),
    ]


def _raw(candidate: Any) -> RawCandidate:
    return RawCandidate(
        candidate.mode, candidate.verdict, candidate.shift, candidate.share,
        candidate.unpaired, candidate.nodes,
    )


_SIGNATURES.update(
    {
        "azs_reach_of": (ctypes.c_double, [ctypes.c_int32, _F64P]),
        "azs_safe_units": (ctypes.c_double, [ctypes.c_double]),
        "azs_is_safe": (ctypes.c_int32, [ctypes.c_double, ctypes.c_double, ctypes.c_double]),
        "azs_buried_nodes": (ctypes.c_int32, [ctypes.c_int32, _I32P, _F64P, _I32P, _I32P]),
        "azs_measure": (
            ctypes.c_int32,
            [ctypes.c_int32, _I32P, _F64P, ctypes.c_double, ctypes.c_double, ctypes.c_int32,
             ctypes.c_double, ctypes.c_double, ctypes.POINTER(_Candidate)],
        ),
        "azs_reshape": (
            ctypes.c_int32,
            [ctypes.c_int32, ctypes.c_int32, _I32P, _F64P, _I32P, _I32P, ctypes.c_int32,
             ctypes.c_double, _F64P, ctypes.POINTER(ctypes.c_uint8), _I32P],
        ),
    }
)


def reach_of(points: Sequence[Any]) -> float:
    return lib().azs_reach_of(len(points), _flat(points))


def safe_units(upm: Any) -> float:
    return lib().azs_safe_units(_upm(upm))


def is_safe(shift: float, share: float, upm: Any) -> bool:
    return bool(lib().azs_is_safe(shift, share, _upm(upm)))


def buried_nodes(paths: Sequence[Sequence[Any]]) -> set[tuple[int, int]]:
    contours, lengths, xy, nodes = _pack(paths)
    refs = (ctypes.c_int32 * (2 * max(nodes, 1)))()
    count = ctypes.c_int32()
    _check(lib().azs_buried_nodes(contours, lengths, xy, refs, ctypes.byref(count)))
    return {(refs[2 * i], refs[2 * i + 1]) for i in range(count.value)}


def measure(
    paths: Sequence[Sequence[Any]], centre: Any, mode: str, shear: float, upm: Any
) -> RawCandidate:
    contours, lengths, xy, _nodes = _pack(paths)
    out = _Candidate()
    _check(
        lib().azs_measure(
            contours, lengths, xy, centre[0], centre[1], mode_code(mode), shear, _upm(upm),
            ctypes.byref(out),
        )
    )
    return _raw(out)


def reshape(
    settle: bool, paths: Sequence[Sequence[Any]], modes: Sequence[str], signs: Sequence[int],
    shear: float,
) -> tuple[list[float], list[bool], int]:
    """``proposal`` (``settle`` False) or ``settlement``: every node's final
    position, whether any step wrote it, and the unpaired count."""
    contours, lengths, xy, nodes = _pack(paths)
    out = (ctypes.c_double * (2 * max(nodes, 1)))()
    written = (ctypes.c_uint8 * max(nodes, 1))()
    unpaired = ctypes.c_int32()
    sign_list = list(signs) if signs else [0] * len(modes)
    _check(
        lib().azs_reshape(
            int(settle), contours, lengths, xy, _ints([mode_code(mode) for mode in modes]),
            _ints(sign_list), len(modes), shear, out, written, ctypes.byref(unpaired),
        )
    )
    return list(out[: 2 * nodes]), [bool(flag) for flag in written[:nodes]], unpaired.value


_SIGNATURES.update(
    {
        "azs_measure_combo": (
            ctypes.c_int32,
            [ctypes.c_int32, _I32P, _F64P, _I32P, ctypes.c_int32, ctypes.c_double,
             ctypes.c_double, ctypes.POINTER(_Candidate)],
        ),
        "azs_scan_layer": (
            ctypes.c_int32,
            [ctypes.c_int32, _I32P, _F64P, ctypes.c_double, ctypes.c_int32, ctypes.c_double,
             _I32P, _I32P, ctypes.c_int32, ctypes.POINTER(_Candidate), _I32P, _I32P, _I32P,
             _F64P],
        ),
    }
)


def _combos(combos: Sequence[Sequence[str]]) -> tuple[Any, Any]:
    """``azsym_scan.COMBOS`` as (every combination's mode codes, each one's length)."""
    return (
        _ints([mode_code(mode) for combo in combos for mode in combo] or [0]),
        _ints([len(combo) for combo in combos] or [0]),
    )


def measure_combo(
    paths: Sequence[Sequence[Any]], modes: Sequence[str], shear: float, upm: Any
) -> RawCandidate:
    contours, lengths, xy, _nodes = _pack(paths)
    out = _Candidate()
    _check(
        lib().azs_measure_combo(
            contours, lengths, xy, _ints([mode_code(mode) for mode in modes] or [0]), len(modes),
            shear, _upm(upm), ctypes.byref(out),
        )
    )
    return _raw(out)


def scan_layer(
    paths: Sequence[Sequence[Any]], shear: float, ceiling: int, upm: Any,
    combos: Sequence[Sequence[str]],
) -> tuple[tuple[RawCandidate, ...], int, int, float]:
    """``(candidates, skipped code or -1, nodes, reach)``."""
    contours, lengths, xy, _nodes = _pack(paths)
    combo_modes, combo_lengths = _combos(combos)
    out = (_Candidate * (3 + len(combos)))()
    count, skipped, nodes = ctypes.c_int32(), ctypes.c_int32(), ctypes.c_int32()
    reach = ctypes.c_double()
    _check(
        lib().azs_scan_layer(
            contours, lengths, xy, shear, ceiling, _upm(upm), combo_modes, combo_lengths,
            len(combos), out, ctypes.byref(count), ctypes.byref(skipped), ctypes.byref(nodes),
            ctypes.byref(reach),
        )
    )
    candidates = tuple(_raw(out[i]) for i in range(count.value))
    return candidates, skipped.value, nodes.value, reach.value


_SIGNATURES.update(
    {
        "azs_walk_begin": (
            ctypes.c_int32,
            [ctypes.c_void_p, ctypes.c_int32, ctypes.POINTER(ctypes.c_char_p), _F64P,
             ctypes.c_double, ctypes.c_char_p, ctypes.c_int32, _I32P, _I32P, ctypes.c_int32,
             ctypes.POINTER(ctypes.c_void_p), _I32P],
        ),
        "azs_walk_glyphs": (
            ctypes.c_int32,
            [ctypes.c_void_p, ctypes.c_int32, ctypes.c_int32, ctypes.POINTER(ctypes.c_void_p),
             ctypes.POINTER(ctypes.c_int64)],
        ),
        "azs_walk_end": (None, [ctypes.c_void_p]),
    }
)


class WalkedLayer(NamedTuple):
    """One master layer as the native walk read it."""

    master: int  # index into the master table handed to walk_begin, -1 unknown
    layer_id: str
    layer_name: str
    components: bool
    bound: tuple[str, ...]
    scan: tuple  # azsym_scan.scan_from_native's argument


class WalkedGlyph(NamedTuple):
    name: str
    unclassified: int
    layers: tuple[WalkedLayer | None, ...]  # None: unreadable


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.at = data, 0

    def i32(self) -> int:
        (value,) = struct.unpack_from("<i", self.data, self.at)
        self.at += 4
        return value

    def f64(self) -> float:
        (value,) = struct.unpack_from("<d", self.data, self.at)
        self.at += 8
        return value

    def text(self) -> str:
        size = self.i32()
        if size < 0:
            return "None"  # str(None): what Python made of a nil
        # UTF-16 code units, as walk.mm writes them: they carry any NSString, a
        # lone surrogate included, where UTF-8 has no form for one.
        value = self.data[self.at : self.at + size].decode("utf-16-le", "surrogatepass")
        self.at += size
        return value


def parse_glyphs(blob: bytes) -> list[WalkedGlyph]:
    """One chunk of the native walk (layout: native/walk.mm)."""
    reader = _Reader(blob)
    glyphs = []
    for _ in range(reader.i32()):
        name = reader.text()
        unclassified = reader.i32()
        layers: list[WalkedLayer | None] = []
        for _ in range(reader.i32()):
            if reader.i32() != 0:
                layers.append(None)
                continue
            master = reader.i32()
            layer_id = reader.text()
            layer_name = reader.text()
            components = bool(reader.i32())
            bound = tuple(reader.text() for _ in range(reader.i32()))
            skipped, nodes, reach = reader.i32(), reader.i32(), reader.f64()
            candidates = tuple(
                RawCandidate(
                    reader.i32(), reader.i32(), reader.f64(), reader.f64(), reader.i32(),
                    reader.i32(),
                )
                for _ in range(reader.i32())
            )
            layers.append(
                WalkedLayer(
                    master, layer_id, layer_name, components, bound,
                    (candidates, skipped, nodes, reach),
                )
            )
        glyphs.append(WalkedGlyph(name, unclassified, tuple(layers)))
    return glyphs


class Walk:
    """One whole-font walk. Holds the image it began in, so a hot-swap in the
    middle of a survey cannot hand this walk's handle to another image."""

    def __init__(self, library: Any, handle: int, glyph_count: int) -> None:
        self._library, self._handle, self.glyph_count = library, handle, glyph_count

    def glyphs(self, start: int, count: int) -> list[WalkedGlyph]:
        blob, length = ctypes.c_void_p(), ctypes.c_int64()
        status = self._library.azs_walk_glyphs(
            self._handle, start, count, ctypes.byref(blob), ctypes.byref(length)
        )
        if status != 0:
            raise ValueError(self._library.azs_last_error().decode())
        return parse_glyphs(ctypes.string_at(blob, length.value))

    def close(self) -> None:
        if self._handle:
            self._library.azs_walk_end(self._handle)
            self._handle = 0


def walk_begin(
    font_pointer: int | None,
    masters: Sequence[tuple[str, float]],
    upm: float,
    key: str,
    ceiling: int,
    combos: Sequence[Sequence[str]],
) -> Walk | None:
    """A walk over the font at ``font_pointer``, or None when this Glyphs is not
    one the walk recognises (the caller then reads outlines in Python)."""
    library = lib()
    ids = (ctypes.c_char_p * max(len(masters), 1))(*[master[0].encode() for master in masters])
    shears = (ctypes.c_double * max(len(masters), 1))(*[master[1] for master in masters])
    combo_modes, combo_lengths = _combos(combos)
    handle, count = ctypes.c_void_p(), ctypes.c_int32()
    status = library.azs_walk_begin(
        font_pointer, len(masters), ids, shears, float(upm), key.encode(), ceiling,
        combo_modes, combo_lengths, len(combos), ctypes.byref(handle), ctypes.byref(count),
    )
    if status == 3:
        return None
    _check(status)
    return Walk(library, handle.value or 0, count.value)


load(LIBRARY)
