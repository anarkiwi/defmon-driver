"""Shared lifecycle scaffolding for the ``python -m defmon_driver.smoke_*``
entry points.

Every smoke / bootstrap / calibration CLI in this package follows the
same boot pattern: copy a defMON ``.d64`` into a temp workdir, start an
asid-vice container, attach a ``BinMon``, resume the CPU, wait for the
seqED splash, run the per-smoke body, then tear everything down. The
:func:`smoke_session` context manager handles the lifecycle so each
entry point only contains its actual probe logic.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from vice_driver.binmon import BinMon
from vice_driver.screen import ScreenSnapshot
from vice_driver.vice_docker import DiskMount, ViceContainer

from .defmon import Defmon

log = logging.getLogger(__name__)


@dataclass
class SmokeSession:
    """Live handles yielded by :func:`smoke_session`."""

    bm: BinMon
    d: Defmon
    container: ViceContainer
    workdir: Path
    boot_snapshot: ScreenSnapshot


def section(title: str, *, char: str = "-", width: int = 72) -> None:
    """Print a banner-style section header. Shared by every smoke."""
    print()
    print(char * width)
    print("  " + title)
    print(char * width)


@contextmanager
def smoke_session(
    d64_path: Path,
    *,
    port: int,
    prefix: str,
    connect_timeout: float = 10.0,
    connect_attempts: int = 80,
    retry_delay: float = 0.25,
    wait_timeout: float = 90.0,
    cleanup_workdir: bool = True,
    stop_container: bool = True,
    container_kwargs: Optional[dict] = None,
) -> Iterator[SmokeSession]:
    """Spin up a one-shot asid-vice container booted to defMON's seqED splash.

    The defMON disk is copied to a temp workdir so the container can
    mount it read-write without scribbling on the caller's image.
    Yields a :class:`SmokeSession` with the live ``BinMon`` / ``Defmon`` /
    ``ViceContainer`` / workdir / first-boot ``ScreenSnapshot``. On
    exit, closes the socket, stops the container, and (by default)
    removes the workdir.

    ``container_kwargs`` is merged into the :class:`ViceContainer`
    constructor for smokes that need extra knobs (e.g. ``warp=False``).
    """
    workdir = Path(tempfile.mkdtemp(prefix=prefix))
    work_d64 = workdir / "disk.d64"
    shutil.copy2(d64_path, work_d64)
    log.info("writable copy of pristine disk: %s", work_d64)

    container = ViceContainer(
        binmon_port=port,
        autostart="/work/disk.d64",
        mounts=[DiskMount(str(work_d64), "/work/disk.d64", read_only=False)],
        **(container_kwargs or {}),
    )

    bm = BinMon("127.0.0.1", port)
    try:
        container.start()
        bm.connect(
            timeout=connect_timeout,
            attempts=connect_attempts,
            retry_delay=retry_delay,
        )
        # Drain the initial STOPPED so the CPU runs and defMON autoboots.
        bm.exit()
        d = Defmon(bm)
        snap = d.wait_for_defmon_loaded(timeout=wait_timeout)
        yield SmokeSession(
            bm=bm,
            d=d,
            container=container,
            workdir=workdir,
            boot_snapshot=snap,
        )
    finally:
        try:
            bm.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            log.debug("bm.close() raised during teardown", exc_info=True)
        if stop_container:
            try:
                container.stop()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                log.debug("container.stop() raised during teardown", exc_info=True)
        else:
            log.info("leaving container running: %s", container.container_id)
        if cleanup_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
