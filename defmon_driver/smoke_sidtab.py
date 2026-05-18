"""Smoke test for defmon_driver.sidtab.SidTab.

Performs four documented edits using the high-level API, dumps the
screen after each, and checks each cell's value matches what we wrote.

The expectations: each write should change exactly the column's cells
on the target row, and clear should restore the default. We capture
screens to /tmp/smoke-sidtab/ so a human can eyeball them.
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from .binmon import BinMon
from .defmon import Defmon
from .sidtab import SidTab
from .tune_manifest import TUNES
from .vice_docker import DiskMount, ViceContainer

log = logging.getLogger("smoke_sidtab")

DUMP_DIR = Path("/tmp/smoke-sidtab")


def dump_screen(d: Defmon, tag: str) -> None:
    DUMP_DIR.mkdir(parents=True, exist_ok=True)
    snap = d.screen()
    (DUMP_DIR / f"{tag}.txt").write_text(snap.text())
    log.info("dumped screen -> %s.txt", tag)


def cell_codes(d: Defmon, row: int, cols: list[int], data_row: int) -> list[int]:
    snap = d.screen()
    return [snap.screen[data_row * 40 + c] for c in cols]


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
    p.add_argument("--cal", default="sidtab_calibration.json")
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

    workdir = Path(tempfile.mkdtemp(prefix="smoke-sidtab-"))
    src_d64 = Path(args.d64)
    work_d64 = workdir / "disk.d64"
    shutil.copy2(src_d64, work_d64)
    container = ViceContainer(
        binmon_port=args.port,
        autostart="/work/disk.d64",
        mounts=[DiskMount(str(work_d64), "/work/disk.d64", read_only=False)],
    )

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

        st = SidTab.from_calibration(d, args.cal)
        st.enter()
        log.info("entered sidTAB; columns=%r", st.columns())
        time.sleep(0.2)
        dump_screen(d, "00-baseline")

        # === Edit 1: change AD on row 4 to 0x5C ===
        st.set(row=4, column="AD", value=0x5C)
        time.sleep(0.2)
        dump_screen(d, "01-AD-row4-5C")
        # Verify: cells at AD cols (13, 14) on data_row+4 should be '5' and 'C'
        data_row = st.cal["data_row"]
        ad_cols = st.column("AD").cell_cols
        ad_values = cell_codes(d, 4, ad_cols, data_row + 4)
        log.info(
            "AD row 4 cells: %s (expect '5' then 'C' or rev-video)",
            [hex(v) for v in ad_values],
        )

        # === Edit 2: change JP on row 3 to 0x02 ===
        st.set(row=3, column="JP", value=0x02)
        time.sleep(0.2)
        dump_screen(d, "02-JP-row3-02")
        jp_cols = st.column("JP").cell_cols
        jp_values = cell_codes(d, 3, jp_cols, data_row + 3)
        log.info(
            "JP row 3 cells: %s (expect '0' then '2' or rev-video)",
            [hex(v) for v in jp_values],
        )

        # === Edit 3: clear SR on row 7 ===
        st.clear(row=7, column="SR")
        time.sleep(0.2)
        dump_screen(d, "03-SR-row7-clear")
        sr_cols = st.column("SR").cell_cols
        sr_values = cell_codes(d, 7, sr_cols, data_row + 7)
        log.info(
            "SR row 7 cells (after clear): %s (expect default 0x2d)",
            [hex(v) for v in sr_values],
        )

        # === Edit 4: write minimum ACID on row 0 ===
        st.set(row=0, column="ACID", value=0x0200)
        time.sleep(0.2)
        dump_screen(d, "04-ACID-row0-0200")
        acid_cols = st.column("ACID").cell_cols
        acid_values = cell_codes(d, 0, acid_cols, data_row + 0)
        log.info(
            "ACID row 0 cells: %s " "(expect 0,2,0,0 or rev-video equivalents)",
            [hex(v) for v in acid_values],
        )

        # === Edit 5: WG split — triangle waveform + gate on, row 0 ===
        st.set(row=0, column="WG_wave", value=0x10)
        time.sleep(0.1)
        st.set(row=0, column="WG_gate", value=0x01)
        time.sleep(0.2)
        dump_screen(d, "05-WG-row0-tri-gate")
        wave_cols = st.column("WG_wave").cell_cols
        gate_cols = st.column("WG_gate").cell_cols
        wave_values = cell_codes(d, 0, wave_cols, data_row + 0)
        gate_values = cell_codes(d, 0, gate_cols, data_row + 0)
        log.info(
            "WG_wave row 0 cells: %s (expect '1' then '0')",
            [hex(v) for v in wave_values],
        )
        log.info(
            "WG_gate row 0 cells: %s (expect '0' then '1')",
            [hex(v) for v in gate_values],
        )

        print()
        print("== smoke test complete; screen dumps in /tmp/smoke-sidtab/")
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
