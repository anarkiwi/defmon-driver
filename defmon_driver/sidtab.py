"""High-level sidTAB editing API.

Built on top of :class:`defmon_driver.defmon.Defmon`. Consumes the
calibration JSON produced by :mod:`defmon_driver.calibrate_sidtab` so
callers can write arbitrary values into any (row, column) without
thinking about CRSRLR/CRSRUD nav offsets.

Caller contract::

    bm = BinMon(...); bm.connect()
    d = Defmon(bm)
    d.wait_for_defmon_loaded()
    cursor_load_tune(d, tune)
    sidtab = SidTab.from_calibration(d, "sidtab_calibration.json")
    sidtab.enter()
    sidtab.set(row=4, column="AD", value=0x5C)
    sidtab.set(row=3, column="JP", value=0x02)
    sidtab.clear(row=7, column="SR")
    sidtab.set(row=0, column="ACID", value=0x0200)   # 4-digit min
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from .defmon import Defmon

log = logging.getLogger(__name__)

# Hex digits used to write values. PETSCII screencodes:
#   '0'..'9' -> 0x30..0x39
#   'A'..'F' -> 0x01..0x06   (upper/graphics charset)
_DIGIT_KEYS = "0123456789ABCDEF"


@dataclass
class ColumnInfo:
    name: str
    first_screen_col: int
    n_digits: int
    nav_idx_to_first_digit: int
    cell_cols: list[int]


class SidTabError(RuntimeError):
    pass


class SidTab:
    """Stateful sidTAB editor wrapper.

    The wrapper tracks:
      - current_row: which sidTAB row the cursor is on (0-indexed)
      - current_nav_idx: how many auto-advance ticks the cursor is past
        its sidTAB-entry position; equivalent to "the cursor is at the
        cell at position N in the calibration order"

    `enter()` resets both to (0, 0) by calling d.enter_sidtab().
    `set()` and `clear()` compute the delta nav and walk it via CRSRLR /
    LSHIFT+CRSRLR; row deltas via CRSRUD / LSHIFT+CRSRUD.
    """

    def __init__(self, d: Defmon, calibration: dict):
        self.d = d
        self.cal = calibration
        self.column_map: dict[str, ColumnInfo] = {}
        for name, rec in calibration["column_map"].items():
            self.column_map[name] = ColumnInfo(
                name=name,
                first_screen_col=rec["first_screen_col"],
                n_digits=rec["n_digits"],
                nav_idx_to_first_digit=rec["nav_idx_to_first_digit"],
                cell_cols=list(rec["cell_cols"]),
            )
        # Per defmoning_102.txt, WG is actually TWO logical fields:
        #   - first WG (upper nibble of cells 9-10)  = waveform bits
        #     (+1 triangle, +2 saw, +4 pulse, +8 noise)
        #   - second WG (lower nibble of cells 11-12) = gate/sync/ring/test
        # The doubled WG header in the on-screen layout is intentional,
        # not a typo. We split it here so callers can write the two
        # fields independently while still being able to write the
        # raw 4-digit WG block if they want.
        wg = self.column_map.get("WG")
        if wg is not None and wg.n_digits == 4 and len(wg.cell_cols) >= 4:
            self.column_map["WG_wave"] = ColumnInfo(
                name="WG_wave",
                first_screen_col=wg.cell_cols[0],
                n_digits=2,
                nav_idx_to_first_digit=wg.nav_idx_to_first_digit,
                cell_cols=wg.cell_cols[:2],
            )
            self.column_map["WG_gate"] = ColumnInfo(
                name="WG_gate",
                first_screen_col=wg.cell_cols[2],
                n_digits=2,
                nav_idx_to_first_digit=wg.nav_idx_to_first_digit + 2,
                cell_cols=wg.cell_cols[2:4],
            )
        self.crsrlr_to_wrap: int = calibration.get("crsrlr_to_wrap") or 0
        self.current_row: int = 0
        self.current_nav_idx: int = 0
        self._entered: bool = False

    @classmethod
    def from_calibration(cls, d: Defmon, path: str | Path) -> "SidTab":
        cal = json.loads(Path(path).read_text())
        return cls(d, cal)

    # ---- entry / navigation ----------------------------------------

    def enter(self) -> None:
        """LEFTARROW to enter sidTAB; reset state tracking. Assumes caller
        has loaded the tune."""
        self.d.enter_sidtab()
        time.sleep(0.15)
        self.current_row = 0
        self.current_nav_idx = 0
        self._entered = True

    def _ensure_entered(self) -> None:
        if not self._entered:
            raise SidTabError("sidTAB not entered; call enter() first")

    def _walk_nav(self, target_nav_idx: int) -> None:
        """Adjust the cursor's horizontal position so its auto-advance
        index matches `target_nav_idx`. Walks right (CRSRLR) or left
        (LSHIFT+CRSRLR) — whichever is shorter, with wrap-around when
        crsrlr_to_wrap is known."""
        cur = self.current_nav_idx
        delta = target_nav_idx - cur
        if delta == 0:
            return
        wrap = self.crsrlr_to_wrap
        if wrap and abs(delta) > wrap // 2:
            # Going the short way around the wrap is cheaper.
            delta = delta - (wrap if delta > 0 else -wrap)
        if delta > 0:
            for _ in range(delta):
                self.d.tap("CRSRLR", settle=0.04)
        else:
            for _ in range(-delta):
                self.d.tap("LSHIFT", "CRSRLR", settle=0.04)
        self.current_nav_idx = target_nav_idx

    def _walk_row(self, target_row: int) -> None:
        cur = self.current_row
        delta = target_row - cur
        if delta == 0:
            return
        if delta > 0:
            for _ in range(delta):
                self.d.tap("CRSRUD", settle=0.04)
        else:
            for _ in range(-delta):
                self.d.tap("LSHIFT", "CRSRUD", settle=0.04)
        self.current_row = target_row

    # ---- editing ----------------------------------------------------

    def goto(self, row: int, column: str) -> None:
        """Position cursor at the first digit of (row, column)."""
        self._ensure_entered()
        ci = self.column_map.get(column)
        if ci is None:
            raise SidTabError(
                f"unknown column {column!r}; " f"known: {sorted(self.column_map)}"
            )
        self._walk_nav(ci.nav_idx_to_first_digit)
        self._walk_row(row)

    def set(self, row: int, column: str, value: int) -> None:
        """Write `value` into (row, column).

        For an n-digit column, value's range is 0..(16**n - 1). The
        high nibble is typed first; defMON auto-advances the cursor
        across the column's digits, so by the end of the write the
        cursor is on the cell *after* the column's last digit. We
        update our nav tracker to match."""
        self._ensure_entered()
        ci = self.column_map.get(column)
        if ci is None:
            raise SidTabError(f"unknown column {column!r}")
        max_value = (1 << (4 * ci.n_digits)) - 1
        if not 0 <= value <= max_value:
            raise SidTabError(
                f"value 0x{value:x} out of range for column {column} "
                f"(n_digits={ci.n_digits}, max=0x{max_value:x})"
            )
        self.goto(row, column)
        for shift in range((ci.n_digits - 1) * 4, -1, -4):
            digit = (value >> shift) & 0xF
            self.d.tap(_DIGIT_KEYS[digit], settle=0.04)
        # Cursor auto-advanced n_digits cells.
        self.current_nav_idx += ci.n_digits

    def clear(self, row: int, column: str) -> None:
        """Restore each digit of (row, column) to its post-load default.

        Uses SPACE (delete-and-advance) per digit; SPACE restores the
        0x2d ('-') default for most sidTAB cells."""
        self._ensure_entered()
        ci = self.column_map.get(column)
        if ci is None:
            raise SidTabError(f"unknown column {column!r}")
        self.goto(row, column)
        for _ in range(ci.n_digits):
            self.d.tap("SPACE", settle=0.04)
        self.current_nav_idx += ci.n_digits

    # ---- introspection --------------------------------------------

    def columns(self) -> list[str]:
        return sorted(self.column_map)

    def column(self, name: str) -> ColumnInfo:
        ci = self.column_map.get(name)
        if ci is None:
            raise SidTabError(f"unknown column {name!r}")
        return ci
