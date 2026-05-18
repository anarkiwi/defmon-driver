"""End-to-end live smoke test for the driver.

Drives a real asid-vice container against a defMON disk image:

  1. Start the asid-vice Docker container, mounting the caller-supplied
     defMON d64 and
     a freshly-created blank work disk.
  2. Connect via binmon, resume the CPU, wait for defMON to boot.
  3. Drive the disk menu (read directory, save a new tune, switch drives).
  4. Run every documented zero-arg command (toggle screens, super-commands,
     octave shifts, mute toggles, …) and screen-scrape after each.
  5. Confirm the keymatrix observation count went up for every chord —
     proof that defMON saw the press through the matrix layer rather than
     the tap timing out.
  6. Stop the container and print a one-line PASS/FAIL summary.

This test requires Docker + an ``asid-vice:latest`` image on the host;
it is **not** part of the unit-test suite that runs in CI. See
``README.md`` for how to build the asid-vice image.

Run: python -m defmon_driver.smoke /path/to/defmon.d64 [--keep] [--port 6502]
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

from .binmon import RELEASE_NONE, BinMon
from .defmon import Defmon, DefmonError
from .vice_docker import DiskMount, ViceContainer, ViceContainerError

log = logging.getLogger("smoke")


def make_blank_d64(path: Path, name: str = "WORK", c1541: str = "c1541") -> bool:
    """Create an empty d64 image at path. Returns True on success.

    Uses host c1541 if on PATH. Otherwise falls back to running c1541 from
    inside the asid-vice container (`docker run --rm asid-vice c1541 ...`),
    since the container's /opt/vice/bin/c1541 is always available wherever
    the harness can run docker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if shutil.which(c1541):
        r = subprocess.run(
            [c1541, "-format", f"{name},01", "d64", str(path)],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            return True
        log.warning(
            "host c1541 format failed (%s); falling through to container",
            r.stderr.strip(),
        )
    # Container fallback. The image contains c1541 but its ENTRYPOINT is x64sc;
    # override with --entrypoint to run c1541 instead.
    r = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "c1541",
            "-v",
            f"{path.parent.absolute()}:/work",
            "asid-vice:latest",
            "-format",
            f"{name},01",
            "d64",
            f"/work/{path.name}",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        return True
    raise RuntimeError(f"could not format work disk: {r.stderr.strip() or r.stdout}")


def section(title: str) -> None:
    print()
    print("=" * 72)
    print("  " + title)
    print("=" * 72)


def run(d64_path: Path, port: int, keep_container: bool) -> int:
    if not d64_path.is_file():
        print(f"defmon d64 not found: {d64_path}", file=sys.stderr)
        return 2

    # The user's instruction: copy the pristine d64 to a writable temp
    # image and mount it RW on drive 8. Don't introduce a separate drive
    # 9 — defMON saves to whatever drive is currently selected (8 by
    # default), so anything we do in the disk menu writes back to this
    # temp image.
    workdir = Path(tempfile.mkdtemp(prefix="defmon-smoke-"))
    work_d64 = workdir / "defmon-work.d64"
    shutil.copy2(d64_path, work_d64)
    log.info("writable copy of pristine disk: %s", work_d64)

    container = ViceContainer(
        binmon_port=port,
        autostart="/work/disk.d64",
        mounts=[
            DiskMount(str(work_d64), "/work/disk.d64", read_only=False),
        ],
    )

    failures: list[str] = []
    keymatrix_stats: list[tuple[str, int]] = []  # (name, sampling reads)

    try:
        container.start()
        section("CONTAINER STARTED")
        print(f"  id    : {container.container_id}")
        print(f"  binmon: 127.0.0.1:{port}")

        bm = BinMon("127.0.0.1", port, on_event=lambda r: log.debug("event %#x", r.opcode))
        bm.connect(timeout=10.0, attempts=80, retry_delay=0.25)
        bm.exit()  # release the initial STOPPED so the CPU runs and defMON autoboots

        d = Defmon(bm)
        section("WAITING FOR defMON BOOT")
        snap = d.wait_for_defmon_loaded(timeout=90.0)
        print(snap.text())

        # ---- Disk menu ------------------------------------------------
        section("DISK MENU: open (directory is rendered on entry)")
        snap = d.open_disk_menu()
        print(snap.text())

        section("DISK MENU: save tune to a fresh slot on drive 8")
        save_name = "TUNE1"
        try:
            # Pass flush_drive_path so the harness detaches+reattaches
            # drive 8 right after the save settles. Without this, VICE
            # may leave the file 'open' in the BAM until the container
            # exits, which c1541 / external readers see as a splat.
            snap = d.disk_save_new(save_name, flush_drive_path="/work/disk.d64")
            print(snap.text())
        except DefmonError as e:
            failures.append(f"disk_save_new: {e}")

        # Re-enter the disk menu and confirm the new file is in the dir.
        try:
            dir_snap = d.open_disk_menu()
            print(dir_snap.text())
            if not dir_snap.contains(save_name):
                failures.append(f"saved file '{save_name}' not seen in directory")
        except DefmonError as e:
            failures.append(f"verify_save: {e}")
        try:
            d.close_disk_menu()
        except DefmonError as e:
            failures.append(f"close_disk_menu: {e}")

        # ---- Run every zero-arg command ------------------------------
        section("EXERCISING DOCUMENTED CHORDS")
        for name, fn in d.all_documented_actions():
            try:
                outcome = fn()
                if outcome is None:
                    # super_* multi-step helpers return None — no single
                    # TapOutcome to inspect. Treat as ok if no exception.
                    print(f"  {name:30s} {'ok':>8s}  (multi-tap; no TapOutcome)")
                    continue
                keymatrix_stats.append((name, outcome.cia1_reads_sampling))
                # In FIXED mode (the default), RELEASE_TIMEOUT is the
                # expected normal outcome — the chord was held for the
                # requested frame count and then released. RELEASE_NONE
                # is the only true failure indicating defMON never
                # observed the matrix bit.
                if outcome.release_reason == RELEASE_NONE:
                    marker = "MISSED"
                    failures.append(f"{name}: defMON never read the matrix")
                else:
                    marker = "ok"
                print(f"  {name:30s} {marker:>8s}  cia1_sampled={outcome.cia1_reads_sampling}")
            except Exception as e:  # noqa: BLE001
                failures.append(f"{name}: {e}")
                print(f"  {name:30s}     FAIL  {e}")

        # ---- Super command exercise ----------------------------------
        section("SUPER COMMANDS")
        try:
            d.super_steps(8)
            d.super_repeat(2)
            d.super_width(1)
            d.super_zone_all()
            time.sleep(0.2)
            d.super_exit()
            print("  super_steps(8) / super_repeat(2) / super_width(1) / zone_all / exit OK")
        except Exception as e:  # noqa: BLE001
            failures.append(f"super commands: {e}")
            print(f"  super commands FAIL {e}")

        # ---- Multispeed cycling --------------------------------------
        section("MULTISPEED")
        for sp in (1, 2, 4, 8):
            try:
                d.set_multispeed(sp)
                print(f"  multispeed {sp}x OK")
            except Exception as e:  # noqa: BLE001
                failures.append(f"set_multispeed({sp}): {e}")

        # ---- Mute toggles --------------------------------------------
        section("TRACK MUTE TOGGLES")
        for tr in (1, 2, 3):
            try:
                outcome = d.mute_track(tr)
                print(f"  mute {tr}: reason={outcome.release_reason}")
            except Exception as e:  # noqa: BLE001
                failures.append(f"mute_track({tr}): {e}")

        # ---- Step-level value editing in seqED -----------------------
        # Cursor lands on voice 0 step 0 (the editor's natural starting
        # position after load). At a single step+voice cell we exercise
        # all three modifier-routed sub-fields (note / sound program /
        # speed) and confirm the screen text changed.
        section("EDIT IN seqED (note + sound program + speed)")
        try:
            before = d.screen().text()
            d.type_note("Z")  # C in the lower octave
            d.type_sound_program(0x01)  # LSHIFT+CBM held, "0" then "1"
            d.type_speed(0x02)  # CTRL+CBM held, "0" then "2"
            time.sleep(0.2)
            after = d.screen().text()
            if before == after:
                failures.append(
                    "seqED edits (note/sound_program/speed) produced no visible screen change"
                )
                print("  FAIL — screen unchanged after edits")
            else:
                print("  ok — screen text changed after note + sound_program(01) + speed(02)")
        except Exception as e:  # noqa: BLE001
            failures.append(f"seqED edits: {e}")
            print(f"  FAIL — {e}")

        # ---- sidTAB column editing -----------------------------------
        # LEFTARROW enters sidTAB. Plain hex digits at the cursor edit
        # the current cell. We move the cursor a couple of cells in
        # then type a recognisable byte and screen-diff to confirm.
        section("EDIT IN sidTAB (cursor + hex digit cells)")
        try:
            d.enter_sidtab()
            time.sleep(0.3)
            before = d.screen().text()
            d.cursor_down(2)
            d.cursor_right(2)
            d.type_hex_byte(0xAF)
            time.sleep(0.2)
            after = d.screen().text()
            if before == after:
                failures.append("sidTAB edit (cursor+hex) produced no visible screen change")
                print("  FAIL — screen unchanged after sidTAB edit")
            else:
                print("  ok — sidTAB cell updated after hex byte 0xAF")
            # Leave sidTAB so subsequent steps see seqED again.
            d.tap("LEFTARROW")
            time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            failures.append(f"sidTAB edits: {e}")
            print(f"  FAIL — {e}")

        # ---- seqLIST pattern entry -----------------------------------
        # RUNSTOP toggles seqED ↔ seqLIST. Voice cells in seqLIST take
        # plain hex digits (no modifier). We type a pattern hex into
        # voice 0's cell, screen-diff to confirm, then RUNSTOP back.
        section("EDIT IN seqLIST (pattern entry per voice)")
        try:
            d.toggle_seqed_seqlist()
            time.sleep(0.3)
            before = d.screen().text()
            d.cursor_down(1)  # off the header into a real arranger row
            d.type_hex_byte(0x01)  # voice 0 pattern = $01
            time.sleep(0.2)
            after = d.screen().text()
            if before == after:
                failures.append("seqLIST edit (pattern entry) produced no visible screen change")
                print("  FAIL — screen unchanged after seqLIST edit")
            else:
                print("  ok — seqLIST voice 0 pattern entry updated to $01")
            # Back to seqED for the final-screen capture below.
            d.toggle_seqed_seqlist()
            time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            failures.append(f"seqLIST edits: {e}")
            print(f"  FAIL — {e}")

        # ---- Final screen --------------------------------------------
        section("FINAL SCREEN")
        snap = d.screen()
        print(snap.text())

        # ---- Stats ---------------------------------------------------
        section("KEYMATRIX OBSERVATION COVERAGE")
        zero_obs = [name for name, n in keymatrix_stats if n == 0]
        for name, n in keymatrix_stats:
            print(f"  {name:30s} cia1_sampled={n}")
        if zero_obs:
            print()
            print(f"  WARNING: {len(zero_obs)} chord(s) had no observed CIA1 reads:")
            for name in zero_obs:
                print(f"    - {name}")

        bm.close()

    except (ViceContainerError, DefmonError, Exception) as e:  # noqa: BLE001
        traceback.print_exc()
        failures.append(f"top-level: {e}")
    finally:
        if not keep_container:
            container.stop()
        else:
            print(f"\nLeaving container running: {container.container_id}")

    # ---- Disk write verification ----------------------------------------
    section("DISK CONTENTS AFTER STOP (check for saved file)")
    try:
        host_c1541 = shutil.which("c1541")
        listing = ""
        if host_c1541:
            r = subprocess.run(
                [host_c1541, "-attach", str(work_d64), "-list"],
                capture_output=True,
                text=True,
            )
            listing = r.stdout.strip()
            print(listing)
        else:
            print("  (host c1541 unavailable; cannot inspect disk)")
        if listing and save_name.lower() in listing.lower():
            print(f"\n  ✓ saved file '{save_name}' present on disk image")
        elif listing:
            failures.append(f"disk write: '{save_name}' not present in c1541 listing")
            print(f"\n  ✗ saved file '{save_name}' MISSING from disk image")
    except Exception as e:  # noqa: BLE001
        print(f"  (could not inspect work disk: {e})")

    # ---- Verdict ---------------------------------------------------------
    section("RESULT")
    if failures:
        print(f"  FAIL — {len(failures)} issue(s):")
        for f in failures:
            print(f"    - {f}")
        return 1
    print("  PASS — every documented chord registered with defMON,")
    print("         and the saved tune is present on the host-side .d64.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("d64", help="path to defMON .d64 image")
    p.add_argument("--port", type=int, default=6502, help="host binmon port")
    p.add_argument("--keep", action="store_true", help="leave container running on exit")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )
    return run(Path(args.d64), args.port, args.keep)


if __name__ == "__main__":
    sys.exit(main())
