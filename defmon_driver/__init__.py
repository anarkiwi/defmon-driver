"""defmon-driver — defMON-specific automation layer on top of vice-driver.

The wire protocol, key matrix, screen scrape, container management,
coverage harness, and state-assertion helpers all live in
:mod:`vice_driver`. This package adds the defMON-specific surface:

  * :mod:`defmon_driver.defmon` — high-level method bindings for every
    documented defMON keyboard shortcut.
  * :mod:`defmon_driver.keyhandler` — direct-call keyboard injection
    (sets defMON's debounced-key register and invokes a mode handler
    stub).
  * :mod:`defmon_driver.field_setter` — high-level "set any UI field"
    API built on top of keyhandler.
  * :mod:`defmon_driver.sidtab` — sidTAB cell-level editing API driven
    by a calibration JSON.
  * :mod:`defmon_driver.calibrate_sidtab` — auto-discover the sidTAB
    column → screen-cell layout for a tune.
  * :mod:`defmon_driver.tune_manifest` / :mod:`defmon_driver.tune_navigation`
    — example-tune metadata and disk-menu cursor walks.
  * :mod:`defmon_driver.keycode_table` /
    :mod:`defmon_driver.bootstrap_keycodes` — internal-keycode LUT
    decoding and live bootstrap from a running defMON.

The shared wire/transport primitives (``BinMon``, ``KEY``,
``ScreenSnapshot``, ``ViceContainer``, ``Expect``, ``verify`` …) are
re-exported here for callers that prefer a single import surface, but
new code should import them directly from :mod:`vice_driver`.

See ``README.md`` for installation, container setup, and a worked
"connect → screen-grab → tap a chord" example.
"""

from vice_driver import (
    KEY,
    OPCODE,
    BinMon,
    BinmonError,
    Expect,
    ScreenSnapshot,
    ViceContainer,
    ViceContainerError,
    chord_to_keys,
    parse_screen_response,
    screencode_to_ascii,
    text_to_chords,
    verify,
)

from ._version import __version__
from .defmon import Defmon, DefmonError

__all__ = [
    "__version__",
    "BinMon",
    "BinmonError",
    "OPCODE",
    "KEY",
    "Expect",
    "verify",
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
