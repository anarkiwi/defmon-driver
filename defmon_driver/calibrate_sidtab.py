"""Calibrate the sidTAB cursor-stop → column-name mapping.

Approach:
  1. Boot defMON, load tune, enter sidTAB.
  2. Capture screen; locate the header row (the row that contains "JP"
     and "DL" — the leftmost two columns named in the wiki).
  3. Walk via auto-advance: tap a unique digit per iteration; find the
     cell that took on its PETSCII code in two consecutive snapshots
     (filters player-IRQ flicker); record (nav_idx, cell, digit).
  4. The cursor wraps back to its starting position when CRSRLR has
     visited every editable cell; the walk ends when we re-visit
     nav_idx=0's cell.
  5. For each visited cell (row=data_row, col=C), look up the header
     letter at (header_row, C). Cluster consecutive cells whose header
     letters spell a known column name (JP, DL, WG, AD, SR, TR, AF,
     PW, PS, RE, FV, CP, ACID).

Outputs ``sidtab_calibration.json`` (relative to CWD by default) with keys:

  - ``cursor_entry``        — first cell touched after enter_sidtab()
  - ``header_row``          — screen row containing the column letters
  - ``data_row``            — screen row holding the active sidTAB row 0
  - ``cells_in_order``      — list of (nav_idx, row, col, written_digit)
                              ordered by CRSRLR advance from entry
  - ``column_map``          — column name → {first_col, n_digits,
                              nav_idx_to_first_digit, cell_cols}
  - ``crsrlr_to_wrap``      — number of CRSRLR-equivalent taps before
                              the cursor cycles back to entry

CLI::

    python -m defmon_driver.calibrate_sidtab --port 6711 --tune ".GLOW WORM"
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Optional

from .binmon import BinMon
from .defmon import Defmon, DefmonError
from .tune_manifest import TUNES
from .vice_docker import DiskMount, ViceContainer

log = logging.getLogger("calibrate_sidtab")

COLS = 40
MAX_TAPS = 60  # enough for at least one full cycle

# Wiki column-name list, in the order they appear left-to-right on screen.
WIKI_COLUMNS = [
    "JP",
    "DL",
    "WG",
    "AD",
    "SR",
    "TR",
    "AF",
    "PW",
    "PS",
    "RE",
    "FV",
    "CP",
    "ACID",
]


def screen_bytes(d: Defmon) -> bytes:
    return bytes(d.screen().screen)


def screencode_of(digit: str) -> int:
    if digit.isdigit():
        return 0x30 + int(digit)
    if "A" <= digit.upper() <= "F":
        return 0x01 + (ord(digit.upper()) - ord("A"))
    raise ValueError(f"not a hex digit: {digit!r}")


def screencode_to_ascii(code: int) -> str:
    """Lossy single-character mapping for cleartext debug + header lookup."""
    c = code & 0x7F
    if c == 0:
        return "@"
    if 1 <= c <= 26:
        return chr(ord("A") + c - 1)
    if 32 <= c <= 63:
        return chr(c)
    return "."


def render_row(buf: bytes, row: int) -> str:
    return "".join(screencode_to_ascii(buf[row * COLS + c]) for c in range(COLS))


def find_header_row(buf: bytes) -> Optional[int]:
    """The header row contains both 'JP' and 'DL' as substring fragments."""
    for r in range(25):
        s = render_row(buf, r)
        if "JP" in s and "DL" in s:
            return r
    return None


def find_data_row(buf: bytes, header_row: int) -> int:
    """The first data row is right below the header row. We pick the
    nearest row containing a hex-digit glyph in a column right of the
    leftmost header letter."""
    # Heuristic: data row is header_row + 2 (one blank separator).
    # Fall back to header_row + 1 if that's blank.
    for delta in (2, 1, 3):
        r = header_row + delta
        if r >= 25:
            continue
        s = render_row(buf, r)
        # Must contain at least one digit/letter in cols >= 2
        if any(c.isalnum() for c in s[2:]):
            return r
    return header_row + 2


# ------------------------------------------------------- main walk


def walk_and_map(
    d: Defmon, header_row: int, data_row: int, max_taps: int = MAX_TAPS
) -> dict:
    """Type unique digits in sequence; identify each written cell with
    two-snapshot confirmation; stop when the cursor has cycled."""
    seq = "13579BDF02468ACE"
    cells: list[dict] = []
    snap_prev = screen_bytes(d)
    first_cell: Optional[tuple[int, int]] = None
    revisit_at: Optional[int] = None

    for tap_idx in range(max_taps):
        digit = seq[tap_idx % 16]
        target = screencode_of(digit)
        target_codes = {target, target | 0x80}

        try:
            d.tap(digit, settle=0.04)
        except DefmonError as e:
            cells.append({"tap_idx": tap_idx, "digit": digit, "error": str(e)})
            break

        # Two-snapshot confirm: cell must hold target in BOTH.
        time.sleep(0.05)
        snap_a = screen_bytes(d)
        time.sleep(0.05)
        snap_b = screen_bytes(d)

        written: Optional[tuple[int, int]] = None
        for i in range(len(snap_prev)):
            if (
                snap_a[i] in target_codes
                and snap_b[i] in target_codes
                and snap_prev[i] not in target_codes
            ):
                row, col = i // COLS, i % COLS
                if row != data_row:
                    continue
                written = (row, col)
                break

        if written is None:
            cells.append(
                {
                    "tap_idx": tap_idx,
                    "digit": digit,
                    "cell": None,
                    "header_letter": None,
                }
            )
        else:
            row, col = written
            header_letter = (
                chr(0)
                if header_row < 0
                else screencode_to_ascii(snap_b[header_row * COLS + col])
            )
            cells.append(
                {
                    "tap_idx": tap_idx,
                    "digit": digit,
                    "cell": [row, col],
                    "header_letter": header_letter,
                }
            )
            if first_cell is None:
                first_cell = written
            elif written == first_cell and tap_idx > 0:
                revisit_at = tap_idx
                log.info(
                    "cursor cycled back to entry cell (%d,%d) at " "tap %d",
                    first_cell[0],
                    first_cell[1],
                    tap_idx,
                )
                break
        snap_prev = snap_b

    return {
        "cells_in_order": cells,
        "cursor_entry_cell": list(first_cell) if first_cell else None,
        "crsrlr_to_wrap": revisit_at,
    }


# ---------------------------------------------------- column grouping


def derive_column_map(header_text: str, cells_in_order: list[dict]) -> dict:
    """From header letters and visit order, derive {column_name -> info}.

    Strategy: scan ``cells_in_order`` and for each cell look at the
    header letter directly above. Greedy-match the longest column name
    in WIKI_COLUMNS that starts at this column letter position in the
    header. Group consecutive cells sharing the same column-name match.
    """
    # Strip cells with no write or wrong row
    write_cells = [c for c in cells_in_order if c.get("cell")]

    # Build header_letter -> column inferral by scanning the header text.
    # Each column abbreviation appears at a known col index; we map
    # screen-col → column-name by scanning left-to-right.
    col_to_column: dict[int, str] = {}
    i = 0
    while i < len(header_text):
        ch = header_text[i]
        if not ch.isalpha():
            i += 1
            continue
        # Greedy-match longest column name starting here.
        best: Optional[str] = None
        for name in sorted(WIKI_COLUMNS, key=lambda x: -len(x)):
            if header_text[i : i + len(name)] == name:
                best = name
                break
        if best is None:
            i += 1
            continue
        for off in range(len(best)):
            col_to_column[i + off] = best
        i += len(best)

    # Group consecutive write_cells by their inferred column.
    column_map: dict[str, dict] = {}
    for entry in write_cells:
        if entry["cell"] is None:
            continue
        _, col = entry["cell"]
        column = col_to_column.get(col)
        if column is None:
            continue
        rec = column_map.setdefault(
            column,
            {
                "first_screen_col": col,
                "cell_cols": [],
                "nav_idx_to_first_digit": entry["tap_idx"],
            },
        )
        rec["cell_cols"].append(col)
        if col < rec["first_screen_col"]:
            rec["first_screen_col"] = col
        if entry["tap_idx"] < rec["nav_idx_to_first_digit"]:
            rec["nav_idx_to_first_digit"] = entry["tap_idx"]

    # Fill in n_digits as the count of distinct cell_cols.
    for rec in column_map.values():
        rec["cell_cols"] = sorted(set(rec["cell_cols"]))
        rec["n_digits"] = len(rec["cell_cols"])

    return column_map


# ---------------------------------------------------- driver


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--port", type=int, default=6711)
    p.add_argument(
        "--tune",
        default=".GLOW WORM",
        help="example-tune name in defmon_driver.tune_manifest",
    )
    p.add_argument(
        "--d64", required=True, help="path to defMON d64 image containing the tune"
    )
    p.add_argument("--out", default="sidtab_calibration.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    tune = next((t for t in TUNES if t.name == args.tune), None)
    if tune is None:
        print(f"tune not found: {args.tune}", file=sys.stderr)
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="calibrate-sidtab-"))
    src_d64 = Path(args.d64)
    work_d64 = workdir / "disk.d64"
    shutil.copy2(src_d64, work_d64)
    container = ViceContainer(
        binmon_port=args.port,
        autostart="/work/disk.d64",
        mounts=[DiskMount(str(work_d64), "/work/disk.d64", read_only=False)],
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rc = 0
    try:
        container.start()
        from .tune_navigation import cursor_load_tune as _cursor_load_tune

        bm = BinMon("127.0.0.1", args.port)
        bm.connect(timeout=15.0, attempts=120, retry_delay=0.25)
        bm.exit()
        d = Defmon(bm)
        d.wait_for_defmon_loaded(timeout=120.0)
        _cursor_load_tune(d, tune)
        d.stop_playback()
        time.sleep(0.15)
        d.enter_sidtab()
        time.sleep(0.2)

        snap_pre = screen_bytes(d)
        header_row = find_header_row(snap_pre)
        if header_row is None:
            raise RuntimeError("header row (with JP+DL) not found")
        data_row = find_data_row(snap_pre, header_row)
        header_text = render_row(snap_pre, header_row)
        log.info("header_row=%d (text=%r)", header_row, header_text)
        log.info("data_row=%d", data_row)

        walk = walk_and_map(d, header_row, data_row)
        column_map = derive_column_map(header_text, walk["cells_in_order"])

        result = {
            "tune": tune.name,
            "header_row": header_row,
            "data_row": data_row,
            "header_text": header_text,
            "cursor_entry_cell": walk["cursor_entry_cell"],
            "crsrlr_to_wrap": walk["crsrlr_to_wrap"],
            "cells_in_order": walk["cells_in_order"],
            "column_map": column_map,
        }
        out_path.write_text(json.dumps(result, indent=2) + "\n")

        # Pretty print
        print()
        print(f"header_row     = {header_row}")
        print(f"data_row       = {data_row}")
        print(f"header_text    = {header_text!r}")
        print(f"cursor_entry   = {walk['cursor_entry_cell']}")
        print(f"crsrlr_to_wrap = {walk['crsrlr_to_wrap']}")
        print()
        print(f"== column map (n={len(column_map)})")
        print(
            f"  {'col':<6s} {'1st_screen_col':>14s} {'n_digits':>9s} "
            f"{'nav_to_first':>12s} cell_cols"
        )
        for name in WIKI_COLUMNS:
            rec = column_map.get(name)
            if rec is None:
                print(f"  {name:<6s} -- not found --")
            else:
                print(
                    f"  {name:<6s} {rec['first_screen_col']:>14d} "
                    f"{rec['n_digits']:>9d} "
                    f"{rec['nav_idx_to_first_digit']:>12d} "
                    f"{rec['cell_cols']}"
                )
        print(f"\nfull JSON -> {out_path}")
        bm.close()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"FATAL: {e}", file=sys.stderr)
        rc = 1
    finally:
        container.stop()
        shutil.rmtree(workdir, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
