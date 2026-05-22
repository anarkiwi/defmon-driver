"""Live smoke for chord-driven note writes in :mod:`defmon_driver.field_setter`.

Boots defMON in asid-vice, runs :func:`field_setter.write_note_chord`
for each note key in the lower octave (Z..M), and verifies the byte
that lands in pattern memory equals
:data:`field_setter.NOTE_PATTERN_BYTES` for that key.

This protects against the pre-v0.2 bug where ``NOTE_KEYCODES`` held
note-byte values (Z=$30, S=$31, …) and was being used as the ``$0E44``
keycode for ``press_via_loop`` — every chord-driven note write produced
the wrong note byte. The smoke now exercises the production code path
end-to-end against a real container.

Run::

    python -m defmon_driver.smoke_note_chord /path/to/defmon.d64
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback
from pathlib import Path

from ._smoke_support import section, smoke_session
from .field_setter import NOTE_PATTERN_BYTES, PATTERN_BASE, write_note_chord

log = logging.getLogger("smoke-note-chord")

# Scan window for the diff. Pattern memory plus a generous slack so the
# search catches wherever defMON's cursor happens to land after a fresh
# boot (the cursor isn't reset to step 0 / V0 by default).
SCAN_BASE = PATTERN_BASE
SCAN_LEN = 0x1000


def find_note_write(
    pre: bytes, post: bytes, expected: int
) -> tuple[int | None, list[tuple[int, int, int]]]:
    """Return (offset_of_expected_write, all_diffs).

    ``offset_of_expected_write`` is the first byte offset whose post-value
    equals ``expected`` (the note-byte value) and whose pre-value differed.
    ``all_diffs`` is every changed byte (used for diagnostics).
    """
    diffs = [(i, pre[i], post[i]) for i in range(len(pre)) if pre[i] != post[i]]
    note_writes = [i for (i, _p, q) in diffs if q == expected]
    return (note_writes[0] if note_writes else None), diffs


def run(d64_path: Path, port: int) -> int:
    failures: list[str] = []
    try:
        with smoke_session(d64_path, port=port, prefix="defmon-note-chord-") as s:
            bm, d = s.bm, s.d
            section(f"CONTAINER {s.container.container_id} on :{port}")
            section("BOOT")
            print(f"  mode = {d.current_mode_name()}")

            section("EXERCISE write_note_chord")
            print(
                f"  {'key':<4s} {'expected':>10s} {'addr':>8s} "
                f"{'other_changes':>14s} {'status':>8s}"
            )
            for key, expected in NOTE_PATTERN_BYTES.items():
                pre = bm.mem_get(SCAN_BASE, SCAN_BASE + SCAN_LEN - 1)
                write_note_chord(bm, key)
                time.sleep(0.05)
                post = bm.mem_get(SCAN_BASE, SCAN_BASE + SCAN_LEN - 1)
                off, diffs = find_note_write(pre, post, expected)
                other = len(diffs) - (1 if off is not None else 0)
                if off is None:
                    status = "FAIL"
                    failures.append(
                        f"{key}: expected 0x{expected:02X} not written; "
                        f"{len(diffs)} byte change(s): "
                        + ", ".join(
                            f"${SCAN_BASE + i:04X}:0x{p:02X}->0x{q:02X}" for (i, p, q) in diffs[:4]
                        )
                    )
                    print(f"  {key:<4s} 0x{expected:08X} {'-':>8s} {other:>14d} {status:>8s}")
                else:
                    status = "ok"
                    print(
                        f"  {key:<4s} 0x{expected:>08X} ${SCAN_BASE + off:04X} "
                        f"{other:>14d} {status:>8s}"
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
    print(f"  PASS — all {len(NOTE_PATTERN_BYTES)} note chords wrote the expected byte")
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
