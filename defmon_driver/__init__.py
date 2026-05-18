"""defmon-driver — Python automation framework for driving defMON in asid-vice.

The public surface mirrors the layered design of the package:

  * :mod:`defmon_driver.binmon` — wire-level binary-monitor client (with the
    asid-vice keymatrix/screenscrape extensions and CPU-history support).
  * :mod:`defmon_driver.keys` — symbolic C64 key-matrix names and ASCII →
    chord conversion.
  * :mod:`defmon_driver.screen` — SCREEN_GET response parsing + screencode
    → ASCII rendering.
  * :mod:`defmon_driver.defmon` — high-level method bindings for every
    documented defMON keyboard shortcut.
  * :mod:`defmon_driver.vice_docker` — convenience wrapper around the
    asid-vice Docker image so a script can spin up an x64sc + binmon
    instance with one ``with`` block.
  * :mod:`defmon_driver.keyhandler` — direct-call keyboard injection (sets
    defMON's debounced-key register and invokes a mode handler stub).
  * :mod:`defmon_driver.field_setter` — high-level "set any UI field" API
    built on top of keyhandler.
  * :mod:`defmon_driver.sidtab` — high-level sidTAB editing API driven by
    the calibration JSON produced by :mod:`defmon_driver.calibrate_sidtab`.
  * :mod:`defmon_driver.coverage` — per-action code-coverage harness using
    CHECK_EXEC watchpoints + cpuhistory.

See ``README.md`` for installation, container setup, and a worked
"connect → screen-grab → tap a chord" example.
"""

from .binmon import OPCODE, BinMon, BinmonError
from .defmon import Defmon, DefmonError
from .keys import KEY, chord_to_keys, text_to_chords
from .screen import ScreenSnapshot, parse_screen_response, screencode_to_ascii
from .vice_docker import ViceContainer, ViceContainerError

__all__ = [
    "BinMon",
    "BinmonError",
    "OPCODE",
    "KEY",
    "text_to_chords",
    "chord_to_keys",
    "ScreenSnapshot",
    "parse_screen_response",
    "screencode_to_ascii",
    "Defmon",
    "DefmonError",
    "ViceContainer",
    "ViceContainerError",
]

__version__ = "0.1.0"
