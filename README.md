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

The driver itself is pure-Python with **no runtime dependencies** beyond the
standard library. It speaks the asid-vice binary monitor protocol over a
single TCP socket per connection.

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
from defmon_driver import BinMon, Defmon, DiskMount, ViceContainer

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

For higher-level workflows there are dedicated sub-modules:

| Module | Purpose |
| --- | --- |
| `defmon_driver.binmon` | Binary-monitor wire client + checkpoint / cpuhistory wrappers |
| `defmon_driver.defmon` | Every documented defMON keyboard shortcut as a Python method |
| `defmon_driver.keys` | C64 key-matrix names + ASCII → chord conversion |
| `defmon_driver.screen` | SCREEN_GET parsing, screencode → ASCII, `find_text()` |
| `defmon_driver.vice_docker` | One-shot Docker container management |
| `defmon_driver.keyhandler` | Direct-call keyboard injection (bypasses matrix scan / debounce) |
| `defmon_driver.field_setter` | High-level "set any UI field" API on top of keyhandler |
| `defmon_driver.sidtab` | sidTAB cell-level editing API driven by a calibration JSON |
| `defmon_driver.coverage` | Per-action code-coverage harness using CHECK_EXEC + cpuhistory |
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
```

## Known limitations

See [`BUGS.md`](BUGS.md) for outstanding issues and
[`FUTURE.md`](FUTURE.md) for features that another contributor could pick
up.

## License

Apache 2.0 — see [`LICENSE`](LICENSE).
