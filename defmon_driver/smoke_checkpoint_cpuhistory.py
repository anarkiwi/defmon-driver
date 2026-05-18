"""Live test for the new checkpoint_set / cpuhistory_get wrappers.

Boots defMON in asid-vice and asserts:

  1. checkpoint_set returns a Checkpoint with the start/end/op echo'd
     back, hit_count starts at 0.
  2. After firing F1 (play-from-cursor), the same checknum's hit_count
     has advanced -- proves the player is actually executing inside
     [$1000, $BFFF].
  3. checkpoint_list sees that checkpoint, and checkpoint_delete makes
     it disappear.
  4. cpuhistory_get returns a non-empty list of records whose pc
     values are real C64 addresses (not zero), and decoded opcodes /
     register values pass simple sanity checks (PC stable across a
     monotonically-non-decreasing cycle counter).

This is a wrapper smoke test, not a defMON behaviour test -- it doesn't
care whether F1 produced sound, only whether the binmon machinery
faithfully echoes hits and history records.

Run:  python -m defmon_driver.smoke_checkpoint_cpuhistory
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

from .binmon import CHECK_EXEC, MEMSPACE_MAIN, BinMon
from .defmon import Defmon
from .vice_docker import DiskMount, ViceContainer

log = logging.getLogger("test-cp-cpuhistory")


def section(title: str) -> None:
    print()
    print("-" * 72)
    print("  " + title)
    print("-" * 72)


def test_checkpoint_lifecycle(bm: BinMon, d: Defmon) -> list[str]:
    """Add a wide exec checkpoint over the player range, fire F1, assert hit."""
    failures: list[str] = []

    # Wide watchpoint over a region we expect the player to touch when F1
    # fires. defMON loads at $1000+ per the wiki callingtheplayer page.
    cp = bm.checkpoint_set(
        start=0x1000,
        end=0xBFFF,
        op=CHECK_EXEC,
        stop_when_hit=False,
        enabled=True,
    )
    print(
        f"  set: checknum={cp.checknum} start=${cp.start:04x} end=${cp.end:04x} "
        f"op={cp.op:#x} enabled={cp.enabled} hit_count={cp.hit_count}"
    )

    if cp.start != 0x1000 or cp.end != 0xBFFF:
        failures.append(f"echo start/end wrong: {cp.start:04x}/{cp.end:04x}")
    if cp.op != CHECK_EXEC:
        failures.append(f"echo op wrong: {cp.op:#x}")
    if not cp.enabled:
        failures.append("checkpoint not enabled after set")
    if cp.hit_count != 0:
        failures.append(f"fresh checkpoint already had hits: {cp.hit_count}")

    # Fire F1. Even on the empty boot song, defMON's player IRQ will
    # execute through the player rangeas soon as we tap play.
    print("  firing F1 (play_from_cursor)...")
    d.play_from_cursor()
    time.sleep(0.3)  # let the warp emulator burn a few thousand cycles
    after = bm.checkpoint_get(cp.checknum)
    print(f"  after F1: hit_count={after.hit_count}")
    if after.hit_count == 0:
        failures.append(
            "checkpoint never fired after F1 — player range wrong, or checkpoint plumbing broken"
        )

    # Stop playback so we don't keep filling cpuhistory with player loop.
    d.stop_playback()
    time.sleep(0.1)

    # checkpoint_list should see this one.
    listed = bm.checkpoint_list()
    print(f"  list: {len(listed)} checkpoint(s); checknums={[c.checknum for c in listed]}")
    if not any(c.checknum == cp.checknum for c in listed):
        failures.append(f"our checknum {cp.checknum} missing from list")

    # Delete and re-list.
    bm.checkpoint_delete(cp.checknum)
    after_delete = bm.checkpoint_list()
    print(f"  after delete: {len(after_delete)} remaining")
    if any(c.checknum == cp.checknum for c in after_delete):
        failures.append("delete did not remove the checkpoint")

    return failures


def test_cpuhistory(bm: BinMon) -> list[str]:
    """Pull a small history slice and validate structure of decoded records."""
    failures: list[str] = []

    # 64 records is well below the response-size cap and big enough to
    # make order/cycle invariants meaningful.
    history = bm.cpuhistory_get(count=64, memspace=MEMSPACE_MAIN)
    print(f"  records returned: {len(history)}")
    if not history:
        failures.append("cpuhistory_get returned no records")
        return failures

    # Print first / last so we can eyeball them.
    for label, rec in (("first", history[0]), ("last", history[-1])):
        print(
            f"  {label}: pc=${rec.pc:04x} op=${rec.op:02x} p1=${rec.p1:02x} "
            f"p2=${rec.p2:02x} a=${rec.a:02x} x=${rec.x:02x} y=${rec.y:02x} "
            f"sp=${rec.sp:02x} flags=${rec.flags:02x} cycle={rec.cycle}"
        )

    # Sanity: PC must be plausible (>= $0100, since 6502 stack page has
    # no executable code in normal operation; really we expect KERNAL
    # / defMON range here).
    bad_pc = [r for r in history if r.pc < 0x100]
    if bad_pc:
        failures.append(f"{len(bad_pc)} records with implausibly low pc")

    # Sanity: cycle should be monotone non-decreasing across records.
    cycles = [r.cycle for r in history]
    decreases = [
        (i, cycles[i - 1], cycles[i]) for i in range(1, len(cycles)) if cycles[i] < cycles[i - 1]
    ]
    if decreases:
        failures.append(f"{len(decreases)} cycle decrease(s); first: {decreases[0]}")

    # All 6 standard regs (a/x/y/pc/sp/flags) should be present in
    # every record per mon_register6502.c REG_LIST_6510.
    required = {0, 1, 2, 3, 4, 5}
    missing = [
        (i, required - set(r.registers))
        for i, r in enumerate(history)
        if not required.issubset(r.registers)
    ]
    if missing:
        failures.append(f"{len(missing)} records missing core regs; first: {missing[0]}")

    return failures


def run(d64_path: Path, port: int) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="defmon-cpcheck-"))
    work_d64 = workdir / "disk.d64"
    shutil.copy2(d64_path, work_d64)

    container = ViceContainer(
        binmon_port=port,
        autostart="/work/disk.d64",
        mounts=[DiskMount(str(work_d64), "/work/disk.d64", read_only=False)],
    )

    failures: list[str] = []
    try:
        container.start()
        section(f"CONTAINER {container.container_id} on :{port}")

        bm = BinMon("127.0.0.1", port)
        bm.connect(timeout=10.0, attempts=80, retry_delay=0.25)
        bm.exit()

        d = Defmon(bm)
        section("BOOTING defMON")
        snap = d.wait_for_defmon_loaded(timeout=90.0)
        # Don't dump the whole boot screen here — keep output focused on
        # what we're actually testing. One header line is enough to prove
        # we got past wait_for_defmon_loaded.
        head = snap.lines()[0].strip() if snap.lines() else ""
        print(f"  boot header: {head!r}")

        section("CHECKPOINT lifecycle")
        failures += test_checkpoint_lifecycle(bm, d)

        section("CPUHISTORY decode")
        failures += test_cpuhistory(bm)

        bm.close()
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        failures.append(f"top-level: {e}")
    finally:
        container.stop()

    section("RESULT")
    if failures:
        print(f"  FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  PASS — checkpoint and cpuhistory wrappers verified live.")
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
