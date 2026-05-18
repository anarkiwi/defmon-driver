# Future work / wish list

Features that another contributor could meaningfully progress without
needing additional reverse-engineering of defMON itself. Each entry
sketches the goal, the rough surface area, and one or two implementation
hints.

## Pytest plugin / fixture for a shared VICE container

**Goal:** Let library users write their own pytest suites that spin up
an asid-vice container once per session and reuse a single boot across
many test functions.

**Sketch:** A `pytest-defmon-driver` companion package (or an
`[project.entry-points."pytest11"]` block in this repo) that exposes a
`vice_container` session-scoped fixture and a `defmon` function-scoped
fixture that returns a `Defmon` instance with each test's state reset
via `tune_navigation.state_reset`.

**Where to start:** `defmon_driver.vice_docker.ViceContainer` already
implements `__enter__` / `__exit__` cleanly. A session-scoped fixture
just needs to wrap that plus the `BinMon.connect` retry loop from
`smoke.py`.

## Asynchronous `BinMon` variant

**Goal:** A non-blocking `asyncio` client for callers that want to drive
multiple containers in parallel (e.g. running the action sweep across
all example tunes concurrently).

**Sketch:** Replace `socket.socket` + the synchronous `call()` loop in
`binmon.py` with `asyncio.StreamReader` / `StreamWriter`. The request-
id matching logic is unchanged — only the I/O substrate moves to
coroutines. Keep the synchronous `BinMon` API intact and add
`AsyncBinMon` alongside it.

**Where to start:** `BinMon._read_response` is the only place that
blocks on the socket. Re-implement it as an async coroutine and the
rest is wrapping.

## Headless audio capture → WAV

**Goal:** A `Defmon.record_audio(path, duration)` helper that plays the
loaded tune via the player IRQ for `duration` seconds and saves a WAV.

**Sketch:** `ViceContainer` already supports
`sounddev="dump"` + `sounddump_path` — that produces a per-SID-write
record. A post-processing module (`defmon_driver.sound_dump`) can
replay the dump into a SID register history → WAV via something like
[`pysid`](https://github.com/sidplay2-fork/sidplay) or a vendored
`sidplay` shell-out.

**Where to start:** `ViceContainer.x64sc_args` writes the dump path
through to VICE. The missing piece is the dump → WAV converter.

## `Defmon` async event stream

**Goal:** Expose unsolicited STOPPED / RESUMED / JAM events as an
`AsyncIterator` so callers can react to a JAM (CPU lockup) immediately
instead of polling.

**Sketch:** `BinMon` already de-multiplexes unsolicited events via the
`on_event` callback; route them into an `asyncio.Queue` that a public
`async for event in d.events():` consumes.

**Where to start:** `BinMon._drain_unsolicited` is the central choke
point.

## Stereo SID #3 chord helpers

**Goal:** First-class shortcuts for the third SID chip — `Defmon`
currently exposes `toggle_stereo` and `switch_sid_chip` but not the
3-SID equivalent. `ViceContainer` already takes `sid_extras=2` plus a
`sid3_address`; the driver-level shortcut for cycling chips through 3
positions is missing.

**Sketch:** Add `Defmon.set_sid_view(chip_index: int)` that issues the
right sequence of `switch_sid_chip` taps to reach chip 0/1/2 given the
current `$7171` state byte.

## Pure-Python `c1541` blank-d64 generator

**Goal:** Drop the host-`c1541` / container-`c1541` fallback in
`smoke.make_blank_d64` and use a pure-Python d64 writer instead.

**Sketch:** A 1541 disk image is a sparse 174,848-byte file with a
known BAM / directory header structure. There are reference
implementations in pure Python (e.g.
[`d64`](https://pypi.org/project/d64/)). Adding an optional
`d64`-package dependency and using it when available would eliminate
one external-tool requirement.

## Tune-state diff utility

**Goal:** Take two defMON saves of the same tune and produce a
human-readable diff: which pattern cells, sidTAB rows, and arranger
positions changed.

**Sketch:** Parse the PRG header + the known RAM layout offsets that
`field_setter.py` documents, then run a byte-diff with field-level
labelling. Lives most naturally under a new
`defmon_driver.tune_diff` module.

**Where to start:** `field_setter.ADDR_PAT_BASE_LO` /
`ADDR_PAT_BASE_HI` + `PATTERN_BASE` already encode the pattern-data
layout.

## Auto-screenshot on test failure

**Goal:** When a pytest assertion fails inside a test that holds a live
`Defmon`, automatically dump the current screen as ASCII into the test
report.

**Sketch:** A small pytest hook (`pytest_runtest_makereport`) that
introspects the test's local namespace, finds any `Defmon` instance,
and calls `.screen().text()` if the test failed.

**Where to start:** Implement once the pytest plugin (above) exists.

## CLI: `defmon-driver tap "<chord>"`

**Goal:** A `defmon-driver` console-script for one-shot interactive use
— "tap LSHIFT+X", "screen", "load tune .GLOW WORM" — so casual users
can drive a running container without writing Python.

**Sketch:** A `[project.scripts]` entry pointing at a new
`defmon_driver.cli` module. Each subcommand maps to a `Defmon` method;
the screen subcommand pretty-prints the ASCII render.

**Where to start:** `Defmon.all_documented_actions()` already gives a
canonical list of method names → callables, perfect for argparse
subcommands.

## `Defmon.set_tape_position` (PRG / TAP autostart)

**Goal:** First-class support for booting defMON from a PRG or TAP
file, not only a D64. `ViceContainer` accepts a path via `autostart`
but the user has to know that PRG vs D64 vs TAP all just work as
long as the file is mounted into the container.

**Sketch:** A factory `ViceContainer.from_tune(path)` that picks the
right mount + autostart args based on file extension and exposes a
typed `media_kind: Literal["d64", "prg", "tap"]` attribute.
