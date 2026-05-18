"""High-level "set any UI field" API for defMON.

Built on top of :func:`defmon_driver.keyhandler.press_via_loop`. Combines:
  - mode switching ($7167 + $0BBA call to do the proper mode-init)
  - cursor positioning (CRSRDOWN / CRSRUP / CRSRLR direct-presses, OR
    direct mem_set of cursor variables for speed)
  - field-specific direct-call chords:
      seqED note          : bare note key (Z=0x30, S=0x31, X=0x32, ...)
      seqED sidCALL2      : CBM+digit (kc $01..$06 → A..F, $30..$39 → 0..9)
      seqED Speed         : CTRL+CBM+digit (auto-advances by step)
      seqED sidCALL1 (V1) : CBM+SHIFT+digit (V0/V2 unstable — see BUGS.md)
      seqLIST pattern num : bare hex digit (writer at $E211)
      sidTAB hex cell     : bare hex digit (writer at $C15F) — but the
                            sidTAB row buffer at $BDA4 is intermediate
                            state; for sidTAB use
                            :class:`defmon_driver.sidtab.SidTab` which
                            handles calibration + commit.
      disk-menu filename  : bare letter (writer at $C4FA) — typed-name
                            input is driven via ``Defmon.disk_*`` helpers.

The data path bypasses defMON's debounce/scan layer entirely — pressing
"Z" via this API takes ~50ms vs ~200ms for keymatrix_tap, AND works
reliably across handlers that JMP-not-RTS.

Two modes of operation:
  1. **Chord-driven**: every write goes via press_via_loop, exercising the
     same code path the user does. Use for:
       - validating chord behavior
       - keeping cursor + screen state in sync
       - working with handlers we don't yet understand
  2. **Direct memory write**: skips the dispatch entirely; mem_sets the
     pattern data byte at $1F00 + offset. Use for:
       - bulk seeding test fixtures
       - 2SID voices once their memory layout is mapped
       - speed-critical setup
     Note: direct writes do NOT update on-screen display until the next
     pattern repaint (which happens automatically when cursor moves).

Pattern memory layout (verified):
    $1F00-$1F0B  : step 0 V0/V1/V2  (4 bytes per voice: flag/slot_a/slot_b/note)
    $1F0C-$1F17  : step 1
    ...           : step N

Editor sub-field names below ('speed', 'sidcall1', 'sidcall2', 'note')
are defMON's user-facing names tied to the input chord, NOT the byte
labels. Byte 0 is the flag byte (bit 4 = GATE_N must be set to play
note; bits 3-0 = duration), and "speed" in the editor is that
duration nibble. Bytes 1/2 are slot_a/slot_b (sidcall starting-row
indices applied when flag bits 6/5 are set).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import keyhandler as kh
from .binmon import BinMon

log = logging.getLogger(__name__)

# Pattern data layout
PATTERN_BASE = 0x1F00
BYTES_PER_VOICE = 4
VOICES_PER_STEP = 3
BYTES_PER_STEP = BYTES_PER_VOICE * VOICES_PER_STEP  # 12

SUB_FIELD_OFFSET = {
    "speed": 0,
    "sidcall1": 1,
    "sidcall2": 2,
    "note": 3,
}

# seqLIST arranger tables (one per voice). Indexed by step (= arranger
# row). Each cell holds a pattern number 0x00..0x7F.
#   SID#1 voices 0/1/2 → $1B00 / $1C00 / $1D00.
#   SID#2 voices 3/4/5 → $6E00 / $6F00 / $7000. (Cursor column 2N / 2N+1
#                        maps to voice N's high/low nibble.)
# Pattern data bank ($1F00 + pat*$80) is SHARED between SID#1 and SID#2;
# the chip distinction is only in which arranger table maps row→pattern.
ARRANGER_BASE = {
    0: 0x1B00,
    1: 0x1C00,
    2: 0x1D00,  # SID#1
    3: 0x6E00,
    4: 0x6F00,
    5: 0x7000,  # SID#2
}
ARRANGER_MAX_STEP = 0x7F

# Cursor variables — canonical mapping (from static disasm of $844C +
# $B27D + $B36B + helper at $8412). Values the writer dispatch actually
# reads to build the ($02) pointer to a (voice, step) struct in pattern
# RAM:
#
#   $71CD  voice selector        voice × 9    ← threshold check at $B27D
#                                                    < 9   → V0 (table $1B00)
#                                                    < $12 → V1 (table $1C00)
#                                                    else  → V2 (table $1D00)
#   $71D2  step within page      0..$1F       ← + $71CE in $B261
#   $71CE  page offset           0 / $20      ← rows beyond visible window
#   $71D1  page-pair counter     increments on $71D2 wrap-around
#   $71CA  iteration count       1 = single cell; >1 loops the writer
#                                ($85B5: DEC $84F3 each iteration)
#   $71CB  inter-iter stride     default 1; only matters when $71CA > 1
#   $71CC  range-fill flag       0 normal; 1 forces row-loop 0..$7F ($84E5)
#   $716D  digit-phase nibble    bit 0 toggled per CBM+digit; 0 = first
#
# Defaults at $85E2 (called on init): $71C8=1, $71C9=0, $71CA=1,
# $71CB=1, $71CC=$0C, $71C1=0. So $71CA=1 is the steady-state and
# range-fill ($71CC=$0C) is the default in some bytes — meaning the
# writer's behaviour on a single keypress is "write one cell, then a
# follow-up cursor step". Our position_cursor explicitly clamps these
# down so writes are predictable.
#
# Cosmetic / screen cursor (don't affect writer's ($02) target):
#   $71CF  visible row counter   (CRSRDOWN: INC $71CF)
#   $7287  column-cursor mod-24  (CRSRRR walks through 24 cell-stops)
#
# Some earlier session notes had wrong labels ($71CF as voice, $7289
# as column cursor, etc). The disassembly is the source of truth.
ADDR_VOICE_SELECTOR = 0x71CD  # voice × 9 (writer-dispatcher voice index)
ADDR_CURSOR_STEP = 0x71D2  # writer-dispatcher's step within page
ADDR_CURSOR_PAGE = 0x71CE  # page offset (added to $71D2 in $B261)
ADDR_CURSOR_PAGE_PAIR = 0x71D1  # page-pair (increments on $71D2 wrap)
ADDR_WRITE_COUNT = 0x71CA  # loop counter for the writer
ADDR_WRITE_STRIDE = 0x71CB  # per-iteration $02 stride
ADDR_RANGE_FILL = 0x71CC  # 0 = single-cell write; non-zero = fill range
ADDR_DIGIT_PHASE = 0x716D  # CBM-digit nibble phase (bit 0)
ADDR_SUPER_FLAGS = 0x71C1  # supercommand flags (mask CTRL+S/R/W/Z/G)

# Cosmetic / screen cursor (don't affect writer's ($02) target):
ADDR_SCREEN_ROW_VIS = 0x71CF  # visible-row counter (0..15 on a page)
ADDR_COL_CURSOR = 0x7287  # CRSRRR mod-24 column position
ADDR_WRITE_OFFSET = 0x71BF  # last write offset (dispatcher-set)

# Mode-init routine. Called as `JSR $0BBA` with A = desired mode value.
ADDR_MODE_INIT_ROUTINE = 0x0BBA

# Internal keycodes (output of $0F90 LUT — verified live):
KC_CRSRRR = 0x6F  # right
KC_CRSRDOWN = 0x6A  # down
KC_LEFTARROW = 0x1F  # toggle to sidTAB / back from sidTAB
KC_RUNSTOP = 0x93  # toggle seqED ↔ seqLIST
KC_SPACE = 0x20
KC_INSTDEL = 0x84
KC_RETURN = 0x0D  # placeholder; actual return code not yet captured

# Note keys (Z..M for the lower octave)
NOTE_KEYCODES = {
    "Z": 0x30,
    "S": 0x31,
    "X": 0x32,
    "D": 0x33,
    "C": 0x34,
    "V": 0x35,
    "G": 0x36,
    "B": 0x37,
    "H": 0x38,
    "N": 0x39,
    "J": 0x3A,
    "M": 0x3B,
}


@dataclass
class FieldWriteResult:
    """Outcome of a high-level field write."""

    ok: bool
    pre_value: int
    post_value: int
    method: str  # "direct_mem" or "chord:<label>"
    detail: Optional[dict] = None


# ---- low-level helpers ------------------------------------------------


def cell_address(voice: int, step: int, sub_field: str) -> int:
    """Return the absolute pattern-memory address of a voice/step cell,
    **assuming the V0/V1/V2 mirroring case** (all three SID#1 voices
    pointing at pattern 0, which lives at $1F00). This is the
    fresh-loaded-tune layout.

    For SID#2 voices, or for tunes where voices reference distinct
    patterns, use `runtime_cell_address(bm, voice, step, sub_field,
    arranger_row)` instead — that path reads the arranger + the
    `$1A00/$1A80` pattern-base table at runtime."""
    if voice not in (0, 1, 2):
        raise ValueError(
            f"voice must be 0/1/2 (use runtime_cell_address "
            f"for voices 3-5 or non-mirroring arranger state); "
            f"got {voice}"
        )
    if sub_field not in SUB_FIELD_OFFSET:
        raise ValueError(f"sub_field must be one of {list(SUB_FIELD_OFFSET)}")
    return (
        PATTERN_BASE
        + step * BYTES_PER_STEP
        + voice * BYTES_PER_VOICE
        + SUB_FIELD_OFFSET[sub_field]
    )


# ---- runtime address resolution (works for ANY voice + arranger state) -

# Pattern-base lookup tables: $1A00 = pat→base low byte; $1A80 = high
# byte. defMON populates these at boot from the loaded tune; the writer
# dispatcher reads them on every cell write (via the self-modifying ADC
# immediate at $8504).
ADDR_PAT_BASE_LO = 0x1A00
ADDR_PAT_BASE_HI = 0x1A80


def pattern_base_for(bm: BinMon, pat_num: int) -> int:
    """Return the runtime base address for pattern ``pat_num`` (0-0x7F),
    by reading ``$1A00,X / $1A80,X``."""
    if not 0 <= pat_num <= 0x7F:
        raise ValueError(f"pat_num must be 0..0x7F; got {pat_num:#x}")
    lo = bm.mem_get(ADDR_PAT_BASE_LO + pat_num, ADDR_PAT_BASE_LO + pat_num)[0]
    hi = bm.mem_get(ADDR_PAT_BASE_HI + pat_num, ADDR_PAT_BASE_HI + pat_num)[0]
    return (hi << 8) | lo


def voice_pattern_base(bm: BinMon, voice: int, arranger_row: int = 0) -> int:
    """Return the runtime pattern base for ``(voice, arranger_row)``.

    Reads the arranger table for ``voice`` at ``arranger_row`` to get
    the pattern number, then dereferences the $1A00/$1A80 table to get
    the per-pattern base address.

    Works for all 6 voices: V0/V1/V2 via SID#1 arrangers
    ($1B00/$1C00/$1D00), V3/V4/V5 via SID#2 arrangers
    ($6E00/$6F00/$7000)."""
    arr_addr = arranger_cell_address(voice, arranger_row)
    pat_num = bm.mem_get(arr_addr, arr_addr)[0]
    return pattern_base_for(bm, pat_num)


def runtime_cell_address(
    bm: BinMon, voice: int, step: int, sub_field: str, arranger_row: int = 0
) -> int:
    """Return the absolute pattern-memory address of a (voice, step,
    sub_field) cell using runtime arranger + pattern-table lookup.

    Resolves via:
      pat_num   = arranger[voice][arranger_row]   ($1B/$1C/$1D/$6E/$6F/$70)
      pat_base  = ($1A80+pat_num << 8) | $1A00+pat_num
      cell_addr = pat_base + step*4 + sub_field_offset

    Works for all 6 voices (SID#1 V0/V1/V2 + SID#2 V3/V4/V5).
    Step is the pattern step (0..31; 4 bytes per step, $80 per pattern).
    """
    if voice not in ARRANGER_BASE:
        raise ValueError(f"voice must be 0..5, got {voice}")
    if sub_field not in SUB_FIELD_OFFSET:
        raise ValueError(f"sub_field must be one of {list(SUB_FIELD_OFFSET)}")
    pat_base = voice_pattern_base(bm, voice, arranger_row)
    return pat_base + step * BYTES_PER_VOICE + SUB_FIELD_OFFSET[sub_field]


def arranger_cell_address(voice: int, step: int) -> int:
    """Return the absolute arranger-table address for a (voice, row) cell.

    seqLIST writes target the arranger pattern-number tables, indexed
    by row (= step, 0..0x7F). Each byte is a pattern number 0x00..0x7F.
      SID#1 voices 0/1/2 → $1B00 / $1C00 / $1D00
      SID#2 voices 3/4/5 → $6E00 / $6F00 / $7000
    """
    if voice not in ARRANGER_BASE:
        raise ValueError(f"voice must be 0..5, got {voice}")
    if not 0 <= step <= ARRANGER_MAX_STEP:
        raise ValueError(f"step must be 0..0x7F, got {step}")
    return ARRANGER_BASE[voice] + step


def read_cell(
    bm: BinMon, voice: int, step: int, sub_field: str, arranger_row: int | None = None
) -> int:
    """Read a cell byte. Voice/arranger-row resolution mirrors
    ``write_cell_direct``: ``arranger_row=None`` + voice in 0..2 uses
    the simple pattern-0 layout; otherwise resolves via the arranger
    + ``$1A00/$1A80`` table at runtime."""
    if arranger_row is None and voice in (0, 1, 2):
        addr = cell_address(voice, step, sub_field)
    else:
        addr = runtime_cell_address(
            bm, voice, step, sub_field, arranger_row=arranger_row or 0
        )
    return bm.mem_get(addr, addr)[0]


def read_arranger_cell(bm: BinMon, voice: int, step: int) -> int:
    addr = arranger_cell_address(voice, step)
    return bm.mem_get(addr, addr)[0]


# ---- direct-memory writes (fastest, bypass everything) ----------------


def write_cell_direct(
    bm: BinMon,
    voice: int,
    step: int,
    sub_field: str,
    value: int,
    arranger_row: int | None = None,
) -> FieldWriteResult:
    """Write a single cell byte by mem_set into the pattern buffer.

    ``arranger_row=None`` (default) uses the simple ``cell_address``
    helper that assumes V0/V1/V2 share pattern 0 at ``$1F00`` and lays
    out 12 bytes per step (V0..V2 ×4 bytes).

    When ``arranger_row`` is given (or ``voice >= 3``), the cell is
    resolved at runtime via the arranger → ``$1A00/$1A80`` →
    pattern-base path. Required for SID#2 voices (3-5) and for tunes
    whose arranger row references distinct patterns per voice."""
    if arranger_row is None and voice in (0, 1, 2):
        addr = cell_address(voice, step, sub_field)
        method = "direct_mem"
    else:
        addr = runtime_cell_address(
            bm, voice, step, sub_field, arranger_row=arranger_row or 0
        )
        method = f"direct_mem:runtime(arr_row={arranger_row or 0})"
    pre = bm.mem_get(addr, addr)[0]
    bm.mem_set(addr, bytes([value & 0xFF]))
    post = bm.mem_get(addr, addr)[0]
    return FieldWriteResult(
        ok=(post == (value & 0xFF)),
        pre_value=pre,
        post_value=post,
        method=method,
        detail={"addr": addr},
    )


def write_pattern_block_direct(
    bm: BinMon, step: int, voice: int, bytes_data: bytes
) -> None:
    """Bulk-write a (voice, step) cell as 4 raw bytes (flag/slot_a/slot_b/note)."""
    if len(bytes_data) != BYTES_PER_VOICE:
        raise ValueError(f"need exactly {BYTES_PER_VOICE} bytes; got {len(bytes_data)}")
    addr = PATTERN_BASE + step * BYTES_PER_STEP + voice * BYTES_PER_VOICE
    bm.mem_set(addr, bytes_data)


def write_arranger_cell_direct(
    bm: BinMon, voice: int, step: int, value: int
) -> FieldWriteResult:
    """Write an arranger pattern-number byte by mem_set.

    SID#1 voices 0/1/2 → $1B00/$1C00/$1D00 + row.
    SID#2 voices 3/4/5 → $6E00/$6F00/$7000 + row."""
    addr = arranger_cell_address(voice, step)
    pre = bm.mem_get(addr, addr)[0]
    bm.mem_set(addr, bytes([value & 0xFF]))
    post = bm.mem_get(addr, addr)[0]
    return FieldWriteResult(
        ok=(post == (value & 0xFF)), pre_value=pre, post_value=post, method="direct_mem"
    )


# ---- mode switching ---------------------------------------------------


def current_mode(bm: BinMon) -> int:
    return bm.mem_get(0x7167, 0x7167)[0]


def set_mode_chord(bm: BinMon, mode: str) -> None:
    """Switch via the keypress chord that defMON itself uses.

    Validated chords (see static disasm):
      - From seqED:    LEFTARROW ($1F) → sidTAB ($04); RUNSTOP/$93 → seqLIST
      - From seqLIST:  $1F → sidTAB; $93 → seqED
      - From sidTAB:   $1F → previous-mode (saved at $7168); $93 → seqLIST
    """
    cur = current_mode(bm)
    target = kh.MODE_VAL[mode]
    if cur == target:
        return
    # Use the global mode-init routine through a trampoline:
    # `LDA #target; JSR $0BBA; RTS` at $CFE0..
    stub = bytes(
        [
            0xA9,
            target,
            0x20,
            ADDR_MODE_INIT_ROUTINE & 0xFF,
            (ADDR_MODE_INIT_ROUTINE >> 8) & 0xFF,
            0xEA,
            0xEA,
            0xEA,
        ]
    )
    with bm.halted():
        bm.run_until_pc(kh.LOOP_TOP, timeout=3.0)
        original = bm.mem_get(kh.TRAMPOLINE_BASE, kh.TRAMPOLINE_BASE + len(stub) - 1)
        bm.mem_set(kh.TRAMPOLINE_BASE, stub)
        bm.registers_set({kh.REG_PC: kh.TRAMPOLINE_BASE})
        try:
            bm.run_until_pc(kh.TRAMPOLINE_BASE + 5, timeout=3.0)
        finally:
            bm.mem_set(kh.TRAMPOLINE_BASE, original)
    if current_mode(bm) != target:
        raise RuntimeError(
            f"set_mode_chord: requested {mode}=${target:02X}, "
            f"$7167 still ${current_mode(bm):02X}"
        )


def set_mode_direct(bm: BinMon, mode: str) -> None:
    """Set $7167 directly. Skips the per-mode init at $0BBA — useful when
    you want to *observe* the mode value influence dispatch without
    triggering screen redraw / saved-state restore."""
    bm.mem_set(0x7167, bytes([kh.MODE_VAL[mode]]))


# ---- cursor positioning -----------------------------------------------

VOICE_SELECTOR_VALUES = (0x00, 0x09, 0x12)  # $71CD value per voice
VOICE_FROM_SELECTOR = {0x00: 0, 0x09: 1, 0x12: 2}


def cursor_state(bm: BinMon) -> dict:
    """Snapshot the cursor variables that determine the writer target."""
    addrs = [
        ADDR_VOICE_SELECTOR,
        ADDR_CURSOR_STEP,
        ADDR_CURSOR_PAGE,
        ADDR_CURSOR_PAGE_PAIR,
        ADDR_WRITE_COUNT,
        ADDR_WRITE_STRIDE,
        ADDR_RANGE_FILL,
        ADDR_DIGIT_PHASE,
        ADDR_SUPER_FLAGS,
        ADDR_SCREEN_ROW_VIS,
        ADDR_COL_CURSOR,
        ADDR_WRITE_OFFSET,
    ]
    data = {a: bm.mem_get(a, a)[0] for a in addrs}
    sel = data[ADDR_VOICE_SELECTOR]
    return {
        "voice_selector": sel,
        "voice": VOICE_FROM_SELECTOR.get(sel),  # None if non-canonical
        "step": data[ADDR_CURSOR_STEP],
        "page": data[ADDR_CURSOR_PAGE],
        "page_pair": data[ADDR_CURSOR_PAGE_PAIR],
        "write_count": data[ADDR_WRITE_COUNT],
        "write_stride": data[ADDR_WRITE_STRIDE],
        "range_fill": data[ADDR_RANGE_FILL],
        "digit_phase": data[ADDR_DIGIT_PHASE],
        "super_flags": data[ADDR_SUPER_FLAGS],
        "row_vis": data[ADDR_SCREEN_ROW_VIS],
        "col": data[ADDR_COL_CURSOR],
        "write_off": data[ADDR_WRITE_OFFSET],
    }


def position_cursor(
    bm: BinMon,
    voice: int,
    step: int,
    *,
    reset_digit_phase: bool = True,
    verify: bool = True,
) -> None:
    """Place the writer-dispatcher cursor on (voice, step) deterministically.

    Sets the variables the writer-dispatcher at $844C / index helper at
    $B27D actually read when building its ``($02)`` pointer:

      $71CD ← voice × 9         (selects per-voice index table $1B/$1C/$1D)
      $71D2 ← step              (within current page)
      $71CE ← 0                 (page offset; we always target page 0)
      $71D1 ← 0                 (page-pair counter; clear to avoid drift)
      $71CA ← 1                 (writer loop runs once → single-cell)
      $71CB ← 1                 (default stride; immaterial at $71CA=1)
      $71CC ← 0                 (range-fill OFF; defMON's defaults set
                                  $71CC=$0C which would tail-loop)
      $71C1 ← 0                 (clear supercommand flag mask)

    Optionally resets $716D (nibble phase) so the next CBM+digit press
    is treated as "first digit" (writes the high nibble of sidCALL).

    This does NOT update the visible screen cursor ($71CF/$7287); those
    are display-only.  A subsequent chord-driven write will land at the
    requested cell regardless.

    Raises RuntimeError if `verify=True` and the variables don't read
    back the requested values.
    """
    if voice not in (0, 1, 2):
        raise ValueError(f"voice must be 0/1/2, got {voice}")
    if not 0 <= step <= 0x1F:
        raise ValueError(f"step must be 0..31, got {step}")

    bm.mem_set(ADDR_VOICE_SELECTOR, bytes([VOICE_SELECTOR_VALUES[voice]]))
    bm.mem_set(ADDR_CURSOR_STEP, bytes([step]))
    bm.mem_set(ADDR_CURSOR_PAGE, bytes([0]))
    bm.mem_set(ADDR_CURSOR_PAGE_PAIR, bytes([0]))
    bm.mem_set(ADDR_WRITE_COUNT, bytes([1]))
    bm.mem_set(ADDR_WRITE_STRIDE, bytes([1]))
    bm.mem_set(ADDR_RANGE_FILL, bytes([0]))
    bm.mem_set(ADDR_SUPER_FLAGS, bytes([0]))
    if reset_digit_phase:
        bm.mem_set(ADDR_DIGIT_PHASE, bytes([0]))

    if verify:
        got = cursor_state(bm)
        if got["voice"] != voice or got["step"] != step or got["page"] != 0:
            raise RuntimeError(
                f"position_cursor verify failed: requested (V{voice}, S{step}); "
                f"got voice_selector=${got['voice_selector']:02X} "
                f"step=${got['step']:02X} page=${got['page']:02X}"
            )


def cursor_step_keypress(bm: BinMon, n: int = 1) -> None:
    """Direct-press CRSRDOWN n times (or CRSRUP if n<0). Cosmetic walk —
    does NOT reliably update the writer-dispatcher's $71CB/$71D2; use
    position_cursor() for write-target placement."""
    kc = KC_CRSRDOWN
    mod = kh.MOD_NONE if n > 0 else kh.MOD_SHIFT
    for _ in range(abs(n)):
        kh.press_via_loop(bm, "seqed", keycode=kc, mod1=mod, wait_timeout=2.0)


def cursor_right_keypress(bm: BinMon, n: int = 1) -> None:
    """Direct-press CRSRRR n times (or CRSRLL if n<0). Cosmetic; see
    cursor_step_keypress."""
    mod = kh.MOD_NONE if n > 0 else kh.MOD_SHIFT
    for _ in range(abs(n)):
        kh.press_via_loop(bm, "seqed", keycode=KC_CRSRRR, mod1=mod, wait_timeout=2.0)


# Short-form aliases.
cursor_step = cursor_step_keypress
cursor_right = cursor_right_keypress


# ---- chord-driven field writes ---------------------------------------


def write_note_chord(bm: BinMon, key: str) -> FieldWriteResult:
    """Direct-call a bare note key (Z..M, S/D/G/H/J for sharps).
    Writes the corresponding pitch byte at the current cursor + auto-advances."""
    if key.upper() not in NOTE_KEYCODES:
        raise ValueError(f"not a note key: {key!r}; valid: {list(NOTE_KEYCODES)}")
    kc = NOTE_KEYCODES[key.upper()]
    # We can't cleanly say "this maps to (voice, step, sub_field=note)"
    # without first finding cursor; chord-driven writes use whatever
    # cursor state is current. Caller is responsible for pre-positioning.
    addr_estimate = PATTERN_BASE + bm.mem_get(ADDR_WRITE_OFFSET, ADDR_WRITE_OFFSET)[0]
    pre = bm.mem_get(addr_estimate, addr_estimate)[0]
    res = kh.press_via_loop(bm, "seqed", keycode=kc, mod1=kh.MOD_NONE, wait_timeout=2.0)
    post = bm.mem_get(addr_estimate, addr_estimate)[0]
    return FieldWriteResult(
        ok=True,
        pre_value=pre,
        post_value=post,
        method=f"chord:note({key})",
        detail={"res": res},
    )


def write_hex_digit_chord(bm: BinMon, digit: int, modifier: int) -> kh.HandlerResult:
    """Direct-call a hex-digit chord (CBM+digit / CBM+SHIFT+digit / CTRL+CBM+digit).

    `digit` is 0..15 (the actual hex value). `modifier` is one of
    kh.MOD_CBM / kh.MOD_CBM_SHIFT / kh.MOD_CTRL_CBM.

    Internal-keycode mapping per static disasm at $AF14/$AFD1/$AFEA:
      digits 0..9 → kc $30..$39 (BCC #$30 path -> AND #$0F)
      digits A..F → kc $01..$06 (BCC #$07 path -> ADC #$09)
    """
    if not 0 <= digit <= 15:
        raise ValueError(f"digit must be 0..15, got {digit}")
    if digit <= 9:
        kc = 0x30 + digit
    else:
        kc = digit - 9  # A->1, B->2, ..., F->6
    return kh.press_via_loop(bm, "seqed", keycode=kc, mod1=modifier, wait_timeout=2.0)


def write_byte_chord(bm: BinMon, value: int, sub_field: str) -> list[kh.HandlerResult]:
    """Write an 8-bit byte at the current cursor by sending two hex-digit
    chords (high nibble then low nibble), using the chord that targets
    the named sub_field.

    Mapping (from the static dispatch tree at
    $AE85/$AF03/$B134/$B06E + sidCALL writer at $B396):

      sub_field    chord                static-disasm reasoning
      sidcall1     CBM+digit            $AF03 takes CBM-only ($0E41==$20),
                                        ADC #$09, JSR $844C(X=$96 Y=$B3
                                        → JSR $B396). $B396 sets X=1
                                        (offset+1 = sidCALL1) for CBM-only.
      sidcall2     CBM+digit            same writer ($B396) sets X=2 when
                       w/ second-digit  mod != $20 — but the only path to
                       phase             $B396 in the dispatch is from
                                        CBM-only, so sidCALL2 is reached
                                        by typing a SECOND digit (the
                                        writer auto-tracks via $716D).
      speed        CTRL+CBM+digit       $B177 takes CTRL+CBM ($0E41==$24),
                                        $B187 LDX #$FF Y=$B3 → JSR $B3FF.
                                        Auto-advances voice/step (writer
                                        increments $71D2 + voice/step
                                        cursor on each call).
      note         (use write_note_chord — bare key, $AEB3 LDA $729A,Y
                                          ADC $71D3 octave, JSR $B3DF)

    What CBM+SHIFT does NOT do: per static dispatch ($B134 takes X=$30
    but only handles CRSRRR/INSTDEL/SPACE/$18/$16 — there's no digit
    handler in that branch). Empirically CBM+SHIFT+digit produces no
    pattern write, only $71CD changes. The wiki's "LSHIFT+CBM = sound
    program" claim is wrong; the actual sidCALL chord is CBM-alone.
    """
    chord_for = {
        # Both sidcall1 and sidcall2 map to the same press chord — the
        # writer alternates target offset based on $716D (digit-pair
        # phase). Use write_byte_chord(value, "sidcall") for a full byte.
        "sidcall1": kh.MOD_CBM,
        "sidcall2": kh.MOD_CBM,
        "sidcall": kh.MOD_CBM,
        "speed": kh.MOD_CTRL_CBM,
    }
    if sub_field not in chord_for:
        raise ValueError(
            f"sub_field must be one of {list(chord_for)} for hex chord; "
            f"got {sub_field!r}"
        )
    mod = chord_for[sub_field]
    hi = (value >> 4) & 0xF
    lo = value & 0xF
    out = []
    out.append(write_hex_digit_chord(bm, hi, mod))
    out.append(write_hex_digit_chord(bm, lo, mod))
    return out


# ---- the deliverable: set_field ---------------------------------------


def set_field(
    bm: BinMon,
    mode: str,
    voice: int,
    step: int,
    sub_field: str,
    value: int,
    *,
    prefer_direct: bool = True,
    arranger_row: int | None = None,
) -> FieldWriteResult:
    """Write a field cell to a specific (mode, voice, step, sub_field).

    Supported modes:
      - `seqed`   — pattern cells; sub_field in
                    {speed, sidcall1, sidcall2, note}.
                       SID#1 voices 0/1/2 (legacy path: assumes pattern 0
                          at $1F00 with 12-byte step stride.)
                       SID#2 voices 3/4/5 (runtime path: reads SID#2
                          arranger $6E00/$6F00/$7000 → $1A00/$1A80
                          pattern-base table → pattern_base + step*4
                          + sub_field_offset.)
                    Pass ``arranger_row`` to force runtime resolution
                    for any voice (needed when SID#1 voices reference
                    distinct patterns or you want to target a non-zero
                    arranger row).
      - `seqlist` — arranger pattern-number cells. sub_field is
                    "pattern_num" (the only field).
                       SID#1 voices 0/1/2 → $1B00 / $1C00 / $1D00
                       SID#2 voices 3/4/5 → $6E00 / $6F00 / $7000
                    (voice, step) is the arranger voice 0..5 + row 0..0x7F.
      - `sidtab`  — use `defmon_driver.sidtab.SidTab.set(row, column, value)`;
                    the sidTAB write path goes through a column-cursor-
                    indexed working buffer ($BDA4) that requires
                    per-tune calibration. This function raises
                    NotImplementedError for sidtab mode.
      - `disk`    — disk-menu fields are filename / pack-name inputs,
                    driven via `Defmon.disk_save_new` etc. Raises
                    NotImplementedError.

    By default writes via mem_set (fastest, most reliable). Pass
    prefer_direct=False to force the chord-driven path (which exercises
    defMON's actual edit logic). For chord-driven writes the caller is
    responsible for ensuring the cursor is on the target cell first;
    this function does NOT navigate."""
    if mode == "seqed":
        return _set_field_seqed(
            bm,
            voice,
            step,
            sub_field,
            value,
            prefer_direct=prefer_direct,
            arranger_row=arranger_row,
        )
    if mode == "seqlist":
        return _set_field_seqlist(
            bm, voice, step, sub_field, value, prefer_direct=prefer_direct
        )
    if mode == "sidtab":
        raise NotImplementedError(
            "set_field(mode='sidtab', ...): use defmon_driver.sidtab.SidTab "
            "instead — sidTAB writes go through a calibration-dependent "
            "working buffer at $BDA4 that this function does not model. "
            "For chord-driven dispatch (no field semantics) use "
            "keyhandler.press_via_loop(mode='sidtab', ...)."
        )
    if mode == "disk":
        raise NotImplementedError(
            "set_field(mode='disk', ...): use Defmon.disk_save_new / "
            "disk_load_via_cursor / etc. for high-level disk operations. "
            "For chord-driven dispatch (e.g. typing a filename letter) "
            "use keyhandler.press_via_loop(mode='disk', ...)."
        )
    raise ValueError(
        f"unknown mode {mode!r}; expected one of " "seqed|seqlist|sidtab|disk"
    )


def _set_field_seqed(
    bm: BinMon,
    voice: int,
    step: int,
    sub_field: str,
    value: int,
    *,
    prefer_direct: bool,
    arranger_row: int | None = None,
) -> FieldWriteResult:
    if prefer_direct:
        return write_cell_direct(
            bm, voice, step, sub_field, value, arranger_row=arranger_row
        )
    # Chord path. For SID#2 voices (3-5) the arranger-proxy mechanic
    # in keyhandler.press_via_loop temporarily routes the SID#1 V0/V1/V2
    # writer through the target SID#2 pattern.
    if voice not in range(6):
        raise ValueError(f"voice must be 0..5, got {voice}")

    arr_row = arranger_row or 0

    if sub_field == "note":
        # Single press — the simple case. Use cursor=(voice, step) so
        # the proxy swap fires for V3-V5.
        rev = {v: k for k, v in NOTE_KEYCODES.items()}
        if value not in rev:
            raise NotImplementedError(
                f"chord-driven note write only handles values in "
                f"{sorted(NOTE_KEYCODES.values())}; got {value:#x}"
            )
        kc = NOTE_KEYCODES[rev[value]]
        addr = (
            cell_address(voice, step, sub_field)
            if voice < 3 and arranger_row is None
            else runtime_cell_address(bm, voice, step, sub_field, arranger_row=arr_row)
        )
        pre = bm.mem_get(addr, addr)[0]
        kh.press_via_loop(
            bm,
            "seqed",
            keycode=kc,
            mod1=kh.MOD_NONE,
            cursor=(voice, step),
            arranger_row=arr_row,
            set_mode=True,
            wait_timeout=3.0,
        )
        actual = bm.mem_get(addr, addr)[0]
        return FieldWriteResult(
            ok=(actual == (value & 0xFF)),
            pre_value=pre,
            post_value=actual,
            method=f"chord:note={value:#x}",
            detail={"addr": addr},
        )

    # Multi-press chords (sidCALL high+low, speed 2 nibbles). The
    # writer auto-advances $716D / step on each press, so re-positioning
    # the cursor between presses would clobber the first nibble.
    #   - SID#1 V0/V1/V2: caller is responsible for placing the cursor;
    #     write_byte_chord just emits two presses with no cursor reset.
    #   - SID#2 V3/V4/V5: needs persistent arranger proxy AND a single
    #     cursor seed shared by both presses — Sid2Chord holds both.
    chord_mod_for = {
        "sidcall1": kh.MOD_CBM,
        "sidcall2": kh.MOD_CBM,
        "sidcall": kh.MOD_CBM,
        "speed": kh.MOD_CTRL_CBM,
    }
    if sub_field not in chord_mod_for:
        raise ValueError(
            f"chord-driven seqED sub_field must be one of "
            f"{list(chord_mod_for) + ['note']}; got {sub_field!r}"
        )

    if voice in (0, 1, 2):
        write_byte_chord(bm, value, sub_field)
        actual = read_cell(bm, voice, step, sub_field, arranger_row=arranger_row)
        return FieldWriteResult(
            ok=(actual == (value & 0xFF)),
            pre_value=0,
            post_value=actual,
            method=f"chord:{sub_field}={value:#x}",
        )

    # SID#2 multi-press path.
    mod = chord_mod_for[sub_field]
    hi = (value >> 4) & 0xF
    lo = value & 0xF
    hi_kc = (0x30 + hi) if hi <= 9 else (hi - 9)
    lo_kc = (0x30 + lo) if lo <= 9 else (lo - 9)
    addr = runtime_cell_address(bm, voice, step, sub_field, arranger_row=arr_row)
    pre = bm.mem_get(addr, addr)[0]
    with kh.Sid2Chord(bm, voice, step, arranger_row=arr_row) as ctx:
        ctx.press(hi_kc, mod1=mod)
        ctx.press(lo_kc, mod1=mod)
    actual = bm.mem_get(addr, addr)[0]
    return FieldWriteResult(
        ok=(actual == (value & 0xFF)),
        pre_value=pre,
        post_value=actual,
        method=f"chord:sid2:{sub_field}={value:#x}",
        detail={"addr": addr},
    )


def _set_field_seqlist(
    bm: BinMon,
    voice: int,
    step: int,
    sub_field: str,
    value: int,
    *,
    prefer_direct: bool,
) -> FieldWriteResult:
    if sub_field != "pattern_num":
        raise ValueError(
            f"seqlist sub_field must be 'pattern_num' (the only field "
            f"in the arranger); got {sub_field!r}"
        )
    if not 0 <= value <= 0x7F:
        raise ValueError(f"arranger pattern_num must be 0..0x7F; got {value:#x}")
    if prefer_direct:
        return write_arranger_cell_direct(bm, voice, step, value)
    # Chord path: requires cursor pre-positioned on (voice column, row).
    # The seqLIST hex-digit writer ($E211) ORs one nibble at a time and
    # uses cursor column ($7286) to decide which voice column; the
    # caller is responsible for placing the cursor. Two presses for a
    # full byte (high nibble then low).
    hi = (value >> 4) & 0xF
    lo = value & 0xF
    hi_kc = (0x30 + hi) if hi <= 9 else (hi - 9)  # 0..9 → $30..$39; A..F → $01..$06
    lo_kc = (0x30 + lo) if lo <= 9 else (lo - 9)
    kh.press_via_loop(bm, "seqlist", keycode=hi_kc, mod1=kh.MOD_NONE, wait_timeout=2.0)
    kh.press_via_loop(bm, "seqlist", keycode=lo_kc, mod1=kh.MOD_NONE, wait_timeout=2.0)
    actual = read_arranger_cell(bm, voice, step)
    return FieldWriteResult(
        ok=(actual == (value & 0xFF)),
        pre_value=0,
        post_value=actual,
        method=f"chord:seqlist:pattern_num={value:#x}",
    )
