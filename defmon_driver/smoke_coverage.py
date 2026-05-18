"""Live smoke for :mod:`defmon_driver.coverage`.

Boots defMON in asid-vice, installs a Coverage harness over the player
band, and validates per-action attribution:

  - install drops 176 page checkpoints over $1000-$BFFF
  - play_from_cursor produces non-zero hits AND at least one page in the
    $80-$9F player-loop band lights up
  - stop_playback also produces hits
  - idle baseline returns coherent numbers (sanity, not a hard threshold)
  - remove() leaves no checkpoints owned by this harness behind

This is a wrapper smoke, not a behaviour test — we don't care about
sound, only that page deltas + cpuhistory PCs come back with plausible
shape.

Run:  python -m defmon_driver.smoke_coverage
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

from .binmon import BinMon
from .coverage import ActionCoverage, Coverage
from .defmon import Defmon
from .vice_docker import DiskMount, ViceContainer

log = logging.getLogger("test-coverage")

EXPECTED_PAGE_COUNT = 0xC0 - 0x10  # 176 pages over $1000-$BFFF
PLAYER_BAND = range(0x80, 0xA0)  # player loop is in $8xxx-$9xxx


def section(title: str) -> None:
    print()
    print("-" * 72)
    print("  " + title)
    print("-" * 72)


def fmt_action(ac: ActionCoverage) -> str:
    top_pages = sorted(ac.page_hits.items(), key=lambda kv: kv[1], reverse=True)[:6]
    pages = ", ".join(f"${p:02x}xx={n}" for p, n in top_pages)
    return (
        f"{ac.name:24s} hits={ac.total_hits:>10d}  "
        f"pages={len(ac.page_hits):>3d}  "
        f"exec_pcs={len(ac.executed_pcs):>5d}  "
        f"hist_pcs={len(ac.cpuhistory_pcs):>4d}  "
        f"cycles={ac.cycles_elapsed:>10d}\n"
        f"      hottest: {pages}"
    )


def run(d64_path: Path, port: int) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="defmon-cov-"))
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
        section("BOOT")
        snap = d.wait_for_defmon_loaded(timeout=90.0)
        head = snap.lines()[0].strip() if snap.lines() else ""
        print(f"  boot header: {head!r}")

        section("INSTALL coverage")
        cov = Coverage(bm)
        # Check that we don't trample existing checkpoints (e.g. from a
        # leaked previous run on a re-used container).
        pre = bm.checkpoint_list()
        if pre:
            print(f"  warning: {len(pre)} checkpoint(s) present before install")
        cov.install()
        try:
            print(
                f"  installed {cov.checkpoint_count} checkpoints "
                f"({cov.granularity}-granular) covering {cov.page_count} "
                f"pages over ${cov.start:04x}-${cov.end:04x}"
            )
            if cov.page_count != EXPECTED_PAGE_COUNT:
                failures.append(f"expected {EXPECTED_PAGE_COUNT} pages, got {cov.page_count}")

            section("MEASURE: idle baseline")
            ac_idle = cov.measure_idle(duration=0.5)
            print("  " + fmt_action(ac_idle))

            section("MEASURE: play_from_cursor")
            ac_play = cov.measure(d.play_from_cursor, "play_from_cursor", settle=0.6)
            print("  " + fmt_action(ac_play))
            if ac_play.total_hits == 0:
                failures.append(
                    "play_from_cursor produced zero hits — player not "
                    "running, or page checkpoints not firing"
                )
            player_pages = [p for p in ac_play.page_hits if p in PLAYER_BAND]
            if not player_pages:
                failures.append(
                    "play_from_cursor: no hits in player band $80-$9F — "
                    "page attribution wrong, or player band assumption stale"
                )
            else:
                hottest_player = max(player_pages, key=lambda p: ac_play.page_hits[p])
                print(
                    f"  player band: {len(player_pages)} pages active; "
                    f"hottest ${hottest_player:02x}xx="
                    f"{ac_play.page_hits[hottest_player]}"
                )

            section("MEASURE: stop_playback")
            ac_stop = cov.measure(d.stop_playback, "stop_playback", settle=0.4)
            print("  " + fmt_action(ac_stop))
            if ac_stop.total_hits == 0:
                failures.append("stop_playback produced zero hits")

            section("MEASURE: aggregate")
            from .coverage import aggregate, union_pcs

            agg = aggregate([ac_idle, ac_play, ac_stop])
            pcs_union = union_pcs([ac_idle, ac_play, ac_stop])
            print(
                f"  combined: {len(agg)} pages touched, "
                f"{sum(agg.values())} total hits, "
                f"{len(pcs_union)} distinct PCs in cpuhistory"
            )
        finally:
            cov.remove()

        # No checkpoints owned by us should remain. Anything else (e.g.
        # leaked from a prior run on a re-used container) is the caller's
        # problem, not ours — but we shouldn't have added to it.
        remaining = bm.checkpoint_list()
        print(f"  after remove(): {len(remaining)} checkpoint(s) live ({len(pre)} pre-existing)")
        if len(remaining) > len(pre):
            failures.append(f"leak: {len(remaining)} > {len(pre)} pre-existing")

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
    print("  PASS — coverage harness validated live.")
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
