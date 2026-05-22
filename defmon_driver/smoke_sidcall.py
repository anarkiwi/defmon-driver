"""Live smoke for chord-driven sidCALL writes across V0/V1/V2.

Boots defMON in asid-vice and exercises ``set_field(sub_field='sidcall1',
prefer_direct=False)`` for every (voice, step, value) cross-product
inside V0/V1/V2 × {step 0, 3, 7} × {0xAB, 0x57, 0x00, 0xFF}. Verifies
each chord write lands the requested byte at the runtime cell address.

This protects against the pre-fix bug (BUGS.md #2) where sidCALL1 on
V0/V2 silently mis-wrote: defMON's main loop restored ``$71CD`` between
the two digit presses, so the low-nibble press landed on V0's pattern
instead of V1/V2's. ``Sid2Chord.press`` now re-seeds the cursor before
each press; this smoke catches future regressions.

Run::

    python -m defmon_driver.smoke_sidcall /path/to/defmon.d64
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

from ._smoke_support import section, smoke_session
from .field_setter import runtime_cell_address, set_field

log = logging.getLogger("smoke-sidcall")


def run(d64_path: Path, port: int) -> int:
    failures: list[str] = []
    try:
        with smoke_session(d64_path, port=port, prefix="defmon-sidcall-") as s:
            bm, d = s.bm, s.d
            section(f"CONTAINER {s.container.container_id} on :{port}")
            section("BOOT")
            print(f"  mode = {d.current_mode_name()}")

            section("EXERCISE chord-driven sidcall1")
            print(
                f"  {'voice':<5s} {'step':<5s} {'value':<7s} {'target':<8s} "
                f"{'read':<6s} {'status':<8s}"
            )
            for voice in (0, 1, 2):
                for step in (0, 3, 7):
                    for value in (0xAB, 0x57, 0x00, 0xFF):
                        addr = runtime_cell_address(bm, voice, step, "sidcall1")
                        res = set_field(
                            bm,
                            mode="seqed",
                            voice=voice,
                            step=step,
                            sub_field="sidcall1",
                            value=value,
                            prefer_direct=False,
                        )
                        actual = bm.mem_get(addr, addr)[0]
                        ok = actual == (value & 0xFF) and res.ok
                        status = "ok" if ok else "FAIL"
                        print(
                            f"  V{voice:<4d} {step:<5d} 0x{value:02X}    "
                            f"${addr:04X}    0x{actual:02X}   {status:<8s}"
                        )
                        if not ok:
                            failures.append(
                                f"V{voice} step={step} val=0x{value:02X}: "
                                f"target=${addr:04X} read=0x{actual:02X} res.ok={res.ok}"
                            )
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        failures.append(f"top-level: {e}")

    section("RESULT")
    if failures:
        print(f"  FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  PASS — every chord-driven sidcall1 write landed the requested byte")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("d64", help="path to defMON .d64 image")
    p.add_argument("--port", type=int, default=6502)
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    return run(Path(args.d64), args.port)


if __name__ == "__main__":
    sys.exit(main())
