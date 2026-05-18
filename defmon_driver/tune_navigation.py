"""Helpers for loading example tunes from a defMON-formatted disk image.

defMON's example tunes ship with names that start with ``.`` and often
exceed the 15-character limit the typed-name save/load path imposes, so
the only reliable way to load them is to walk the disk-menu cursor down
to the right directory row and hit RETURN. This module wraps that walk
in :func:`cursor_load_tune`, plus a small :func:`state_reset` quiescer
for callers that want to run a sequence of disparate actions back-to-back.

Inputs:
  - :class:`defmon_driver.defmon.Defmon` — already booted past the splash.
  - :class:`defmon_driver.tune_manifest.TuneEntry` — provides ``dir_index``.

Outputs:
  - The tune is loaded into seqED. The function returns once one of the
    voice-header markers (``VOC0`` / ``VOC1`` / ``VOC2``) appears on
    screen, so the caller does not race the load.
"""

from __future__ import annotations

import logging
import time

from .defmon import Defmon, DefmonError
from .tune_manifest import TuneEntry

log = logging.getLogger(__name__)


def state_reset(d: Defmon) -> None:
    """Between-action sanitiser. Does not try to restore a specific mode —
    leaves any super-command session and stops playback so the next
    action sees a quiescent player."""
    try:
        d.tap("CTRL", "RETURN")  # exit super-command; harmless if not in one
    except Exception:  # noqa: BLE001
        pass
    try:
        d.tap("F7")  # stop playback; harmless if already stopped
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.05)


def cursor_load_tune(
    d: Defmon, tune: TuneEntry, max_attempts: int = 3, load_timeout: float = 12.0
) -> None:
    """Open the disk menu, walk to the tune's row, hit RETURN to load.

    After RETURN, polls the screen for seqED voice-header markers
    (VOC0/VOC1/VOC2) so this function does not return until defMON is
    actually in seqED with the tune loaded. ``disk_load_by_index``'s
    screen-stability wait can return early during a brief loading-
    screen quiesce, leaving the caller racing the load — verifying VOC*
    before returning closes that gap.

    Retries the whole sequence up to ``max_attempts`` if seqED markers
    don't appear within ``load_timeout``.
    """
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            d.open_disk_menu()
            time.sleep(0.2)
            for _ in range(tune.dir_index):
                d.tap("CRSRUD")
                time.sleep(0.04)
            d.disk_load_by_index()
        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("[%s] cursor_load_tune attempt %d: %s", tune.name, attempt, e)
            time.sleep(0.5)
            continue

        deadline = time.monotonic() + load_timeout
        while time.monotonic() < deadline:
            try:
                snap = d.screen()
            except Exception:  # noqa: BLE001
                time.sleep(0.2)
                continue
            text = snap.text()
            if any(m in text for m in ("VOC0", "VOC1", "VOC2")):
                return
            time.sleep(0.2)
        last_err = DefmonError(
            f"after RETURN, seqED markers (VOC*) not visible within "
            f"{load_timeout}s — load did not complete"
        )
        log.warning(
            "[%s] cursor_load_tune attempt %d: %s", tune.name, attempt, last_err
        )

    raise DefmonError(
        f"cursor_load_tune({tune.name!r}): {max_attempts} attempts "
        f"failed; last error: {last_err}"
    )
