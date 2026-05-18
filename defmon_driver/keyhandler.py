"""Direct invocation of defMON's keyboard dispatch routines.

Bypasses the keymatrix-tap path entirely. Sets state ($7167 mode, $0E44
key, $0E41/$0E42 modifier flags), then calls the mode handler subroutine
directly via a tiny trampoline stub installed in RAM. Returns when the
trampoline's checkpoint fires.

Map:

    Mode byte $7167   Handler
    $01 = seqED       $AE78
    $02 = seqLIST     $E550
    $04 = sidTAB      $BBB5
    $20 = disk menu   $C491

Modifier flags (built by $0F32 from the matrix mirror $0E39..$0E40):

    $0E41 bit $04 = CTRL
    $0E41 bit $10 = LSHIFT or RSHIFT
    $0E41 bit $20 = CBM (Commodore key)
    $0E42 bit $01 = COLON   (voice-mute toggle for V0)
    $0E42 bit $02 = SEMICOLON (voice-mute toggle for V1)
    $0E42 bit $04 = EQUALS    (voice-mute toggle for V2)

Internal keycodes (output of $0F90,Y matrix-slot → keycode LUT) are
NOT the matrix (row,col) — they are defMON's internal representation,
captured by the live keymatrix scan and stored at $0E44. The specific
mapping can be discovered by experiment (issue a real key tap, then
mem_get $0E44; see :func:`capture_internal_keycode`).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .binmon import BinMon

# --- defMON RAM addresses ---
ADDR_MODE = 0x7167  # current modal context
ADDR_KEY = 0x0E44  # debounced current key (output of $0E47 scanner)
ADDR_PREV_KEY = 0x0E45  # previous-key register (used by debounce)
ADDR_MOD1 = 0x0E41  # CTRL/SHIFT/CBM flags
ADDR_MOD2 = 0x0E42  # COLON/SEMICOLON/EQUALS flags
ADDR_REPEAT_CTR = 0x0E46  # debounce/repeat counter

# Mode handlers (set PC here to dispatch one keystroke):
HANDLER_SEQED = 0xAE78
HANDLER_SEQLIST = 0xE550
HANDLER_SIDTAB = 0xBBB5
HANDLER_DISK = 0xC491

MODE_VAL = {"seqed": 0x01, "seqlist": 0x02, "sidtab": 0x04, "disk": 0x20}
MODE_HANDLER = {
    "seqed": HANDLER_SEQED,
    "seqlist": HANDLER_SEQLIST,
    "sidtab": HANDLER_SIDTAB,
    "disk": HANDLER_DISK,
}

# Modifier-flag combinations:
MOD_NONE = 0x00
MOD_CTRL = 0x04
MOD_SHIFT = 0x10
MOD_CBM = 0x20
MOD_CBM_SHIFT = MOD_CBM | MOD_SHIFT
MOD_CTRL_CBM = MOD_CTRL | MOD_CBM

# Trampoline location. We park it at $CFE0..$CFEF (in the cassette buffer
# region $0334-$03FB on stock C64; defMON uses RAM-under-I/O so $Cxxx is
# RAM. $CF00 is unused per the static-vs-live diff). We patch the JSR
# operand bytes to point at the desired handler.
TRAMPOLINE_BASE = 0xCFE0
# Layout:
#   CFE0  20 LO HI    JSR <handler>       (3 bytes)
#   CFE3  EA          NOP                 (landing pad for stop-on-hit cp)
TRAMP_JSR = TRAMPOLINE_BASE
TRAMP_LAND = TRAMPOLINE_BASE + 3

# CPU register IDs in VICE binmon protocol (monitor.h):
REG_A = 0
REG_X = 1
REG_Y = 2
REG_PC = 3
REG_SP = 4
REG_FLAGS = 5


@dataclass
class HandlerResult:
    """Snapshot taken after a direct-call handler returned."""

    pre_pattern: bytes  # 16 bytes from $1f00 before the call
    post_pattern: bytes  # 16 bytes from $1f00 after the call
    pre_screen_strip: bytes  # 80 bytes from a slice of screen RAM (cursor row)
    post_screen_strip: bytes
    pre_state: dict[int, int]  # snapshot of cursor-related ZP/RAM
    post_state: dict[int, int]
    pc_at_stop: int
    cycles_estimated: int


# Cursor-state addresses we snapshot to detect cursor moves:
STATE_ADDRS = (
    0x7286,
    0x7287,
    0x7288,
    0x7289,
    0x728A,
    0x71BE,
    0x71BF,
    0x71C0,
    0x71C1,
    0x71C2,
    0x71C3,
    0x71CB,
    0x71CD,
    0x71CE,
    0x71D2,
    0x71D3,
    0x716D,
)

# Pattern data span in defMON RAM. Cover at least 16 bytes (V0-V2 step
# 0 = 12 bytes + slack); 64 bytes captures the first 4 steps too.
PATTERN_BASE = 0x1F00
PATTERN_LEN = 64


def install_trampoline(bm: BinMon) -> None:
    """Write the trampoline stub. Idempotent — safe to call before each press."""
    # JSR $0000 placeholder (the operand is patched per call)
    stub = bytes([0x20, 0x00, 0x00, 0xEA, 0xEA, 0xEA])
    bm.mem_set(TRAMPOLINE_BASE, stub)


def _patch_handler(bm: BinMon, handler_addr: int) -> None:
    bm.mem_set(TRAMP_JSR + 1, bytes([handler_addr & 0xFF, (handler_addr >> 8) & 0xFF]))


def _snapshot_state(bm: BinMon) -> dict[int, int]:
    out: dict[int, int] = {}
    for addr in STATE_ADDRS:
        out[addr] = bm.mem_get(addr, addr)[0]
    return out


def press(
    bm: BinMon,
    mode: str,
    keycode: int,
    mod1: int = MOD_NONE,
    mod2: int = MOD_NONE,
    *,
    set_mode: bool = False,
    screen_strip: tuple[int, int] = (0x05F0, 0x0640),
    run_timeout: float = 2.0,
    verbose: bool = False,
) -> HandlerResult:
    """Issue one direct keypress.

    Sets ($7167, $0E44, $0E41, $0E42), then calls the mode handler via
    the trampoline at $CFE0. Captures pattern bytes ($1F00) and a strip
    of screen RAM before/after.

    `keycode` is defMON's *internal* keycode (the value $0E44 takes
    after the $0F90 matrix-slot LUT). For note keys this is the wiki's
    PETSCII-ish mapping (`Z`→$30, `S`→$31, ...). For digit keys see the
    $0F90 LUT (run dump_keycode_lut() to inspect).

    `mod1` / `mod2` are the OR'd modifier-flag bits ($MOD_CTRL etc.).

    `set_mode=True` writes $7167 = MODE_VAL[mode] before dispatching.
    Default False — caller is expected to already be in the right mode
    (real key taps put the cursor where it needs to be)."""

    if mode not in MODE_HANDLER:
        raise ValueError(f"unknown mode {mode!r}")

    install_trampoline(bm)
    _patch_handler(bm, MODE_HANDLER[mode])

    # Pre-snapshot
    pre_pattern = bm.mem_get(PATTERN_BASE, PATTERN_BASE + PATTERN_LEN - 1)
    pre_screen = bm.mem_get(screen_strip[0], screen_strip[1])
    pre_state = _snapshot_state(bm)

    # Set up state
    if set_mode:
        bm.mem_set(ADDR_MODE, bytes([MODE_VAL[mode]]))
    bm.mem_set(ADDR_KEY, bytes([keycode & 0xFF]))
    bm.mem_set(
        ADDR_PREV_KEY, bytes([(keycode ^ 0xFF) & 0xFF])
    )  # different from key, defeats debounce
    bm.mem_set(ADDR_MOD1, bytes([mod1 & 0xFF]))
    bm.mem_set(ADDR_MOD2, bytes([mod2 & 0xFF]))
    bm.mem_set(ADDR_REPEAT_CTR, bytes([0x01]))  # not in repeat-suppress

    # Set PC to the trampoline JSR.
    bm.registers_set({REG_PC: TRAMP_JSR})

    if verbose:
        tramp_bytes = bm.mem_get(TRAMPOLINE_BASE, TRAMPOLINE_BASE + 5)
        print(
            f"    [verbose] $CFE0..CFE5 = {tramp_bytes.hex()}; "
            f"$01 = ${bm.mem_get(0x01, 0x01).hex()}; "
            f"PC pre-run = ${bm.registers_get()[REG_PC]:04X}"
        )

    # Run until the NOP after JSR fires.
    start = time.monotonic()
    try:
        bm.run_until_pc(TRAMP_LAND, timeout=run_timeout)
    except Exception:
        # Capture the actual PC at timeout for diagnosis.
        regs = bm.registers_get()
        if verbose:
            print(
                f"    [verbose] TIMEOUT: PC=${regs.get(REG_PC, 0):04X}  "
                f"A=${regs.get(REG_A, 0):02X}  X=${regs.get(REG_X, 0):02X}  "
                f"Y=${regs.get(REG_Y, 0):02X}  SP=${regs.get(REG_SP, 0):02X}"
            )
            # Recent CPU history
            try:
                hist = bm.cpuhistory_get(20)
                print(
                    "    [verbose] Last 20 PCs: "
                    + " ".join(f"${h.pc:04X}" for h in hist[-20:])
                )
            except Exception as he:
                print(f"    [verbose] cpuhistory failed: {he}")
        raise
    elapsed = time.monotonic() - start

    # Post-snapshot
    post_pattern = bm.mem_get(PATTERN_BASE, PATTERN_BASE + PATTERN_LEN - 1)
    post_screen = bm.mem_get(screen_strip[0], screen_strip[1])
    post_state = _snapshot_state(bm)
    regs = bm.registers_get()

    return HandlerResult(
        pre_pattern=pre_pattern,
        post_pattern=post_pattern,
        pre_screen_strip=pre_screen,
        post_screen_strip=post_screen,
        pre_state=pre_state,
        post_state=post_state,
        pc_at_stop=regs.get(REG_PC, 0),
        cycles_estimated=int(elapsed * 1_000_000),  # crude
    )


def diff_pattern(pre: bytes, post: bytes) -> list[tuple[int, int, int]]:
    """Return list of (offset, before, after) for each byte that changed."""
    return [(i, pre[i], post[i]) for i in range(len(pre)) if pre[i] != post[i]]


def diff_state(pre: dict[int, int], post: dict[int, int]) -> list[tuple[int, int, int]]:
    return [(addr, pre[addr], post[addr]) for addr in pre if pre[addr] != post[addr]]


def dump_keycode_lut(bm: BinMon) -> bytes:
    """Read the matrix-slot → keycode LUT at $0F90..$0FCF (64 entries).
    Note: only valid after defMON's boot scan has populated everything."""
    return bm.mem_get(0x0F90, 0x0FCF)


# ---- Alternate dispatch: hijack defMON's existing main loop ---------
#
# The trampoline approach assumes the seqED handler RTSs cleanly back to
# our trampoline. In practice many defMON handlers JMP to the main loop
# via $092C without RTSing — so we never reach our landing pad.
#
# This second strategy uses a stop-on-hit checkpoint at the *top* of the
# main editor loop, $092C (right before "JSR $0E47" — the keyboard scan).
# We let the CPU run until the loop-top checkpoint fires (defMON is now
# idle, about to scan keys), then we:
#   1. Patch the JSR $0E47 to NOPs so the scan doesn't overwrite $0E44.
#   2. Set $0E44/$0E41/$0E42, set A=keycode (so the post-scan CMP #$FF
#      doesn't BEQ to skip).
#   3. Set PC=$092F (just past the patched JSR) and resume.
#   4. CPU dispatches the key, eventually returns to $092C → loop top
#      checkpoint fires again.
#   5. Restore the JSR $0E47, capture pattern bytes.

LOOP_TOP = 0x092C  # main editor loop entry: JSR $0E47
LOOP_AFTER_SCAN = 0x092F  # immediately after JSR $0E47


def press_via_loop(
    bm: BinMon,
    mode: str,
    keycode: int,
    mod1: int = MOD_NONE,
    mod2: int = MOD_NONE,
    *,
    set_mode: bool = False,
    cursor: tuple[int, int] | None = None,
    arranger_row: int = 0,
    wait_timeout: float = 5.0,
    verbose: bool = False,
) -> HandlerResult:
    """Direct-call alternative that hijacks the existing main loop.

    Sequence (all under bm.halted() so the CPU stays halted between
    setup steps; the scanner does NOT get a chance to overwrite $0E44):
      1. run_until_pc(LOOP_TOP)  — CPU halts at $092C.
      2. (optional) inject cursor positioning if `cursor=(voice, step)`.
         For SID#2 voices 3-5, also temp-swap the SID#1 proxy arranger
         entry (see "SID#2 arranger proxy" below).
      3. patch $092C..$092E with NOPs (skip the keyboard scan).
      4. write $0E44 / $0E41 / $0E42 / $7167; set A=keycode.
      5. run_until_pc(LOOP_TOP)  — explicit resume + wait for the loop
                                   to return after dispatching our key.
      6. restore $092C bytes + proxy arranger byte (if swapped).

    Robust against handlers that JMP (rather than RTS) back to the
    loop, since we wait on the loop-top checkpoint either way.

    `cursor=(voice, step)` mem_sets the writer-dispatcher's cursor
    variables ($71CD voice-selector, $71D2 step, $71CE page, $71D1
    page-pair, $71CA write-count, $71CB stride, $71CC range-fill,
    $71C1 supercommand-flags, $716D digit-phase) so this single
    dispatched press writes to the requested (voice, step) cell. Voice
    is 0..5; the writer-dispatcher only handles SID#1 voices natively,
    so for voices 3-5 we apply the arranger proxy below.

    **SID#2 arranger proxy** (voice in {3, 4, 5}): the seqED writer at
    $844C looks up pat_num via the SID#1 arranger tables ($1B00/$1C00/
    $1D00), with no knowledge of $7171 (chip view). To target a SID#2
    cell, we (a) read the SID#2 arranger entry at
    ($6E00/$6F00/$7000)+arranger_row, (b) save and overwrite the
    corresponding SID#1 arranger byte with that pat_num, (c) set
    $71CD to the SID#1 proxy voice selector (V3→V0, V4→V1, V5→V2),
    (d) fire the chord — the writer derefs the patched SID#1 arranger
    and lands the cell in the SID#2 pattern, (e) restore the SID#1
    arranger byte. All inside the halted block so no IRQ sees the
    patched state."""

    if mode not in MODE_HANDLER:
        raise ValueError(f"unknown mode {mode!r}")

    # Pre-snapshot (with CPU resumed; values are pre-injection).
    pre_pattern = bm.mem_get(PATTERN_BASE, PATTERN_BASE + PATTERN_LEN - 1)
    pre_screen = bm.mem_get(0x05F0, 0x0640)
    pre_state = _snapshot_state(bm)

    # SID#2 arranger proxy bookkeeping (only used when voice in {3,4,5}).
    # See module-level SID1_ARR_BASES / SID2_ARR_BASES / _VOICE_SELECTOR_VALUES.
    proxy_restore: tuple[int, int] | None = None

    with bm.halted():
        # 1. Wait for CPU to reach the loop top so we can safely patch.
        bm.run_until_pc(LOOP_TOP, timeout=wait_timeout)

        # 2. (optional) Position the writer-dispatcher cursor BEFORE the
        # dispatch runs. CPU is halted here, so the screen-update IRQ
        # can't clobber $71CD between mem_set and dispatch.
        if cursor is not None:
            _voice, _step = cursor
            if _voice not in range(6):
                raise ValueError(f"cursor voice must be 0..5, got {_voice}")
            if not 0 <= _step <= 0x1F:
                raise ValueError(f"cursor step must be 0..31, got {_step}")
            if not 0 <= arranger_row <= 0x7F:
                raise ValueError(
                    f"arranger_row must be 0..0x7F, got " f"{arranger_row}"
                )

            # SID#2 voices proxy through the SID#1 voice selector with
            # the same index (V3→V0, V4→V1, V5→V2). Selector value goes
            # in $71CD; the writer's threshold check at $B27D maps
            # selector → SID#1 arranger base.
            proxy_voice = _voice if _voice < 3 else _voice - 3
            _voice_sel = _VOICE_SELECTOR_VALUES[proxy_voice]

            if _voice >= 3:
                sid2_arr_addr = SID2_ARR_BASES[_voice - 3] + arranger_row
                sid1_arr_addr = SID1_ARR_BASES[proxy_voice] + arranger_row
                pat_num = bm.mem_get(sid2_arr_addr, sid2_arr_addr)[0]
                saved_byte = bm.mem_get(sid1_arr_addr, sid1_arr_addr)[0]
                proxy_restore = (sid1_arr_addr, saved_byte)
                bm.mem_set(sid1_arr_addr, bytes([pat_num]))

            # Force back to seqED mode — defMON's post-write auto-advance
            # can wrap into seqLIST mode ($B352 JMP $0BBA with A=$02) if
            # the previous press left the cursor near a page boundary.
            bm.mem_set(ADDR_MODE, bytes([MODE_VAL["seqed"]]))
            bm.mem_set(0x71CD, bytes([_voice_sel]))  # voice selector
            bm.mem_set(0x71D2, bytes([_step]))  # step within page
            bm.mem_set(0x71CE, bytes([0]))  # page = 0
            bm.mem_set(0x71D1, bytes([0]))  # page-pair = 0
            bm.mem_set(0x71CA, bytes([1]))  # write count = 1
            bm.mem_set(0x71CB, bytes([1]))  # stride = 1
            bm.mem_set(0x71CC, bytes([0]))  # range-fill OFF
            bm.mem_set(0x71C1, bytes([0]))  # clear supercommand flags
            bm.mem_set(0x716D, bytes([0]))  # nibble phase = first

        # Snapshot the original 3 bytes of "JSR $0E47".
        original_scan_jsr = bm.mem_get(LOOP_TOP, LOOP_TOP + 2)

        # 3. Patch the scan JSR out for this single iteration.
        bm.mem_set(LOOP_TOP, bytes([0xEA, 0xEA, 0xEA]))

        # 3. Set the input state.
        if set_mode:
            bm.mem_set(ADDR_MODE, bytes([MODE_VAL[mode]]))
        bm.mem_set(ADDR_KEY, bytes([keycode & 0xFF]))
        bm.mem_set(ADDR_PREV_KEY, bytes([(keycode ^ 0xFF) & 0xFF]))
        bm.mem_set(ADDR_MOD1, bytes([mod1 & 0xFF]))
        bm.mem_set(ADDR_MOD2, bytes([mod2 & 0xFF]))
        bm.mem_set(ADDR_REPEAT_CTR, bytes([0x01]))
        # A must satisfy "CMP #$FF; BEQ $0967" at $092F → take the dispatch.
        # Skip the patched NOP-out region by jumping straight to $092F.
        bm.registers_set({REG_A: keycode & 0xFF, REG_PC: LOOP_AFTER_SCAN})

        if verbose:
            regs = bm.registers_get()
            print(
                f"    [verbose] pre-resume: PC=${regs[REG_PC]:04X}  "
                f"A=${regs[REG_A]:02X}  X=${regs[REG_X]:02X}  Y=${regs[REG_Y]:02X}  "
                f"$0E44=${bm.mem_get(0x0E44, 0x0E44)[0]:02X}  "
                f"$0E41=${bm.mem_get(0x0E41, 0x0E41)[0]:02X}"
            )

        # 4. Resume; wait for next loop-top hit (after dispatch returns).
        try:
            bm.run_until_pc(LOOP_TOP, timeout=wait_timeout)
        finally:
            # 5. Restore the scan JSR before resuming normal operation.
            bm.mem_set(LOOP_TOP, original_scan_jsr)
            # Restore the SID#1 arranger byte if we proxy-swapped it.
            if proxy_restore is not None:
                _restore_addr, _restore_byte = proxy_restore
                bm.mem_set(_restore_addr, bytes([_restore_byte]))

        # Post-snapshot while CPU still halted at LOOP_TOP.
        post_pattern = bm.mem_get(PATTERN_BASE, PATTERN_BASE + PATTERN_LEN - 1)
        post_screen = bm.mem_get(0x05F0, 0x0640)
        post_state = _snapshot_state(bm)
        regs = bm.registers_get()

    return HandlerResult(
        pre_pattern=pre_pattern,
        post_pattern=post_pattern,
        pre_screen_strip=pre_screen,
        post_screen_strip=post_screen,
        pre_state=pre_state,
        post_state=post_state,
        pc_at_stop=regs.get(REG_PC, 0),
        cycles_estimated=0,
    )


# ---- Multi-press SID#2 chord context (sidCALL, speed for V3-V5) -----
#
# `press_via_loop(cursor=(voice>=3, step), ...)` handles single-press
# chords on SID#2 voices by (a) running the SID#1→SID#2 arranger proxy
# swap and (b) re-seeding the cursor variables, all inside one halted
# block. For multi-press chords (sidCALL = 2 nibbles into one byte,
# speed = 2 nibbles + auto-advance) doing that per-press clobbers the
# writer's `$716D` digit-phase between presses, so the second nibble
# can't OR into the first byte.
#
# `Sid2Chord` holds the swap + cursor positioning once across N presses
# without re-positioning. CPU stays halted at $092C between presses.
#
# Voice must be in {3, 4, 5}. For V0/V1/V2 multi-press chords use
# `field_setter.write_byte_chord` directly — no proxy needed.

SID1_ARR_BASES = (0x1B00, 0x1C00, 0x1D00)
SID2_ARR_BASES = (0x6E00, 0x6F00, 0x7000)
_VOICE_SELECTOR_VALUES = (0x00, 0x09, 0x12)


class Sid2Chord:
    """Context manager that holds the SID#1→SID#2 arranger-proxy swap
    across multiple direct-call key presses, enabling multi-press
    chords (sidCALL, speed) on SID#2 voices 3-5.

    On entry: halts the CPU at $092C, reads the SID#2 arranger entry at
    ($6E00/$6F00/$7000) + arranger_row, saves the corresponding SID#1
    arranger byte, overwrites it with the SID#2 pat_num, and seeds the
    writer-dispatcher cursor variables ($71CD voice selector, $71D2
    step, $71CE/$71D1 page, $71CA/$71CB write loop, $71CC range-fill
    OFF, $71C1 supercommand flags, $716D nibble phase = first).

    Each `.press(keycode, mod1=, mod2=)` call patches the keyboard-scan
    JSR at $092C to NOPs, writes ($0E44, $0E41, $0E42), sets A=keycode
    + PC=$092F, resumes, and waits for $092C to re-fire. Between presses
    the writer's cursor + $716D state evolves naturally (defMON's
    writer auto-advances step and toggles digit phase), exactly as it
    does for SID#1 multi-press chords.

    On exit: restores the SID#1 arranger byte. Resuming the CPU is
    deferred to the outer `bm.halted()` cleanup.

    Usage:

        with kh.Sid2Chord(bm, voice=3, step=2, arranger_row=0) as ctx:
            ctx.press(hi_kc, mod1=kh.MOD_CBM)
            ctx.press(lo_kc, mod1=kh.MOD_CBM)
        # SID#1 arranger byte restored; CPU resumed when this returns.
    """

    def __init__(
        self,
        bm: BinMon,
        voice: int,
        step: int,
        *,
        arranger_row: int = 0,
        wait_timeout: float = 5.0,
    ):
        if voice not in (3, 4, 5):
            raise ValueError(
                f"Sid2Chord voice must be 3, 4, or 5 (use "
                f"write_byte_chord directly for SID#1 V0/V1/V2); "
                f"got {voice}"
            )
        if not 0 <= step <= 0x1F:
            raise ValueError(f"step must be 0..31, got {step}")
        if not 0 <= arranger_row <= 0x7F:
            raise ValueError(f"arranger_row must be 0..0x7F, got {arranger_row}")
        self.bm = bm
        self.voice = voice
        self.step = step
        self.arranger_row = arranger_row
        self.wait_timeout = wait_timeout
        self._halt_cm = None
        self._proxy_restore: tuple[int, int] | None = None

    def __enter__(self) -> "Sid2Chord":
        bm = self.bm
        proxy_voice = self.voice - 3
        sid2_arr_addr = SID2_ARR_BASES[proxy_voice] + self.arranger_row
        sid1_arr_addr = SID1_ARR_BASES[proxy_voice] + self.arranger_row
        voice_sel = _VOICE_SELECTOR_VALUES[proxy_voice]

        self._halt_cm = bm.halted()
        self._halt_cm.__enter__()
        try:
            bm.run_until_pc(LOOP_TOP, timeout=self.wait_timeout)
            # Arranger proxy swap.
            pat_num = bm.mem_get(sid2_arr_addr, sid2_arr_addr)[0]
            saved_byte = bm.mem_get(sid1_arr_addr, sid1_arr_addr)[0]
            self._proxy_restore = (sid1_arr_addr, saved_byte)
            bm.mem_set(sid1_arr_addr, bytes([pat_num]))
            # Mode + cursor seed (matches press_via_loop cursor block).
            bm.mem_set(ADDR_MODE, bytes([MODE_VAL["seqed"]]))
            bm.mem_set(0x71CD, bytes([voice_sel]))
            bm.mem_set(0x71D2, bytes([self.step]))
            bm.mem_set(0x71CE, bytes([0]))
            bm.mem_set(0x71D1, bytes([0]))
            bm.mem_set(0x71CA, bytes([1]))
            bm.mem_set(0x71CB, bytes([1]))
            bm.mem_set(0x71CC, bytes([0]))
            bm.mem_set(0x71C1, bytes([0]))
            bm.mem_set(0x716D, bytes([0]))
        except Exception:
            self._halt_cm.__exit__(None, None, None)
            self._halt_cm = None
            raise
        return self

    def press(self, keycode: int, mod1: int = MOD_NONE, mod2: int = MOD_NONE) -> None:
        """Fire one chord press at the SID#2 cell. The arranger proxy
        and the initial cursor seed installed in __enter__ remain in
        effect; the writer's own $716D / step auto-advance carries
        across presses (same as SID#1 multi-press)."""
        if self._halt_cm is None:
            raise RuntimeError("Sid2Chord.press() called outside `with` block")
        bm = self.bm
        original_scan_jsr = bm.mem_get(LOOP_TOP, LOOP_TOP + 2)
        bm.mem_set(LOOP_TOP, bytes([0xEA, 0xEA, 0xEA]))
        try:
            bm.mem_set(ADDR_KEY, bytes([keycode & 0xFF]))
            bm.mem_set(ADDR_PREV_KEY, bytes([(keycode ^ 0xFF) & 0xFF]))
            bm.mem_set(ADDR_MOD1, bytes([mod1 & 0xFF]))
            bm.mem_set(ADDR_MOD2, bytes([mod2 & 0xFF]))
            bm.mem_set(ADDR_REPEAT_CTR, bytes([0x01]))
            bm.registers_set({REG_A: keycode & 0xFF, REG_PC: LOOP_AFTER_SCAN})
            bm.run_until_pc(LOOP_TOP, timeout=self.wait_timeout)
        finally:
            bm.mem_set(LOOP_TOP, original_scan_jsr)

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        try:
            if self._proxy_restore is not None:
                addr, byte = self._proxy_restore
                self.bm.mem_set(addr, bytes([byte]))
                self._proxy_restore = None
        finally:
            if self._halt_cm is not None:
                self._halt_cm.__exit__(exc_type, exc_val, exc_tb)
                self._halt_cm = None
        return False


def capture_keycode_via_checkpoint(
    bm: BinMon,
    key_name: str,
    *,
    d=None,  # type: ignore[name-defined]
    timeout: float = 2.0,
) -> int | None:
    """Issue a real key tap and capture the value defMON's scanner wrote
    to $0E44 immediately after $0EFA stores it. Uses a stop-on-hit
    checkpoint at $0EFA (the path where a single key was decoded).

    `d` must be a Defmon instance for the tap. Returns the captured byte
    or None if the checkpoint never fired within the timeout.
    """
    from .defmon import (
        Defmon,
    )  # noqa: F401  (kept for type hint; import side-effect free)

    if d is None:
        raise ValueError("Defmon instance required")
    cp = bm.checkpoint_set(
        0x0EFA, op=0x04, stop_when_hit=True, enabled=True, temporary=True
    )
    try:
        # Tap (release will time out — but the press will fire the
        # checkpoint as soon as the scanner runs).
        from .binmon import TAP_MODE_FIXED
        from .keys import lookup

        rc = lookup(key_name)
        bm.keymatrix_tap([rc], mode=TAP_MODE_FIXED, frames=8)
        # Wait for the checkpoint to fire by polling registers — when CPU
        # is halted, registers_get returns immediately.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                regs = bm.registers_get()
                # If CPU is at $0EFA, A holds the captured keycode.
                if regs.get(REG_PC, 0) == 0x0EFA:
                    return regs.get(REG_A, 0) & 0xFF
            except Exception:
                pass
            time.sleep(0.05)
        return None
    finally:
        try:
            bm.checkpoint_delete(cp.checknum)
        except Exception:
            pass
