# defmon-driver

[![CI](https://github.com/anarkiwi/defmon-driver/actions/workflows/ci.yml/badge.svg)](https://github.com/anarkiwi/defmon-driver/actions/workflows/ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

`defmon-driver` is a Python automation framework for driving
[defMON](https://csdb.dk/release/?id=190380) — the Commodore 64 cross-platform
music editor by Ilkke / defcom — running inside
[asid-vice](https://github.com/anarkiwi/asid-vice), an extension of the VICE
C64 emulator that exposes a binary monitor with key-matrix tap and screen-
scrape opcodes.

It is designed for the case where you want to programmatically:

* boot defMON in an emulator,
* load a tune from disk,
* press keys (chords, super-commands, modal switches),
* read the screen back as text or screencode bytes,
* edit pattern / sidTAB / seqLIST fields,
* and snapshot CPU state, RAM, or per-instruction code coverage.

The driver's defMON-specific layer is pure-Python and depends only on
[`vice-driver`](https://pypi.org/project/vice-driver/) (which itself has
no runtime dependencies beyond the standard library). `vice-driver`
provides the binary-monitor wire client, key matrix, screen scrape,
container management, coverage harness, and state-assertion helpers;
`defmon-driver` provides everything specific to defMON (mode handlers,
field setters, sidTAB calibration, tune navigation, etc.).

> defMON is a property of its authors; this project is an independent
> automation harness and is **not affiliated with** the defMON project.

## Requirements

* Python ≥ 3.10
* Docker, with a built `asid-vice:latest` image — see
  [`anarkiwi/asid-vice`](https://github.com/anarkiwi/asid-vice) for the
  Dockerfile and build instructions.
* A defMON-formatted `.d64` disk image (e.g. `defmon-20201008.d64` or
  `defmon-withtunes.d64`, both available from
  [csdb.dk](https://csdb.dk/release/?id=190380)).

The package itself does not bundle defMON or any disk image — the user
provides their own copy in line with the original distribution terms.

## Installation

```sh
pip install defmon-driver
```

Or, for development:

```sh
git clone https://github.com/anarkiwi/defmon-driver
cd defmon-driver
pip install -e ".[dev]"
pytest tests/unit
```

## Quick start

```python
import logging
from vice_driver import BinMon, DiskMount, ViceContainer
from defmon_driver import Defmon

logging.basicConfig(level=logging.INFO)

# Spin up an asid-vice container that autostarts the defMON disk image.
container = ViceContainer(
    autostart="/work/defmon.d64",
    mounts=[DiskMount("/host/path/to/defmon.d64", "/work/defmon.d64", read_only=True)],
)

with container:
    bm = BinMon("127.0.0.1", 6502)
    bm.connect(timeout=10.0, attempts=80, retry_delay=0.25)
    # Drain the initial halt and resume the CPU.
    bm.exit()

    d = Defmon(bm)
    d.wait_for_defmon_loaded(timeout=90.0)

    # Press F1 to start playback from cursor.
    d.play_from_cursor()
    # Scrape the screen back as 25 lines of ASCII.
    print(d.screen().text())
    d.stop_playback()

    bm.close()
```

For higher-level workflows there are dedicated sub-modules. The
transport / emulator-control layer lives in
[`vice-driver`](https://pypi.org/project/vice-driver/); everything below
is defMON-specific.

| Module | Purpose |
| --- | --- |
| `vice_driver.binmon` | Binary-monitor wire client + checkpoint / cpuhistory wrappers |
| `vice_driver.keys` | C64 key-matrix names + ASCII → chord conversion |
| `vice_driver.screen` | SCREEN_GET parsing, screencode → ASCII, `find_text()` |
| `vice_driver.vice_docker` | One-shot Docker container management |
| `vice_driver.coverage` | Per-action code-coverage harness using CHECK_EXEC + cpuhistory |
| `vice_driver.expect` | `Expect` predicate + `verify` polling helper for post-action state assertions |
| `defmon_driver.defmon` | Every documented defMON keyboard shortcut as a Python method |
| `defmon_driver.keyhandler` | Direct-call keyboard injection (bypasses matrix scan / debounce) |
| `defmon_driver.field_setter` | High-level "set any UI field" API on top of keyhandler |
| `defmon_driver.sidtab` | sidTAB cell-level editing API driven by a calibration JSON |
| `defmon_driver.keycode_table` | name → ``$0E44`` keycode resolver built from defMON's ``$0F90`` LUT |
| `defmon_driver.calibrate_sidtab` | Auto-discover the sidTAB column → screen layout for a tune |
| `defmon_driver.tune_manifest` | Static manifest of example tunes shipped with the defMON d64s |
| `defmon_driver.tune_navigation` | Cursor-walk a tune off the disk menu by `dir_index` |

## CLI smoke tests

Several modules expose a `python -m …` entry point that drives a real
asid-vice container end-to-end. They are not part of the CI unit-test
suite (CI is offline-only); they exist for manual validation against a
local Docker setup:

```sh
# End-to-end driver smoke: every documented chord + disk-menu round-trip.
python -m defmon_driver.smoke /path/to/defmon-20201008.d64

# Coverage harness smoke: install 176 page checkpoints, fire F1, verify the
# $80-$9F player band lights up.
python -m defmon_driver.smoke_coverage /path/to/defmon-20201008.d64

# Checkpoint + cpuhistory smoke.
python -m defmon_driver.smoke_checkpoint_cpuhistory /path/to/defmon-20201008.d64

# sidTAB calibration: discover the column → screen-cell mapping for a tune.
python -m defmon_driver.calibrate_sidtab \
    --d64 /path/to/defmon-withtunes.d64 \
    --tune ".GLOW WORM" \
    --out sidtab_calibration.json

# sidTAB driving smoke: load .GLOW WORM, write JP/AD/SR/ACID cells.
python -m defmon_driver.smoke_sidtab \
    --d64 /path/to/defmon-withtunes.d64 \
    --tune ".GLOW WORM" \
    --cal sidtab_calibration.json

# Chord-driven note write smoke: write_note_chord for Z..M, verify the
# expected note byte ($30..$3B) actually lands in pattern memory.
python -m defmon_driver.smoke_note_chord /path/to/defmon-20201008.d64

# Chord-driven sidcall1 smoke: cross-product V0/V1/V2 × {step 0,3,7} ×
# {0x00, 0x57, 0xAB, 0xFF}, verify each chord-driven write lands at the
# runtime cell address (catches the BUGS.md #2 regression class).
python -m defmon_driver.smoke_sidcall /path/to/defmon-20201008.d64
```

## Known limitations

See [`BUGS.md`](BUGS.md) for outstanding issues,
[`FUTURE.md`](FUTURE.md) for features that another contributor could pick
up, and [`CHANGELOG.md`](CHANGELOG.md) for behavioural changes —
including any breaking renames or fixes whose impact downstream callers
should review.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
