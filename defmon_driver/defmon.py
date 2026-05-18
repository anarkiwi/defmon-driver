"""High-level defMON command bindings.

Every documented defMON shortcut from the wiki is bound here as a method
on the Defmon class. The method either:

  * sends a chord via BinMon.keymatrix_tap() (observation-based release —
    defMON polls $DC00/$DC01 every frame, so the tap returns as soon as
    defMON has actually sampled the bit), or

  * for actions that take many frames (load, save, pack), drives the same
    chord and then polls SCREEN_GET until a known text marker appears or
    a timeout is reached.

The reference for which keys map to which command is the wiki Field
Guide ('defMONing 101'), the seqED keys image, and the dedicated
super-commands and stereo-SID pages. See README of this harness module
for citations.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .binmon import (
    RELEASE_OBSERVED,
    TAP_MODE_FIXED,
    BinMon,
    BinmonError,
)
from .keys import chord_to_keys, lookup, text_to_chords
from .screen import ScreenSnapshot, parse_screen_response

log = logging.getLogger(__name__)


class DefmonError(RuntimeError):
    pass


@dataclass
class TapOutcome:
    """Lightweight result returned by tap()."""

    keys: tuple[str, ...]
    release_reason: int
    cia1_reads_total: int
    cia1_reads_sampling: int


class Defmon:
    """Drive a running defMON instance via the binary monitor.

    All methods that affect screen state poll SCREEN_GET on demand —
    there is no asynchronous worker, no internal state cache. The harness
    is deliberately thin so a higher-level test/demo can interleave its
    own observation logic.
    """

    # Tunables. Adjust if running without -warp.
    #
    # defMON debounces — its event-handler only fires after the matrix bit
    # has been seen across multiple consecutive scans. asid-vice's default
    # observation-based release returns after a SINGLE scan, so chords are
    # observed at the matrix layer but never reach defMON's command logic.
    # We therefore default to fixed-mode hold of 12 emulated frames —
    # well above any reasonable debounce window, but cheap in -warp.
    DEFAULT_TAP_FRAMES = 12
    DEFAULT_TAP_MODE = TAP_MODE_FIXED
    DEFAULT_SETTLE_S = 0.05
    LOAD_TIMEOUT_S = 30.0
    SAVE_TIMEOUT_S = 30.0

    def __init__(self, binmon: BinMon):
        self.bm = binmon

    # ---------------------------------------------------------------- core

    def tap(
        self,
        *names: str,
        frames: int = DEFAULT_TAP_FRAMES,
        mode: int = DEFAULT_TAP_MODE,
        settle: float = DEFAULT_SETTLE_S,
        require_observed: bool = False,
        wait_release: bool = True,
        wait_timeout: float = 1.5,
    ) -> TapOutcome:
        """Tap a chord, optionally polling until defMON observes/times out.

        names - symbolic key names (case-insensitive). e.g. tap("LSHIFT", "X").
        frames - max hold time. observed mode returns earlier.
        mode - TAP_MODE_OBSERVED (default) or TAP_MODE_FIXED.
        settle - real-time sleep after release; bridges any frame between
                 defMON polling and us issuing the next tap.
        require_observed - raise if release_reason isn't 'observed'.
        wait_release - poll keymatrix_get until the tap has completed
                 (release_reason != NONE). Required for accurate cia1
                 stats — without it the get races the tap submission.
        wait_timeout - cap on the polling loop. Even at the 60-frame
                 default, in warp mode this never approaches the cap.
        """
        rc = chord_to_keys(*names)
        self.bm.keymatrix_tap(rc, mode=mode, frames=frames)

        out = self.bm.keymatrix_get()
        if wait_release:
            deadline = time.monotonic() + wait_timeout
            while out.release_reason == 0 and time.monotonic() < deadline:
                time.sleep(0.02)
                out = self.bm.keymatrix_get()

        if settle > 0:
            time.sleep(settle)
        if require_observed and out.release_reason != RELEASE_OBSERVED:
            raise DefmonError(
                f"chord {names} did not observe a CIA1 read "
                f"(reason={out.release_reason}, total={out.cia1_reads_total})"
            )
        return TapOutcome(
            keys=tuple(names),
            release_reason=out.release_reason,
            cia1_reads_total=out.cia1_reads_total,
            cia1_reads_sampling=out.cia1_reads_sampling,
        )

    def hold(self, *names: str, frames: int) -> None:
        """Fixed-duration chord press (no observation early-release)."""
        self.tap(*names, frames=frames, mode=TAP_MODE_FIXED)

    def held_modifier_press(
        self,
        modifiers: tuple[str, ...] | list[str],
        key: str,
        pre_modifier_settle: float = 0.10,
        hold_seconds: float = 0.20,
        post_release_settle: float = 0.10,
    ) -> None:
        """Press `modifiers` (sticky), wait, press `key` (additive),
        hold, release `key`, release `modifiers`.

        Uses ONLY ``keymatrix_set`` so the sticky bits are guaranteed to
        persist across the digit press. ``keymatrix_tap`` is avoided
        because it appears to overwrite the matrix during its hold
        window, releasing previously-sticky bits prematurely.

        This matches the temporal pattern a human typist produces:
        modifier(s) down → digit down → digit up → modifier(s) up. Use
        for chords that defMON treats as a "modifier-prefix then data
        keys" sequence (e.g. CBM-hold-for-sidCALL1 edits).
        """
        mod_rc = [lookup(m) for m in modifiers]
        key_rc = lookup(key)
        self.bm.keymatrix_set([(r, c, 1) for r, c in mod_rc])
        if pre_modifier_settle > 0:
            time.sleep(pre_modifier_settle)
        self.bm.keymatrix_set([(key_rc[0], key_rc[1], 1)])
        if hold_seconds > 0:
            time.sleep(hold_seconds)
        self.bm.keymatrix_set([(key_rc[0], key_rc[1], 0)])
        # Settle to let defMON's keyscan observe the digit-released
        # state with modifier still held — defMON's chord handler may
        # need this to commit the edit.
        if post_release_settle > 0:
            time.sleep(post_release_settle)
        self.bm.keymatrix_set([(r, c, 0) for r, c in mod_rc])

    def held_modifier_sequence(
        self,
        modifiers: tuple[str, ...] | list[str],
        keys: tuple[str, ...] | list[str],
        per_key_hold: float = 0.18,
        per_key_release_settle: float = 0.06,
    ) -> None:
        """Hold `modifiers` continuously while tapping each key in
        sequence. Modifiers released only after the last key.

        Use for multi-digit edits like CBM-held-for-sidCALL1 where
        defMON expects 2 hex digits with the modifier held throughout.
        """
        mod_rc = [lookup(m) for m in modifiers]
        self.bm.keymatrix_set([(r, c, 1) for r, c in mod_rc])
        time.sleep(0.10)
        for key in keys:
            key_rc = lookup(key)
            self.bm.keymatrix_set([(key_rc[0], key_rc[1], 1)])
            time.sleep(per_key_hold)
            self.bm.keymatrix_set([(key_rc[0], key_rc[1], 0)])
            time.sleep(per_key_release_settle)
        self.bm.keymatrix_set([(r, c, 0) for r, c in mod_rc])
        time.sleep(0.10)

    def hold_then_tap(
        self,
        modifiers: tuple[str, ...] | list[str],
        key: str,
        frames: int = DEFAULT_TAP_FRAMES,
        settle: float = DEFAULT_SETTLE_S,
    ) -> None:
        """Press `modifiers` first (sticky), then tap `key` while modifiers
        remain held, then release modifiers.

        defMON's keyscan distinguishes "all keys pressed simultaneously"
        from "modifier-first-then-digit" for some chord families (e.g.
        sidCALL edits via c=+SHIFT, speed via CTRL+c=). The simultaneous
        ``tap("LSHIFT","CBM","5")`` doesn't trigger those handlers; this
        helper replicates the proper temporal pattern via two
        ``keymatrix_set`` calls (sticky-on, sticky-off) around a single
        ``keymatrix_tap`` of the data key.
        """
        mod_rc = [(r, c) for r, c in (lookup(m) for m in modifiers)]
        # 1. Set modifiers sticky-on.
        self.bm.keymatrix_set([(r, c, 1) for r, c in mod_rc])
        # 2. Tap the data key while modifiers held.
        self.bm.keymatrix_tap(
            [lookup(key)],
            mode=TAP_MODE_FIXED,
            frames=frames,
        )
        # 3. Poll until tap completes.
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            out = self.bm.keymatrix_get()
            if out.release_reason != 0:
                break
            time.sleep(0.02)
        # 4. Release modifiers.
        self.bm.keymatrix_set([(r, c, 0) for r, c in mod_rc])
        if settle > 0:
            time.sleep(settle)

    def type_text(self, text: str, per_char_settle: float = 0.04) -> None:
        """Type printable ASCII via the keyboard matrix (defMON disk-menu prompt)."""
        for chord in text_to_chords(text):
            self.tap(*chord, settle=per_char_settle)

    # ---------------------------------------------------------- screen IO

    def screen(self) -> ScreenSnapshot:
        return parse_screen_response(self.bm.screen_get())

    def wait_for_screen_text(
        self,
        needle: str,
        timeout: float = 10.0,
        poll: float = 0.1,
        absent: bool = False,
    ) -> ScreenSnapshot:
        """Poll SCREEN_GET until needle is present (or, with absent=True, until
        it is gone). Returns the snapshot in which the predicate became true."""
        deadline = time.monotonic() + timeout
        last: ScreenSnapshot | None = None
        while True:
            last = self.screen()
            ok = (not last.contains(needle)) if absent else last.contains(needle)
            if ok:
                return last
            if time.monotonic() >= deadline:
                raise DefmonError(
                    f"timeout waiting for text {needle!r} ({'absent' if absent else 'present'})"
                )
            time.sleep(poll)

    def wait_for_screen_change(
        self,
        baseline: ScreenSnapshot,
        timeout: float = 3.0,
        poll: float = 0.05,
    ) -> ScreenSnapshot:
        """Poll until SCREEN_GET differs from baseline.screen, or return the
        latest snapshot at timeout (no exception — the caller decides)."""
        deadline = time.monotonic() + timeout
        snap = baseline
        while time.monotonic() < deadline:
            snap = self.screen()
            if snap.screen != baseline.screen:
                return snap
            time.sleep(poll)
        return snap

    def wait_for_screen_stable(
        self, stable_for: float = 0.4, timeout: float = 10.0, poll: float = 0.1
    ) -> ScreenSnapshot:
        """Wait until two consecutive snapshots compare equal for `stable_for`
        seconds. Useful right after autostart to know the boot has finished."""
        deadline = time.monotonic() + timeout
        last_screen: bytes = b""
        stable_since: float | None = None
        snap: ScreenSnapshot | None = None
        while time.monotonic() < deadline:
            snap = self.screen()
            if snap.screen == last_screen:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= stable_for:
                    return snap
            else:
                last_screen = snap.screen
                stable_since = None
            time.sleep(poll)
        if snap is None:
            raise DefmonError("screen never returned a snapshot")
        return snap

    # -------------------------------------------------------- defMON boot

    def wait_for_defmon_loaded(self, timeout: float = 60.0) -> ScreenSnapshot:
        """Wait until defMON's main screen is up.

        defMON's info display shows its version string ('DEFMON' literal),
        which is absent from the BASIC boot screen. We poll for that as
        the primary signal, then fall back to a stability check for
        custom-charset builds where 'DEFMON' may be in glyphs we cannot
        decode."""
        deadline = time.monotonic() + timeout
        try:
            return self.wait_for_screen_text("DEFMON", timeout=timeout * 0.6, poll=0.2)
        except DefmonError:
            pass
        remaining = max(2.0, deadline - time.monotonic())
        return self.wait_for_screen_stable(stable_for=1.0, timeout=remaining)

    # =========================================================== screens

    def toggle_seqed_seqlist(self) -> TapOutcome:
        """RUNSTOP toggles between seqED (pattern view) and seqLIST (arranger)."""
        return self.tap("RUNSTOP")

    def jump_arranger_position(self) -> TapOutcome:
        """C= + RUNSTOP — jump to a specific arranger position."""
        return self.tap("CBM", "RUNSTOP")

    def enter_sidtab(self) -> TapOutcome:
        """LEFTARROW (top-left of C64 keyboard) — open the sidTAB."""
        return self.tap("LEFTARROW")

    def jump_sidtab_position(self) -> TapOutcome:
        """SHIFT + LEFTARROW — jump to a specific sidTAB position."""
        return self.tap("LSHIFT", "LEFTARROW")

    def jump_sidtab_position_alt(self) -> TapOutcome:
        """C= + LEFTARROW — alternate jump-to-sidTAB."""
        return self.tap("CBM", "LEFTARROW")

    def switch_sid_chip(self) -> TapOutcome:
        """CTRL + LEFTARROW — switch between SID #1 and SID #2 seqLISTs."""
        return self.tap("CTRL", "LEFTARROW")

    def toggle_stereo(self) -> TapOutcome:
        """CTRL + SHIFT + LEFTARROW — enable/disable stereo SID mode."""
        return self.tap("CTRL", "LSHIFT", "LEFTARROW")

    def cycle_sid_high_byte(self) -> TapOutcome:
        """CTRL + SHIFT + UPARROW — cycle high byte ($D4xx → $D5xx → $DExx → $DFxx)."""
        return self.tap("CTRL", "LSHIFT", "UPARROW")

    def adjust_sid_low_byte(self) -> TapOutcome:
        """CTRL + C= + UPARROW — bump low byte by $20 (SID #2 base address)."""
        return self.tap("CTRL", "CBM", "UPARROW")

    # ---- stereo state ----------------------------------------------------
    # Exactly one byte flips per stereo-related chord:
    #
    #   $715D  stereo enable flag        0 = mono, 1 = stereo (toggle_stereo)
    #   $7171  current chip view         0 = SID#1, 1 = SID#2 (switch_sid_chip)
    #   $7165  SID#2 base high byte      cycles $D4 → $D5 → $DE → $DF
    #   $7164  SID#2 base low byte       bumps by $20 per chord
    #
    # All four live in defMON's $7000-$73FF state region.

    ADDR_STEREO_FLAG = 0x715D
    ADDR_CHIP_SELECT = 0x7171
    ADDR_SID2_HIGH = 0x7165
    ADDR_SID2_LOW = 0x7164
    SID2_HIGH_BYTES = (0xD4, 0xD5, 0xDE, 0xDF)  # the 4 wiki-documented slots

    # Mode byte — the main-loop dispatch at $0939 is a CMP $7167 cascade,
    # so this byte authoritatively names the currently-active handler.
    # $7168 shadows the previous mode (used by LEFTARROW return-to-prev).
    ADDR_MODE = 0x7167
    ADDR_PREV_MODE = 0x7168
    MODE_SEQED = 0x01
    MODE_SEQLIST = 0x02
    MODE_SIDTAB = 0x04
    MODE_DISK = 0x20
    MODE_NAME = {
        MODE_SEQED: "seqed",
        MODE_SEQLIST: "seqlist",
        MODE_SIDTAB: "sidtab",
        MODE_DISK: "disk",
    }

    def current_mode(self) -> int:
        """Return the raw $7167 mode byte. Compare against MODE_SEQED /
        MODE_SEQLIST / MODE_SIDTAB / MODE_DISK; values outside that set
        indicate either a transitional state or memory corruption."""
        return self.bm.mem_get(self.ADDR_MODE, self.ADDR_MODE)[0]

    def current_mode_name(self) -> str:
        """Human-readable mode name, or 'unknown:$XX' for unmapped bytes."""
        m = self.current_mode()
        return self.MODE_NAME.get(m, f"unknown:${m:02X}")

    def is_stereo_enabled(self) -> bool:
        """Read the stereo flag at $715D directly. 1 = stereo on."""
        return self.bm.mem_get(self.ADDR_STEREO_FLAG, self.ADDR_STEREO_FLAG)[0] == 1

    def current_sid_chip(self) -> int:
        """Return 1 (SID#1) or 2 (SID#2) based on $7171."""
        v = self.bm.mem_get(self.ADDR_CHIP_SELECT, self.ADDR_CHIP_SELECT)[0]
        return 2 if v == 1 else 1

    def current_sid2_base_address(self) -> int:
        """Read defMON's currently-configured SID#2 base address from
        $7165 (high) / $7164 (low)."""
        lo = self.bm.mem_get(self.ADDR_SID2_LOW, self.ADDR_SID2_LOW)[0]
        hi = self.bm.mem_get(self.ADDR_SID2_HIGH, self.ADDR_SID2_HIGH)[0]
        return (hi << 8) | lo

    # Time budget for defMON to update its state vars after a chord tap:
    # 12-frame fixed-tap debounce (~240ms at PAL) + one main-loop iteration
    # for the handler to fire. 0.6s gives ~3x margin; empirically the
    # state-update verifier polls every 0.1s for up to ``STEREO_VERIFY_TIMEOUT``.
    STEREO_VERIFY_TIMEOUT = 1.5

    def _stereo_quiesce(self) -> None:
        """Clear any held super-command flag and stop playback so the
        next multi-modifier chord lands on a quiescent state. Without
        this, back-to-back CTRL+* chords intermittently fail to register
        (the previous chord's modifier latches confuse the dispatch)."""
        try:
            self.tap("CTRL", "RETURN")
        except Exception:  # noqa: BLE001
            pass
        try:
            self.tap("F7")
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.1)

    def _poll_byte_until(self, addr: int, want: int, timeout: float) -> bool:
        """Poll a single byte address until it equals ``want`` or ``timeout``
        elapses. Returns True if seen."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.bm.mem_get(addr, addr)[0] == want:
                return True
            time.sleep(0.1)
        return False

    STEREO_CHORD_RETRIES = 3

    def _retry_chord_until(self, addr: int, want: int, tap_fn, chord_name: str) -> None:
        """Quiesce → tap → poll. Retry up to STEREO_CHORD_RETRIES times if
        the byte at ``addr`` doesn't reach ``want`` — defMON's chord
        handler intermittently misses a tap when consecutive multi-mod
        chords arrive too close together, even with quiesce."""
        for _attempt in range(1, self.STEREO_CHORD_RETRIES + 1):
            self._stereo_quiesce()
            tap_fn()
            if self._poll_byte_until(addr, want, self.STEREO_VERIFY_TIMEOUT):
                return
        cur = self.bm.mem_get(addr, addr)[0]
        raise DefmonError(
            f"{chord_name}: byte at 0x{addr:04X} did not reach 0x{want:02X} "
            f"after {self.STEREO_CHORD_RETRIES} attempts; still 0x{cur:02X}"
        )

    def ensure_stereo(self, enabled: bool = True) -> None:
        """Idempotent toggle_stereo: if the state already matches `enabled`,
        do nothing; otherwise quiesce + tap the toggle and poll $715D.
        Retries up to STEREO_CHORD_RETRIES times if the chord doesn't land."""
        if self.is_stereo_enabled() == enabled:
            return
        want = 1 if enabled else 0
        self._retry_chord_until(
            self.ADDR_STEREO_FLAG, want, self.toggle_stereo, f"ensure_stereo({enabled})"
        )

    def select_sid_chip(self, chip: int) -> None:
        """Idempotent chip-view selection. chip must be 1 or 2."""
        if chip not in (1, 2):
            raise DefmonError(
                f"select_sid_chip(chip): chip must be 1 or 2; got {chip!r}"
            )
        if self.current_sid_chip() == chip:
            return
        want = 1 if chip == 2 else 0
        self._retry_chord_until(
            self.ADDR_CHIP_SELECT,
            want,
            self.switch_sid_chip,
            f"select_sid_chip({chip})",
        )

    def set_sid2_base_address(self, target: int) -> None:
        """Cycle SID#2 base address to `target` (e.g. 0xD420). High byte
        must be one of $D4/$D5/$DE/$DF; low byte must be a multiple of
        $20 in $00-$E0. Stereo must already be enabled (the chords only
        take effect with stereo on); call ensure_stereo(True) first."""
        if not self.is_stereo_enabled():
            raise DefmonError(
                "set_sid2_base_address: stereo is OFF; "
                "call ensure_stereo(True) first"
            )
        hi = (target >> 8) & 0xFF
        lo = target & 0xFF
        if hi not in self.SID2_HIGH_BYTES:
            raise DefmonError(
                f"set_sid2_base_address: high byte must be one of "
                f"{[f'0x{b:02X}' for b in self.SID2_HIGH_BYTES]}; got 0x{hi:02X}"
            )
        if lo % 0x20 != 0:
            raise DefmonError(
                f"set_sid2_base_address: low byte must be a multiple of "
                f"$20; got 0x{lo:02X}"
            )
        # Cycle the high byte. The chord rotates through SID2_HIGH_BYTES
        # in order — at most 3 taps to reach any slot from any other.
        # Each iteration runs a brief quiesce (CTRL+RETURN / F7) before the
        # tap so the previous chord's modifier release is fully decoded.
        self._cycle_until(
            self.ADDR_SID2_HIGH,
            hi,
            self.cycle_sid_high_byte,
            max_taps=len(self.SID2_HIGH_BYTES),
            what=f"high byte 0x{hi:02X}",
        )
        # Bump the low byte by $20 per chord. defMON wraps the low byte
        # back to $00 at $E0 + $20 = $100 (so 8 taps cover the full range).
        self._cycle_until(
            self.ADDR_SID2_LOW,
            lo,
            self.adjust_sid_low_byte,
            max_taps=8,
            what=f"low byte 0x{lo:02X}",
        )

    def _cycle_until(
        self, addr: int, want: int, tap_fn, max_taps: int, what: str
    ) -> None:
        """Tap ``tap_fn`` repeatedly until the byte at ``addr`` equals
        ``want``. Quiesces (CTRL+RETURN + F7) BEFORE each tap and retries
        if a tap fails to advance the byte off its prior value —
        defMON's chord handler intermittently drops consecutive
        multi-mod chord taps."""
        attempts_per_step = self.STEREO_CHORD_RETRIES
        for _ in range(max_taps):
            cur = self.bm.mem_get(addr, addr)[0]
            if cur == want:
                return
            advanced = False
            for _attempt in range(attempts_per_step):
                self._stereo_quiesce()
                tap_fn()
                deadline = time.monotonic() + self.STEREO_VERIFY_TIMEOUT
                while time.monotonic() < deadline:
                    if self.bm.mem_get(addr, addr)[0] != cur:
                        advanced = True
                        break
                    time.sleep(0.1)
                if advanced:
                    break
            if not advanced:
                raise DefmonError(
                    f"set_sid2_base_address: tap failed to advance "
                    f"0x{addr:04X} off 0x{cur:02X} after "
                    f"{attempts_per_step} retries (target {what})"
                )
        raise DefmonError(
            f"set_sid2_base_address: could not reach {what} after {max_taps} taps"
        )

    # ============================================================ disk IO

    def open_disk_menu(
        self, max_attempts: int = 3, per_attempt_timeout: float = 1.5
    ) -> ScreenSnapshot:
        """SHIFT + X. Returns the disk menu screen snapshot.

        Verifies via screen-diff: ``VOC0``/``VOC1``/``VOC2`` markers
        absent (we left seqED) AND a disk-menu cursor glyph ``>`` is
        present (we're in the directory listing).

        Important — *the disk menu does NOT change* ``$7167``. The
        visible menu is a nested input loop at ``$75DB`` invoked
        synchronously from ``$8244`` via ``$7423``; the main-loop
        dispatcher at ``$0939`` is suspended for the menu's lifetime
        and ``$7167`` stays at ``$01`` (seqED) throughout. The mode-
        byte ``MODE_DISK`` ($20) is a *separate* feature reachable
        only by ``CTRL+/`` from sidTAB (``$BD5D`` writer site), unused
        by this driver. So screen-diff is the right detector here even
        though ``current_mode()`` is authoritative for every other
        modal check.

        Stops playback (F7) before each attempt — the player IRQ can
        cause the LSHIFT+X chord handler to be missed when defMON is
        actively processing audio. Retries up to ``max_attempts``
        before raising DefmonError."""
        last_text: str | None = None
        for _attempt in range(max_attempts):
            try:
                self.tap("F7")  # stop playback if running
            except Exception:
                pass
            time.sleep(0.05)
            self.tap("LSHIFT", "X")
            deadline = time.monotonic() + per_attempt_timeout
            while time.monotonic() < deadline:
                snap = self.screen()
                text = snap.text()
                last_text = text
                if any(m in text for m in ("VOC0", "VOC1", "VOC2")):
                    time.sleep(0.1)
                    continue
                if Defmon._find_dir_cursor_row(snap) is not None:
                    return snap
                time.sleep(0.1)
        head = (last_text or "")[:200].replace("\n", " | ")
        raise DefmonError(
            f"open_disk_menu: after {max_attempts} attempt(s), screen "
            f"still doesn't look like the disk menu (seqED markers "
            f"present or no '>' cursor visible). screen head: {head!r}"
        )

    def ensure_seqed(self, max_steps: int = 6, settle: float = 0.18) -> None:
        """Drive defMON back to seqED home from any mode.

        Long action sweeps tend to leave the cursor in seqLIST, sidTAB,
        super-mode, or some combination, and ``open_disk_menu`` alone
        cannot recover — its LSHIFT+X chord is only honoured from a
        seqED-like state. This helper drains those states by alternating
        always-safe quieting (F7, CTRL+RETURN) with the two mode-exit
        chords (RUNSTOP for seqLIST, LEFTARROW for sidTAB / disk menu)
        and polling ``$7167`` (the mode byte the main-loop dispatch at
        $0939 reads) for ``MODE_SEQED`` ($01) between each step.

        Convergence:

          - seqED start: returns on the first liveness check.
          - seqLIST start: RUNSTOP returns to seqED; second check passes.
          - sidTAB start: LEFTARROW returns to seqED; check passes.
          - disk-menu start: LEFTARROW closes menu; check passes.
          - super-mode: CTRL+RETURN clears; check passes.
          - sidTAB-inside-super or seqLIST-inside-super: clears super,
            then mode-exit.

        Raises ``DefmonError`` if ``$7167 != MODE_SEQED`` after
        ``max_steps`` recovery iterations."""
        # Always-safe pre-pass: silence the player, exit any super-mode.
        # CTRL+RETURN is `super_exit` and is a no-op outside super-mode.
        for chord in (("F7",), ("CTRL", "RETURN")):
            try:
                self.tap(*chord)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(settle)
        if self.current_mode() == self.MODE_SEQED:
            return

        # Alternate mode-exit chords. LEFTARROW first because it covers
        # two of the three non-seqED modes (sidTAB and disk menu), and
        # the third (seqLIST) is comparatively rare in the action-sweep
        # tail. Worst case (seqED→sidTAB→seqED→…→sidTAB) converges in
        # 2 steps; we budget max_steps=6 for headroom.
        for step in range(max_steps):
            chord = ("LEFTARROW",) if step % 2 == 0 else ("RUNSTOP",)
            try:
                self.tap(*chord)
            except Exception:  # noqa: BLE001
                pass
            time.sleep(settle)
            if self.current_mode() == self.MODE_SEQED:
                return

        # Fallback: force the mode byte directly. The alternating
        # chord approach can oscillate (seqLIST → seqED → sidTAB → …)
        # without converging on seqED in `max_steps` for some
        # transitions. The dispatcher at $0939 reads $7167 fresh on
        # every main-loop iteration, so a bare mem_set is sufficient
        # — defMON picks it up on the next cycle. None of the three
        # writers of $7167 ($0BC5, $BD6C, $C4A5) run during normal
        # seqED operation, so the write sticks.
        self.bm.mem_set(self.ADDR_MODE, bytes([self.MODE_SEQED]))
        time.sleep(settle)
        if self.current_mode() == self.MODE_SEQED:
            return

        raise DefmonError(
            f"ensure_seqed: $7167 = ${self.current_mode():02X} "
            f"({self.current_mode_name()}) after {max_steps} recovery "
            f"steps + mem_set fallback; want ${self.MODE_SEQED:02X} "
            f"(seqed)."
        )

    def close_disk_menu(self) -> ScreenSnapshot:
        """LEFTARROW exits back to seqED/seqLIST."""
        before = self.screen()
        self.tap("LEFTARROW")
        return self.wait_for_screen_change(before, timeout=3.0)

    def disk_read_directory(self) -> ScreenSnapshot:
        """Bare SPACE inside the disk menu — refresh the directory
        listing. Dispatched by ``$75DB``'s internal scan loop at
        ``$7643``: ``CMP #$20; JMP $75DB`` re-enters the paint phase
        so a freshly-saved file shows up. (Earlier analysis incorrectly
        targeted ``$C491``'s ``$729A`` LUT, which marks $20 as ignored;
        but ``$C491`` is a different mode — the visible disk menu is
        the nested ``$75DB`` UI loop, which DOES respond to bare SPACE.)"""
        self.tap("SPACE")
        time.sleep(0.5)
        return self.screen()

    def disk_prev_drive(self) -> TapOutcome:
        """Bare COMMA inside the disk menu — previous drive (e.g. 9 → 8).
        ``$75DB`` dispatch at ``$7629``: ``CMP #$2C; DEC $BA; AND #$0B;
        ORA #$08; STA $BA; JMP $75DB`` (wraps within drives 8-11)."""
        out = self.tap("COMMA")
        time.sleep(0.2)
        return out

    def disk_next_drive(self) -> TapOutcome:
        """Bare PERIOD inside the disk menu — next drive (e.g. 8 → 9).
        ``$75DB`` dispatch at ``$763A``: ``CMP #$2E; INC $BA`` then same
        wrap as prev_drive."""
        out = self.tap("PERIOD")
        time.sleep(0.2)
        return out

    def disk_current_drive(self) -> int | None:
        """Read the drive number defMON is showing in the disk menu
        footer. Returns None if not parseable."""
        snap = self.screen()
        last = snap.lines()[-1].strip()
        if last and last[0].isdigit():
            try:
                return int(last[0])
            except ValueError:
                return None
        for row in reversed(snap.lines()):
            stripped = row.strip()
            if len(stripped) >= 1 and stripped[0].isdigit():
                return int(stripped[0])
        return None

    def disk_select_drive(self, drive: int, max_steps: int = 5) -> int:
        """Cycle drives until ``disk_current_drive() == drive``. Returns
        the final drive value. Raises if the drive can't be reached
        within ``max_steps`` next-drive taps, or if the menu footer
        can't be parsed at all."""
        for _ in range(max_steps):
            cur = self.disk_current_drive()
            if cur == drive:
                return drive
            self.disk_next_drive()
            self.disk_read_directory()
        cur = self.disk_current_drive()
        if cur != drive:
            raise DefmonError(f"could not select drive {drive}; ended on {cur}")
        return drive

    def disk_save_new(
        self,
        filename: str,
        timeout: float = SAVE_TIMEOUT_S,
        max_dir_rows: int = 30,
        flush_drive_path: Optional[str] = None,
        flush_unit: int = 8,
        flush_drive: int = 0,
    ) -> ScreenSnapshot:
        """Save current tune to a new directory slot. Implements the full
        documented sequence:

          1. Read the directory (SPACE) so the listing is on screen with
             the '>' cursor at the top-left.
          2. Navigate the cursor DOWN past every populated entry until
             we're on a blank slot (the row beyond the last filename).
          3. Press S — defMON shows a name prompt: a '.' inside quotes
             with an up-arrow cursor beneath the empty character slot.
          4. Type the filename (max 15 chars — defMON prepends '.' itself,
             so the on-disk name fits the 16-byte CBM DOS slot).
          5. Press RETURN. Disk activity scrolls characters along the
             bottom row; when finished, defMON returns to the seqED screen.

        We use the disappearance of the disk menu (i.e. seqED reappearing)
        as the completion signal. defMON only re-paints the seqED bar once
        the 1541 has finished writing the file.

        If `flush_drive_path` is supplied, the harness detaches and
        reattaches that drive after the save returns. VICE flushes any
        pending sector writes when an image is detached, so this guarantees
        the host-side .d64 file is consistent on disk and can be inspected
        by other tools without a 'splat' (unclosed file) in its directory.

        The detach window is also a useful hook for callers who want to
        snapshot the image: between detach and reattach there is no other
        writer, so a `shutil.copy()` of `flush_drive_path` produces a
        clean copy without racing the live VICE process. If you need that
        behaviour, override this method or call `binmon.detach_drive()` /
        `binmon.attach_drive()` directly around your snapshot."""
        if len(filename) > 15:
            raise DefmonError(
                f"defMON typed filenames are 15 chars max (defMON prepends "
                f"'.' so the on-disk name fits the 16-byte CBM DOS slot); "
                f"got {len(filename)} chars: {filename!r}"
            )

        # Step 1 — navigate down past every filename until we land on a
        # blank slot. The directory listing is rendered automatically on
        # disk-menu entry; defMON has no "refresh directory" chord (bare
        # SPACE is in the $C491 LUT-ignore set), so just rely on the
        # post-open paint. defMON's directory rows always begin with the
        # block count followed by a space and an open quote on populated
        # rows, so a blank row is one whose first non-blank glyph isn't a
        # digit.
        #
        # We walk the on-screen directory row-by-row from the top (where
        # the '>' cursor starts), pressing CRSRUD (down) for each row,
        # checking after each step that we've moved off populated entries.
        for _ in range(max_dir_rows):
            snap = self.screen()
            cursor_row = self._find_dir_cursor_row(snap)
            if cursor_row is None:
                break
            row_text = snap.lines()[cursor_row].strip()
            # Strip the cursor glyph itself ('>') if present at index 0.
            if row_text.startswith(">"):
                row_text = row_text[1:].strip()
            if self._dir_row_is_blank(row_text):
                break
            self.tap("CRSRUD")  # cursor down
            time.sleep(0.05)

        # Step 3 — open the save prompt.
        self.tap("S")
        time.sleep(0.3)

        # Step 4 — type the filename.
        self.type_text(filename)
        time.sleep(0.2)

        # Step 5 — commit. Wait for the disk menu to disappear (seqED
        # reappears). The seqED has the literal column headers 'JP DL'
        # in row 0 of the sidTAB or 'VOC0/VOC1/VOC2' in seqED itself.
        before = self.screen()
        self.tap("RETURN")
        snap = self._wait_for_disk_menu_exit(before, timeout)

        # Force a flush so the host-side .d64 reflects the new file.
        # See the docstring for why this is also a useful analysis hook.
        # Retry on transient errors: if the 1541 emulation is still
        # writing when we issue the detach, VICE returns err 0x8f
        # (drive busy / write in progress). Waiting and re-attempting
        # gives the write a chance to finish and lets the detach close
        # the file cleanly. Without this loop, the on-disk d64 ends up
        # with a splat (file_type=2) entry for the new file — which is
        # exactly the B1 T03 regression.
        if flush_drive_path is not None:
            backoff_s = 0.5
            last_err: BinmonError | None = None
            for _attempt in range(6):
                try:
                    self.bm.flush_drive(
                        flush_drive_path,
                        unit=flush_unit,
                        drive=flush_drive,
                    )
                    last_err = None
                    break
                except BinmonError as e:
                    last_err = e
                    time.sleep(backoff_s)
                    backoff_s = min(backoff_s * 1.5, 4.0)
            if last_err is not None:
                raise last_err
        return snap

    @staticmethod
    def _find_dir_cursor_row(snap: ScreenSnapshot) -> int | None:
        """Find the screen row whose first non-blank character is '>'."""
        for r, line in enumerate(snap.lines()):
            stripped = line.lstrip()
            if stripped.startswith(">"):
                return r
        return None

    @staticmethod
    def _dir_row_is_blank(text: str) -> bool:
        """A populated defMON dir row begins with hex block count digits
        and a quoted name; a blank row is empty or contains only spaces."""
        text = text.strip()
        if not text:
            return True
        # If the leading char isn't a hex digit, it's not a real entry.
        if text[0] not in "0123456789ABCDEFabcdef":
            return True
        return False

    def _wait_for_disk_menu_exit(
        self, before: ScreenSnapshot, timeout: float
    ) -> ScreenSnapshot:
        """Poll until the screen no longer looks like the disk menu.

        defMON's seqED screen has the literal text 'VOC0', 'VOC1', or
        'VOC2' in the top header row; the disk menu doesn't. After the
        seqED reappears we keep the screen settled — defMON switches
        the display before the 1541 emulation has flushed its last
        block, and racing the container stop (or a follow-up flush_drive
        call) produces an unclosed ('*' splat) file in the directory.

        3.0s settle (bumped from 1.5s after B1 repro showed T03 races
        the flush at the original timing — defMON's save takes longer
        after a heavy action sweep)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self.screen()
            if snap.contains("VOC0") or snap.contains("VOC1") or snap.contains("VOC2"):
                # Stay in seqED for a beat so the 1541 finishes its close.
                time.sleep(3.0)
                return self.screen()
            time.sleep(0.1)
        return self.screen()

    def disk_save_overwrite(self) -> ScreenSnapshot:
        """SHIFT + S — overwrite the currently-loaded file in place."""
        self.tap("LSHIFT", "S")
        return self.wait_for_screen_stable(stable_for=0.6, timeout=self.SAVE_TIMEOUT_S)

    def disk_pack_song(self, packed_filename: Optional[str] = None) -> ScreenSnapshot:
        """SHIFT + P — pack and save. If a name prompt appears, type packed_filename."""
        self.tap("LSHIFT", "P")
        time.sleep(0.3)
        if packed_filename is not None:
            self.type_text(packed_filename)
            self.tap("RETURN")
        return self.wait_for_screen_stable(stable_for=0.6, timeout=self.SAVE_TIMEOUT_S)

    def disk_legacy_write(self) -> ScreenSnapshot:
        """W — write tune in legacy (older defMON) format."""
        self.tap("W")
        return self.wait_for_screen_stable(stable_for=0.4, timeout=self.SAVE_TIMEOUT_S)

    def disk_legacy_read(self) -> ScreenSnapshot:
        """R — read tune in legacy format."""
        self.tap("R")
        return self.wait_for_screen_stable(stable_for=0.4, timeout=self.LOAD_TIMEOUT_S)

    def disk_load_by_index(self, index_key: str = "RETURN") -> ScreenSnapshot:
        """RETURN or L — load the song the cursor is sitting on in the dir."""
        self.tap(index_key)
        return self.wait_for_screen_stable(stable_for=0.6, timeout=self.LOAD_TIMEOUT_S)

    def disk_load_by_name(self, filename: str) -> ScreenSnapshot:
        """L → type name → RETURN. Loads by typed filename."""
        self.tap("L")
        time.sleep(0.2)
        self.type_text(filename)
        self.tap("RETURN")
        return self.wait_for_screen_stable(stable_for=0.6, timeout=self.LOAD_TIMEOUT_S)

    # Throwaway-name wrappers for the disk-action coverage sweep. Each
    # exercises a documented save/load/pack code path with a deterministic
    # name so the action is zero-arg and the on-disk effect is bounded
    # (one extra file per name across the whole sweep). Names are 3-char
    # so they fit comfortably under the 15-char typed limit and stay
    # human-recognisable in the directory listing.

    # Throwaway-name prefix chosen to be (a) all-typeable on the C64
    # matrix — '@' is on the keyboard (row 5 col 6) and goes through
    # `text_to_chords` cleanly; '_' is NOT and would raise. (b) sorts
    # late in the directory so the bundled tune entries stay at the
    # top in defMON's listing. (c) short enough to leave plenty of
    # room under the 15-char typed limit.
    DISK_TRACE_SAVE_NAME = "@SAV"
    DISK_TRACE_PACK_NAME = "@PACK"
    DISK_TRACE_MISSING_NAME = "@NOPE"

    def disk_save_new_throwaway(self) -> ScreenSnapshot:
        """Save the current tune as '_TS'. Caller is responsible for
        the disk menu being open beforehand."""
        return self.disk_save_new(self.DISK_TRACE_SAVE_NAME)

    def disk_load_by_name_throwaway(self) -> ScreenSnapshot:
        """Load '_TS' by name — round-trips the throwaway save. Order
        matters: must run after disk_save_new_throwaway."""
        return self.disk_load_by_name(self.DISK_TRACE_SAVE_NAME)

    def disk_pack_song_throwaway(self) -> ScreenSnapshot:
        """Pack-save the current tune as '_TP' (SHIFT+P + name + RETURN)."""
        return self.disk_pack_song(self.DISK_TRACE_PACK_NAME)

    def disk_load_by_name_missing(self) -> ScreenSnapshot:
        """L → '_NOPE' → RETURN — exercises the load-by-name error path
        (file not found). defMON should print an error and return to
        the menu rather than load anything."""
        return self.disk_load_by_name(self.DISK_TRACE_MISSING_NAME)

    # ========================================================= playback

    def play_from_cursor(self) -> TapOutcome:
        """F1."""
        return self.tap("F1")

    def play_from_start(self) -> TapOutcome:
        """F3."""
        return self.tap("F3")

    def toggle_follow(self) -> TapOutcome:
        """F5."""
        return self.tap("F5")

    def stop_playback(self) -> TapOutcome:
        """F7."""
        return self.tap("F7")

    def mute_track(self, track: int) -> TapOutcome:
        """[ for track 1, ] for 2, = for 3.

        Note: on PETSCII keyboards [ is SHIFT+: and ] is SHIFT+;. defMON
        treats the printed glyph (i.e. SHIFT+colon, SHIFT+semicolon).
        """
        if track == 1:
            return self.tap("LSHIFT", "COLON")  # produces "["
        if track == 2:
            return self.tap("LSHIFT", "SEMICOLON")  # produces "]"
        if track == 3:
            return self.tap("EQUALS")
        raise ValueError(f"track must be 1..3, got {track}")

    # =========================================================== tempo

    def set_multispeed(self, speed: int) -> TapOutcome:
        """F2/F4/F6/F8 → 1x/2x/4x/8x multispeed.

        F2/F4/F6/F8 do not have their own keys on a C64 — they are
        SHIFT + F1 / F3 / F5 / F7."""
        base = {1: "F1", 2: "F3", 4: "F5", 8: "F7"}[speed]
        return self.tap("LSHIFT", base)

    def bump_bpm(self, fkey: str = "F1") -> TapOutcome:
        """C= + Fn — adjust BPM (defMON treats each F-key as a different bump)."""
        return self.tap("CBM", fkey)

    def reset_bpm(self) -> TapOutcome:
        """SHIFT + F1 — reset BPM to default."""
        return self.tap("LSHIFT", "F1")

    # ========================================================= note edit

    def insert_step(self) -> TapOutcome:
        """RETURN — insert a step/note row."""
        return self.tap("RETURN")

    def remove_step(self) -> TapOutcome:
        """SHIFT + RETURN — remove step/note row."""
        return self.tap("LSHIFT", "RETURN")

    def value_decrement(self) -> TapOutcome:
        """< — decrease value (SHIFT + comma)."""
        return self.tap("LSHIFT", "COMMA")

    def value_increment(self) -> TapOutcome:
        """> — increase value (SHIFT + period)."""
        return self.tap("LSHIFT", "PERIOD")

    def delete_value(self) -> TapOutcome:
        """DEL — delete value at cursor (does not advance)."""
        return self.tap("INSTDEL")

    def delete_advance(self) -> TapOutcome:
        """SPACE — delete value at cursor and advance one step."""
        return self.tap("SPACE")

    # --- cursor movement -------------------------------------------------
    # The C64 has only two physical cursor keys — CRSRLR (left/right)
    # and CRSRUD (up/down). Direction is modifier-controlled: bare key =
    # right/down, LSHIFT+key = left/up. Used everywhere the cursor moves
    # in seqED, seqLIST, sidTAB, and the disk-menu directory listing.

    def cursor_right(self, count: int = 1, settle: float = 0.04) -> None:
        for _ in range(count):
            self.tap("CRSRLR")
            time.sleep(settle)

    def cursor_left(self, count: int = 1, settle: float = 0.04) -> None:
        for _ in range(count):
            self.tap("LSHIFT", "CRSRLR")
            time.sleep(settle)

    def cursor_down(self, count: int = 1, settle: float = 0.04) -> None:
        for _ in range(count):
            self.tap("CRSRUD")
            time.sleep(settle)

    def cursor_up(self, count: int = 1, settle: float = 0.04) -> None:
        for _ in range(count):
            self.tap("LSHIFT", "CRSRUD")
            time.sleep(settle)

    # --- step-level value editing in seqED ------------------------------
    # Per the wiki interface_overview, a seqED step has three editable
    # sub-fields routed by modifier-while-typing:
    #
    #   Note (rightmost)        — bare keytap on the chromatic row
    #   Sound Program (instr)   — hold LSHIFT + CBM, type 2 hex digits
    #   Speed                   — hold CTRL   + CBM, type 2 hex digits
    #
    # The cursor stays on the same step+voice cell across these edits;
    # the modifier alone decides which sub-field gets the digit.

    def type_note(self, note_key: str) -> TapOutcome:
        """Type a note at the current cursor step+voice. ``note_key`` is
        the C64-matrix name of the chromatic-row key (Z S X D C V G B H
        N J M for the lower octave's C…B; Q 2 W 3 E R 5 T 6 Y 7 U for
        the next octave up). Octave shift is via ``octave_up()`` /
        ``octave_down()``."""
        return self.tap(note_key)

    @staticmethod
    def _hex_digits(byte_value: int) -> tuple[str, str]:
        if not 0 <= byte_value <= 0xFF:
            raise DefmonError(f"byte out of range: {byte_value}")
        return f"{byte_value >> 4:X}", f"{byte_value & 0xF:X}"

    def type_sound_program(
        self, byte_value: int, per_digit_settle: float = 0.05
    ) -> None:
        """Hold LSHIFT+CBM and tap two hex digits — sets the Sound
        Program (instrument) byte at the current step."""
        for digit in self._hex_digits(byte_value):
            self.tap("LSHIFT", "CBM", digit)
            time.sleep(per_digit_settle)

    def type_speed(self, byte_value: int, per_digit_settle: float = 0.05) -> None:
        """Hold CTRL+CBM and tap two hex digits — sets the Speed byte
        at the current step."""
        for digit in self._hex_digits(byte_value):
            self.tap("CTRL", "CBM", digit)
            time.sleep(per_digit_settle)

    def type_hex_byte(self, byte_value: int, per_digit_settle: float = 0.05) -> None:
        """Tap two hex digits with no modifier. Used in sidTAB cells
        (waveform/ADSR/etc) and seqLIST voice cells (pattern numbers)
        — both modes accept hex digits directly at the cursor."""
        for digit in self._hex_digits(byte_value):
            self.tap(digit)
            time.sleep(per_digit_settle)

    def multi_insert(self, digit: str) -> TapOutcome:
        """CTRL + digit — insert value across the current super-zone."""
        return self.tap("CTRL", digit)

    # ============================================== pattern / instrument

    def clone_pattern_new(self) -> TapOutcome:
        """SHIFT + N — clone current pattern into a new arranger slot."""
        return self.tap("LSHIFT", "N")

    def clone_pattern_unused(self) -> TapOutcome:
        """SHIFT + U — clone into the next unused arranger slot."""
        return self.tap("LSHIFT", "U")

    def edit_instrument_columns(self) -> TapOutcome:
        """C= + SHIFT — toggle instrument column edit mode."""
        return self.tap("CBM", "LSHIFT")

    def edit_speed_column(self) -> TapOutcome:
        """CTRL + C= — toggle speed column edit mode."""
        return self.tap("CTRL", "CBM")

    def insert_pattern_break(self) -> TapOutcome:
        """CTRL + C= + SPACE — insert pattern break."""
        return self.tap("CTRL", "CBM", "SPACE")

    # ===================================================== octave / chunk

    def shift_octave_up(self) -> TapOutcome:
        """CTRL + > (i.e. CTRL + SHIFT + PERIOD)."""
        return self.tap("CTRL", "LSHIFT", "PERIOD")

    def shift_octave_down(self) -> TapOutcome:
        """CTRL + < (i.e. CTRL + SHIFT + COMMA)."""
        return self.tap("CTRL", "LSHIFT", "COMMA")

    def chunk_size_decrease(self) -> TapOutcome:
        """C= + / — sidTAB chunk size down (8→4→2→1→0=full)."""
        return self.tap("CBM", "SLASH")

    def chunk_size_increase(self) -> TapOutcome:
        """SHIFT + / — sidTAB chunk size up. ? glyph on screen."""
        return self.tap("LSHIFT", "SLASH")

    # ========================================================== super cmd

    def super_steps(self, n: int) -> None:
        """CTRL + S then digits — set step count for following edit ops."""
        self.tap("CTRL", "S")
        for ch in str(n):
            self.tap(ch)

    def super_repeat(self, n: int) -> None:
        """CTRL + R then digits."""
        self.tap("CTRL", "R")
        for ch in str(n):
            self.tap(ch)

    def super_width(self, n: int) -> None:
        """CTRL + W then digits — interval between affected steps."""
        self.tap("CTRL", "W")
        for ch in str(n):
            self.tap(ch)

    def super_zone_all(self) -> TapOutcome:
        """CTRL + Z, then A — enter ZONE-ALL super-command mode.
        Returns the outcome of the second tap (A)."""
        self.tap("CTRL", "Z")
        return self.tap("A")

    # CTRL + letter cursor jumps from defMONing-102: CTRL+G/H/J/K move
    # the cursor to position $0/$4/$8/$C respectively. The multi-tap
    # CTRL+G+G ("very first step" = -1) is a distinct code path.

    def cursor_pos_0(self) -> TapOutcome:
        """CTRL + G — cursor to position $0 of the current pattern."""
        return self.tap("CTRL", "G")

    def cursor_pos_4(self) -> TapOutcome:
        """CTRL + H — cursor to position $4 of the current pattern."""
        return self.tap("CTRL", "H")

    def cursor_pos_8(self) -> TapOutcome:
        """CTRL + J — cursor to position $8 of the current pattern."""
        return self.tap("CTRL", "J")

    def cursor_pos_c(self) -> TapOutcome:
        """CTRL + K — cursor to position $C of the current pattern."""
        return self.tap("CTRL", "K")

    def cursor_first_step_twice(self) -> TapOutcome:
        """CTRL + G twice — cursor to '-1 position' (very first step).
        Per defMONing-102, double-press of CTRL+G targets the
        before-position-0 slot, exercising a different branch of the
        cursor-jump handler than a single press."""
        self.tap("CTRL", "G")
        return self.tap("CTRL", "G")

    def super_chain_steps_repeat(self) -> TapOutcome:
        """CTRL+S 4 CTRL+R 2 — sets STEPS=4 then chains into REPEAT=2
        without exiting super-mode. Per the defMONing-102 example,
        super-commands can be chained ('Hold CTRL while typing S 2 0
        R C'); this exercises the super→super transition that single
        super_* actions don't reach."""
        self.tap("CTRL", "S")
        self.tap("4")
        self.tap("CTRL", "R")
        return self.tap("2")

    # Zero-arg wrappers for the digit-bearing super-commands. Each pair
    # below exercises two distinct branches of defMON's value-accumulator
    # state machine: a single-digit value (one digit then implicit
    # commit) and a multi-digit value (×10 + next digit). Without these
    # the value-handling code paths are unreached — super_steps/repeat/
    # width are the only documented commands where defMON has to parse a
    # numeric argument from the keyboard. CTRL+RETURN exits the super-
    # mode after either path completes.

    def super_steps_4(self) -> None:
        """CTRL + S, '4' — STEPS=4 (single-digit value path)."""
        self.super_steps(4)

    def super_steps_16(self) -> None:
        """CTRL + S, '1','6' — STEPS=16 (multi-digit accumulator path)."""
        self.super_steps(16)

    def super_repeat_2(self) -> None:
        """CTRL + R, '2' — REPEAT=2 (single-digit value path)."""
        self.super_repeat(2)

    def super_repeat_12(self) -> None:
        """CTRL + R, '1','2' — REPEAT=12 (multi-digit accumulator path)."""
        self.super_repeat(12)

    def super_width_3(self) -> None:
        """CTRL + W, '3' — WIDTH=3 (single-digit value path)."""
        self.super_width(3)

    def super_width_24(self) -> None:
        """CTRL + W, '2','4' — WIDTH=24 (multi-digit accumulator path)."""
        self.super_width(24)

    def super_exit(self) -> TapOutcome:
        """CTRL + RETURN — exit super-command mode (border colour clears)."""
        return self.tap("CTRL", "RETURN")

    # ====================================================== bookkeeping

    def tap_chord(self, *names: str, **kw) -> TapOutcome:
        """Public alias for tapping arbitrary chords (notes, etc.)."""
        return self.tap(*names, **kw)

    def all_documented_actions(self) -> list[tuple[str, "Callable[[], object]"]]:
        """Index of every documented (zero-arg) command. Used by the smoke
        test to exercise every shortcut without typing the same list twice."""
        return [
            ("toggle_seqed_seqlist", self.toggle_seqed_seqlist),
            ("jump_arranger_position", self.jump_arranger_position),
            ("enter_sidtab", self.enter_sidtab),
            ("jump_sidtab_position", self.jump_sidtab_position),
            ("jump_sidtab_position_alt", self.jump_sidtab_position_alt),
            ("switch_sid_chip", self.switch_sid_chip),
            ("toggle_stereo", self.toggle_stereo),
            ("cycle_sid_high_byte", self.cycle_sid_high_byte),
            ("adjust_sid_low_byte", self.adjust_sid_low_byte),
            ("play_from_cursor", self.play_from_cursor),
            ("play_from_start", self.play_from_start),
            ("toggle_follow", self.toggle_follow),
            ("stop_playback", self.stop_playback),
            ("reset_bpm", self.reset_bpm),
            ("insert_step", self.insert_step),
            ("remove_step", self.remove_step),
            ("value_decrement", self.value_decrement),
            ("value_increment", self.value_increment),
            ("delete_value", self.delete_value),
            ("delete_advance", self.delete_advance),
            ("clone_pattern_new", self.clone_pattern_new),
            ("clone_pattern_unused", self.clone_pattern_unused),
            ("edit_instrument_columns", self.edit_instrument_columns),
            ("edit_speed_column", self.edit_speed_column),
            ("insert_pattern_break", self.insert_pattern_break),
            ("shift_octave_up", self.shift_octave_up),
            ("shift_octave_down", self.shift_octave_down),
            ("chunk_size_decrease", self.chunk_size_decrease),
            ("chunk_size_increase", self.chunk_size_increase),
            ("super_zone_all", self.super_zone_all),
            ("cursor_pos_0", self.cursor_pos_0),
            ("cursor_pos_4", self.cursor_pos_4),
            ("cursor_pos_8", self.cursor_pos_8),
            ("cursor_pos_c", self.cursor_pos_c),
            ("cursor_first_step_twice", self.cursor_first_step_twice),
            ("super_chain_steps_repeat", self.super_chain_steps_repeat),
            ("super_steps_4", self.super_steps_4),
            ("super_steps_16", self.super_steps_16),
            ("super_repeat_2", self.super_repeat_2),
            ("super_repeat_12", self.super_repeat_12),
            ("super_width_3", self.super_width_3),
            ("super_width_24", self.super_width_24),
            ("super_exit", self.super_exit),
        ]

    def all_documented_disk_actions(self) -> list[tuple[str, "Callable[[], object]"]]:
        """Index of disk-menu-mode commands. Caller is responsible for
        opening the disk menu before the first action and for ensuring
        the menu is open again before each subsequent action — most of
        these (save, load, pack, overwrite) return to seqED on
        completion; the caller can use :func:`Defmon.ensure_seqed` +
        :func:`Defmon.open_disk_menu` to re-open the menu between
        actions.

        Order matters for the round-trip pair: save_new_throwaway
        creates the '_TS' file that load_by_name_throwaway then loads.
        save_overwrite needs a previously-loaded file to target — placed
        after load_by_name_throwaway so '_TS' is the active name.

        legacy_read/write probe defMON's older-format read/write paths.
        They may error in the absence of a matching file but the
        keyboard handler still fires, which is the point. The error
        path itself is interesting code coverage."""
        return [
            ("disk_read_directory", self.disk_read_directory),
            ("disk_next_drive", self.disk_next_drive),
            ("disk_prev_drive", self.disk_prev_drive),
            ("disk_save_new_throwaway", self.disk_save_new_throwaway),
            ("disk_load_by_name_throwaway", self.disk_load_by_name_throwaway),
            ("disk_save_overwrite", self.disk_save_overwrite),
            ("disk_pack_song_throwaway", self.disk_pack_song_throwaway),
            ("disk_load_by_name_missing", self.disk_load_by_name_missing),
            ("disk_legacy_write", self.disk_legacy_write),
            ("disk_legacy_read", self.disk_legacy_read),
            ("close_disk_menu", self.close_disk_menu),
        ]
