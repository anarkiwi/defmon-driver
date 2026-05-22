"""defMON internal-keycode table + chord resolver for direct-call dispatch.

`keymatrix_tap`'s reliability problems (debounce, multi-modifier latch,
player-IRQ races) are eliminated by `keyhandler.press_via_loop`, which
writes defMON's debounced-key register (``$0E44``) and modifier-flag
bytes (``$0E41`` / ``$0E42``) directly inside a halted block. To do that
we need a name → internal-keycode table — the values $0E44 takes after
the matrix-slot → keycode LUT at ``$0F90``.

This module provides:

  * :data:`STATIC_KEYCODES` — known $0E44 values, verified in
    :mod:`defmon_driver.field_setter` against a live defMON build.
  * :func:`resolve_chord` — split a symbolic chord like ``("CTRL", "S")``
    into ``(mod1, mod2, key, keycode)`` ready for ``press_via_loop``.
  * :func:`load_table` / :func:`save_table` — JSON persistence, merging
    over the static fallback.
  * :func:`bootstrap_keycodes` — live capture against a running
    container, snapshotting ``$0E44`` for every documented key via
    :func:`keyhandler.capture_keycode_via_checkpoint`.

CTRL, LSHIFT/RSHIFT, and CBM are the unambiguous modifier keys — they
set bits in ``$0E41`` and never carry data. COLON / SEMICOLON / EQUALS
are *voice-mute* modifiers (``$0E42``), but they're also regular keys
in their own right (disk-menu typed input). The default resolver treats
them as keys; callers that want voice-mute behaviour pass an explicit
``mod2=`` byte.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vice_driver.keys import canonical_name

if TYPE_CHECKING:
    from vice_driver.binmon import BinMon

    from .defmon import Defmon

log = logging.getLogger(__name__)

# Canonical-name → modifier-flag bit for $0E41.
MOD1_BITS: dict[str, int] = {
    "CTRL": 0x04,
    "LSHIFT": 0x10,
    "RSHIFT": 0x10,
    "CBM": 0x20,
}

# Canonical-name → modifier-flag bit for $0E42 (voice-mute toggles).
# Not promoted by resolve_chord by default — see module docstring.
MOD2_BITS: dict[str, int] = {
    "COLON": 0x01,
    "SEMICOLON": 0x02,
    "EQUALS": 0x04,
}

# Known $0E44 values, derived directly from the $0F90 matrix-slot →
# keycode LUT shipped with stock defMON. The LUT is indexed
# ``slot = row * 8 + (7 - col)`` — within each matrix row the eight
# columns are stored with the column index reversed. Verified by
# direct $0E44 observation against real matrix-taps (the byte at
# $0E44 always matches ``LUT[slot]`` during the keypress window).
#
# ``defmon_driver.field_setter.NOTE_KEYCODES`` was originally seeded
# with the *note-byte values* (the bytes defMON stores in pattern
# memory after a note keypress: $30 for Z, $31 for S, …) instead of the
# $0E44 keycodes the scanner writes. Verified empirically: matrix-tap of
# Z writes $30 to the note slot; injecting keycode=$30 via press_via_loop
# writes $4B (wrong); injecting keycode=$1A writes $30 (correct, matches
# matrix-tap). field_setter.NOTE_KEYCODES has since been corrected to the
# $0E44 values from this table, and the original note-byte mapping moved
# to field_setter.NOTE_PATTERN_BYTES.
#
# Slot bytes $FF and $FE in the LUT are sentinels (modifier keys, and
# slots without a $0E44 dispatch) and are excluded.
STATIC_KEYCODES: dict[str, int] = {
    # Row 0 — INSTDEL / RETURN / CRSRLR / F7 / F1 / F3 / F5 / CRSRUD
    "INSTDEL": 0x84,
    "RETURN": 0x92,
    "CRSRLR": 0x6A,
    "F7": 0xB7,
    "F1": 0xB1,
    "F3": 0xB3,
    "F5": 0xB5,
    "CRSRUD": 0x6F,
    # Row 1 — 3 W A 4 Z S E LSHIFT
    "3": 0x33,
    "W": 0x17,
    "A": 0x01,
    "4": 0x34,
    "Z": 0x1A,
    "S": 0x13,
    "E": 0x05,
    # Row 2 — 5 R D 6 C F T X
    "5": 0x35,
    "R": 0x12,
    "D": 0x04,
    "6": 0x36,
    "C": 0x03,
    "F": 0x06,
    "T": 0x14,
    "X": 0x18,
    # Row 3 — 7 Y G 8 B H U V
    "7": 0x37,
    "Y": 0x19,
    "G": 0x07,
    "8": 0x38,
    "B": 0x02,
    "H": 0x08,
    "U": 0x15,
    "V": 0x16,
    # Row 4 — 9 I J 0 M K O N
    "9": 0x39,
    "I": 0x09,
    "J": 0x0A,
    "0": 0x30,
    "M": 0x0D,
    "K": 0x0B,
    "O": 0x0F,
    "N": 0x0E,
    # Row 5 — + P L - . : @ ,
    "PLUS": 0x2D,
    "P": 0x10,
    "L": 0x0C,
    "MINUS": 0x3D,
    "PERIOD": 0x2E,
    # COLON, EQUALS, SEMICOLON map to LUT sentinel $FE — voice-mute
    # modifier keys, dispatched via $0E42, not $0E44.
    "AT": 0x1D,
    "COMMA": 0x2C,
    # Row 6 — pound * ; CLR/HOME RSHIFT = ^ /
    "POUND": 0x3A,
    "STAR": 0x00,
    "CLRHOME": 0x88,
    "UPARROW": 0x1E,
    "SLASH": 0x2F,
    # Row 7 — 1 left-arrow CTRL 2 SPACE CBM Q RUNSTOP
    "1": 0x31,
    "LEFTARROW": 0x1F,
    "2": 0x32,
    "SPACE": 0x20,
    "Q": 0x11,
    "RUNSTOP": 0x93,
}

# Matrix-slot LUT base in defMON RAM (64 bytes, indexed by
# ``row * 8 + (7 - col)``). ``bootstrap_keycodes`` reads this region
# directly to derive a per-build keycode table.
LUT_ADDR = 0x0F90
LUT_LEN = 64


@dataclass(frozen=True)
class ResolvedChord:
    """A chord split into its direct-call ingredients."""

    mod1: int
    mod2: int
    key: str | None  # canonical name of the non-modifier key (or None)
    keycode: int | None  # $0E44 value to write (or None for pure-modifier)


def resolve_chord(
    names: tuple[str, ...] | list[str],
    table: dict[str, int] | None = None,
) -> ResolvedChord:
    """Split ``names`` into ``(mod1, mod2, key, keycode)``.

    CTRL/LSHIFT/RSHIFT/CBM are auto-promoted to ``mod1`` bits. All other
    names must resolve to a canonical key in ``table`` (defaults to
    :data:`STATIC_KEYCODES`); at most one such key per chord is allowed.

    A pure-modifier chord (e.g. ``("CTRL",)``) returns ``key=None`` and
    ``keycode=None`` — callers using ``press_via_loop`` should still
    pass *some* keycode, typically the previous $0E44 value or ``0``.
    """
    if table is None:
        table = STATIC_KEYCODES
    mod1 = 0
    mod2 = 0
    key: str | None = None
    for raw in names:
        up = canonical_name(raw)
        if up in MOD1_BITS:
            mod1 |= MOD1_BITS[up]
            continue
        if key is not None:
            raise ValueError(
                f"chord {tuple(names)!r} has more than one non-modifier key: {key!r} and {up!r}"
            )
        key = up
    if key is None:
        return ResolvedChord(mod1=mod1, mod2=mod2, key=None, keycode=None)
    keycode = table.get(key)
    if keycode is None:
        raise KeyError(
            f"no internal keycode for key {key!r}; run "
            f"defmon_driver.bootstrap_keycodes against a live container "
            f"or extend STATIC_KEYCODES"
        )
    return ResolvedChord(mod1=mod1, mod2=mod2, key=key, keycode=keycode)


def load_table(path: str | Path | None) -> dict[str, int]:
    """Return a keycode table, JSON-overlaid onto :data:`STATIC_KEYCODES`.

    ``path=None`` or a missing file returns a copy of the static table
    (with a warning logged if the file was named but absent). The JSON
    is a plain object of ``{canonical_name: int}``; keys are upper-cased
    and values must be in ``0..255``.
    """
    out = dict(STATIC_KEYCODES)
    if path is None:
        return out
    p = Path(path)
    if not p.exists():
        log.warning("keycode table %s not found; falling back to STATIC_KEYCODES", p)
        return out
    raw = json.loads(p.read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"keycode table {p}: top-level value must be a JSON object")
    for k, v in raw.items():
        if not isinstance(k, str):
            raise ValueError(f"keycode table {p}: non-string key {k!r}")
        if not isinstance(v, int) or not 0 <= v <= 0xFF:
            raise ValueError(f"keycode table {p}: entry {k!r}={v!r} not a u8")
        out[k.upper()] = v
    return out


def save_table(path: str | Path, table: dict[str, int]) -> None:
    """Write ``table`` to ``path`` as a sorted JSON object (one key per
    line, deterministic for diffing)."""
    sorted_items = {k: int(v) for k, v in sorted(table.items())}
    Path(path).write_text(json.dumps(sorted_items, indent=2, sort_keys=True) + "\n")


def decode_lut(lut: bytes) -> dict[str, int]:
    """Decode a 64-byte ``$0F90`` LUT dump into a keycode table.

    Slot indexing is ``row * 8 + (7 - col)`` — within each matrix row
    the eight columns are stored with the column index reversed. Sentinel
    bytes ``$FF`` (modifier keys) and ``$FE`` (voice-mute keys, dispatched
    via ``$0E42``) are excluded from the returned mapping.
    """
    from vice_driver.keys import _CANONICAL

    if len(lut) != LUT_LEN:
        raise ValueError(f"LUT must be {LUT_LEN} bytes, got {len(lut)}")
    out: dict[str, int] = {}
    for key in _CANONICAL:
        slot = key.row * 8 + (7 - key.col)
        kc = lut[slot]
        if kc in (0xFE, 0xFF):
            continue
        out[key.name] = kc
    return out


def bootstrap_keycodes(bm: "BinMon", d: "Defmon | None" = None) -> dict[str, int]:
    """Read defMON's ``$0F90`` matrix-slot LUT and decode it into a
    full keycode table.

    A single ``mem_get`` on a halted CPU; takes a few milliseconds,
    deterministic, doesn't tap any keys. Replaces the older per-key
    checkpoint-capture approach (which captured a pre-LUT value rather
    than the post-LUT byte written to ``$0E44``).

    ``d`` is accepted for backwards compatibility with the previous
    signature but is unused — callers no longer need a ``Defmon``
    instance to bootstrap the table.
    """
    del d  # unused; kept for back-compat
    lut = bm.mem_get(LUT_ADDR, LUT_ADDR + LUT_LEN - 1)
    table = decode_lut(lut)
    log.info("bootstrap: decoded %d keycodes from $0F90 LUT", len(table))
    return table
