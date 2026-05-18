# Known bugs and rough edges

A non-exhaustive list of issues that future contributors can pick up.
Each entry tries to capture the symptom, the smallest reproduction, and
where in the codebase to start looking.

## Multi-modifier chord taps intermittently dropped

**Where:** `defmon_driver.defmon._cycle_until`, callers of
`Defmon.toggle_stereo` / `Defmon.switch_sid_chip` / `Defmon.cycle_sid_high_byte`.

**Symptom:** Back-to-back CTRL+CBM+* or CBM+SHIFT+* chords occasionally
fail to register a state-byte change — the matrix tap is observed but
defMON's chord handler does not run. The current code defends against
this with a retry loop (`STEREO_CHORD_RETRIES`) plus a CTRL+RETURN/F7
quiesce before each tap.

**What's missing:** A reliable root cause. The retries hide the
behaviour but do not prevent the lost tap. A diagnostic that captures
defMON's debounce / modifier-flag bytes at the moment a chord is
silently dropped would be valuable.

## seqED V0 / V2 sidCALL1 (`CBM+SHIFT+digit`) is unstable

**Where:** `defmon_driver.field_setter.set_field(sub_field="sidcall1")`.

**Symptom:** The CBM+SHIFT+digit chord lands the digit reliably on
voice 1 but is flaky on voices 0 and 2. The docstring at the top of
`field_setter.py` notes this; the chord-driven path silently mis-writes
on those voices.

**What's missing:** A direct-RAM fallback specifically for `sidcall1`
across V0 / V2, or a writer-routine inspection that explains the
asymmetry. The arranger writer at `$B396` is the first place to look.

## Byte-granularity coverage installs slowly

**Where:** `defmon_driver.coverage.Coverage(granularity="byte")`.

**Symptom:** Installing 45,000+ per-byte CHECK_EXEC checkpoints across
the player / dispatcher / sidTAB / disk-menu / encoder bands takes ~11
seconds under `bm.halted()` — but the matching `remove()` is *15×*
slower (~150-200 ms per delete versus ~0.25 ms per install). On a full
sweep across all example tunes the teardown is the dominant cost.

**Mitigation:** The current code accepts a `drop_only=True` flag on
`remove()` that skips the VICE round-trips and only clears the Python-
side checknum map. Callers that immediately tear the container down
should pass it.

**What's missing:** A patch to asid-vice's checkpoint-delete fast path
(symmetric with the insert path) so `drop_only` is no longer needed.

## `silent=True` checkpoints still throttle warp emulation

**Where:** `defmon_driver.binmon.BinMon.checkpoint_set(silent=True)`.

**Symptom:** Even with the `silent` flag set, installing many thousands
of watchpoints adds noticeable per-instruction overhead to warp
emulation — at ~6000 silent watchpoints, VICE's warp playback wedges
behind a backlog of internal events.

**Workaround:** Install large watchpoint sets *before* booting defMON
(during the initial halt), not after, so the warp-mode IRQ stream isn't
running through them.

**What's missing:** A VICE-side change that truly eliminates the per-
instruction comparison cost when every watchpoint is silent.

## `Defmon.wait_for_defmon_loaded` is timing-sensitive

**Where:** `defmon_driver.defmon.Defmon.wait_for_defmon_loaded`.

**Symptom:** Under heavy host load, the boot screen scrape can race
defMON's splash → seqED transition and return early. Callers that
immediately tap a chord can see it fire against the still-running
splash code.

**Mitigation:** `tune_navigation.cursor_load_tune` polls for `VOC0` /
`VOC1` / `VOC2` markers and retries — use it instead of opening the
disk menu directly after `wait_for_defmon_loaded`.

**What's missing:** A more robust boot-complete detector (e.g. polling
the mode byte `$7167` for seqED ($01) directly).

## Non-warp tap timing is untested

**Where:** `defmon_driver.defmon.Defmon.DEFAULT_TAP_FRAMES = 12`.

**Symptom:** The 12-frame fixed tap is calibrated for warp-mode
playback (`-warp` is set by default in `ViceContainer`). Running with
`warp=False` may produce flaky chord recognition — the constant has
not been re-tuned for real-time playback.

**What's missing:** A small calibration script that bisects
`DEFAULT_TAP_FRAMES` against `cia1_reads_sampling` on the non-warp
path, and either a `Defmon.set_realtime()` helper or auto-detection
based on the container's `warp` flag.

## `text_to_chords` covers only the basic ASCII subset

**Where:** `defmon_driver.keys.text_to_chords`.

**Symptom:** Characters with no single-key (optionally with shift) matrix
path — e.g. backtick, vertical bar, curly braces — raise `KeyError`.
defMON file-name input is restricted to PETSCII so this is rarely a
practical limit, but it's a sharp edge.

**What's missing:** A clearer error message that lists the characters
that *are* supported, or an option to substitute `?` for unmappable
characters.

## Coverage page attribution misses sub-page granularity

**Where:** `defmon_driver.coverage.Coverage(granularity="page")`.

**Symptom:** Page-granular coverage gives one hit per 256-byte page,
which is enough to identify hot bands but not enough to pin down which
specific routine inside a page was hit. The cpuhistory drain partially
compensates but is lossy under the warp-mode player IRQ.

**Workaround:** Pass `granularity="byte"` (slower, see above) when
you need per-PC resolution.

**What's missing:** A hybrid mode that starts at page-granularity and
adaptively subdivides pages whose hit count crosses a threshold.

## The sidTAB calibration JSON path is implicit in many smokes

**Where:** `defmon_driver.smoke_sidtab`,
`defmon_driver.sidtab.SidTab.from_calibration`.

**Symptom:** The smoke defaults to `sidtab_calibration.json` in the
working directory. If a caller forgets to first run
`calibrate_sidtab`, the smoke fails with a `FileNotFoundError` rather
than a helpful "run the calibration step first" message.

**What's missing:** Either a `--auto-calibrate` flag on
`smoke_sidtab`, or a clearer error message that suggests the
calibration command.
