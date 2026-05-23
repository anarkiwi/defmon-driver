# Changelog

All notable changes to `defmon-driver` are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project uses semantic-versioning-ish tags but the v0.x line is
still pre-stable, so breaking changes can land in minor bumps.

## [Unreleased]

## [0.3.0] - 2026-05-23

### Changed

- **Split shared transport/emulator-control code into the `vice-driver`
  package.** `defmon-driver` now depends on `vice-driver>=0.2.0`. The
  modules `defmon_driver.binmon`, `defmon_driver.keys`,
  `defmon_driver.screen`, `defmon_driver.vice_docker`, and
  `defmon_driver.coverage` have been removed; their content lives in
  the matching `vice_driver.*` modules. The `Expect` / `ExpectPredicate`
  / `verify` symbols previously defined in `defmon_driver.keyhandler`
  also moved to `vice_driver.expect` — `defmon_driver.keyhandler`
  re-exports them so existing imports keep working.
  `defmon_driver.__init__` continues to re-export the shared symbols
  (`BinMon`, `KEY`, `ScreenSnapshot`, `ViceContainer`, `Expect`,
  `verify`, …) so callers using the top-level package surface need no
  changes; new code should import them directly from `vice_driver`.

### Added

- `defmon_driver.keycode_table` — name → ``$0E44`` keycode resolver
  built from defMON's ``$0F90`` matrix-slot LUT (slot indexing is
  ``row*8 + (7-col)``). Exposes ``STATIC_KEYCODES``, ``resolve_chord``,
  ``decode_lut``, ``load_table`` / ``save_table``, and ``bootstrap_keycodes``.
- `defmon_driver.bootstrap_keycodes` — ``python -m`` CLI that boots a
  one-shot container, reads the LUT, and writes the decoded JSON.
- `defmon_driver.keyhandler.Expect`, ``InjectOutcome``, ``verify(bm, expect)``
  — transport-agnostic verification dataclasses and polling helper.
- `Defmon.inject(*names, mode=None, expect=None, ...)` — direct-call
  dispatch through ``press_via_loop`` for chords whose handler runs in
  the mode dispatcher (notes, hex digits). Multi-modifier chord
  *commands* (CTRL+letter etc.) are not handled — they're scanner-
  decoded and ``press_via_loop`` skips the scanner. The docstring
  notes the limitation.
- `Defmon.tap(..., expect=, max_retries=, pre_quiesce=)` — post-tap
  verification predicate + automatic retry over the matrix-tap
  transport. Folds the three previous ad-hoc retry loops
  (`_retry_chord_until`, `_cycle_until`, `_stereo_quiesce` + retries)
  into one mechanism.
- `defmon_driver.field_setter.NOTE_PATTERN_BYTES` — separate dict for
  the byte values defMON stores in pattern memory after a note
  keypress (Z=$30, S=$31, …, M=$3B). See *Fixed* below for the
  rationale.
- `defmon_driver.smoke_note_chord` — live smoke that exercises
  ``write_note_chord`` for every note in the lower octave and
  verifies the expected byte lands in pattern memory.
- `defmon_driver.smoke_sidcall` — live smoke that exercises chord-driven
  sidcall1 across V0/V1/V2 × step 0/3/7 × value 0x00/0x57/0xAB/0xFF and
  verifies the byte lands at the runtime cell address. Catches future
  regressions of the BUGS.md #2 class.
- `defmon_driver.keys.canonical_name(name)` — public alias-aware name
  normalisation, used by ``keycode_table.resolve_chord``.

### Changed

- `defmon_driver.field_setter.read_cell` / `write_cell_direct` /
  chord-driven `set_field` paths — now always resolve cells via
  `runtime_cell_address`. The legacy `cell_address` fallback (which
  assumed V0/V1/V2 mirror at pattern 0 with a 12-byte stride) silently
  read/wrote the wrong address even on a fresh-boot disk image; the
  legacy helper is retained for callers that need the pattern-0
  mirroring assumption but is no longer exercised by the public API.
- `defmon_driver.keyhandler.Sid2Chord` — now accepts voices 0..5
  (was 3..5). For SID#1 voices the arranger-proxy swap is a no-op;
  the value of the context manager is holding the halt across the
  cursor seed and all subsequent presses, and re-seeding
  ``$71CD``/``$71D2`` before each press so defMON's main-loop cursor
  recompute can't bump the second digit to a different voice.
- `defmon_driver.field_setter.NOTE_KEYCODES` — now holds the
  LUT-correct ``$0E44`` keycodes (Z=$1A, S=$13, X=$18, D=$04, C=$03,
  V=$16, G=$07, B=$02, H=$08, N=$0E, J=$0A, M=$0D). Previously these
  values were the note-byte values (Z=$30, …) used as keycodes for
  ``press_via_loop`` — see *Fixed* for the full story.
- `defmon_driver.field_setter` — `KC_CRSRRR` → **renamed** to
  `KC_CRSRLR` (and its value moved from $6F to $6A). `KC_CRSRDOWN` →
  **renamed** to `KC_CRSRUD` (and its value moved from $6A to $6F).
  The old names were labelled with the *unshifted* movement direction
  ("right", "down") but held the keycode for the *opposite* key on
  the C64 matrix (the up/down key and the left/right key
  respectively). The behaviour of `cursor_step_keypress` and
  `cursor_right_keypress` was correspondingly swapped:
  `cursor_step_keypress(n=1)` now actually moves the cursor *down*
  (it was moving right) and `cursor_right_keypress(n=1)` now actually
  moves *right* (it was moving down).

  **Downstream impact:** code that relied on the old (buggy)
  behaviour — typically by passing a different sign or by using
  ``cursor_right_keypress`` to mean "advance to the next step row" —
  needs to be reviewed. The smoke harness did not exercise these
  helpers, so the bug went unobserved for the v0.1 line.
- `defmon_driver.field_setter.KC_RETURN` — fixed from the placeholder
  `0x0D` to the correct LUT value `0x92`.
- `defmon_driver.field_setter._set_field_seqed_note_chord` — chord-driven
  note write now reverse-looks the requested note byte through
  ``NOTE_PATTERN_BYTES`` and forward-looks the key name through the
  corrected ``NOTE_KEYCODES``, so it actually writes the requested note.

### Removed

- `Defmon._retry_chord_until`, `Defmon._poll_byte_until` — collapsed
  into `Defmon.tap(..., expect=, max_retries=)`.

### Fixed

- **seqED V0/V2 sidCALL1 was silently mis-writing** (BUGS.md #2).
  Two compounding bugs: (a) ``position_cursor``'s ``mem_set`` of
  ``$71CD`` doesn't stick because defMON's main loop restores it on
  every iteration — and ``Sid2Chord`` was only re-seeding before the
  first press of a multi-press chord, so the second digit landed on
  V0 even when targeting V1/V2; (b) ``read_cell`` /
  ``write_cell_direct`` / chord-driven ``set_field`` fell back to the
  legacy ``cell_address`` helper for V0-V2, which assumes V0/V1/V2
  share pattern 0 at ``$1F00`` with a 12-byte stride — empirically
  false even on freshly booted ``defmon-20201008.d64``. Both fixed:
  ``Sid2Chord.press`` re-seeds ``$71CD``/``$71D2`` before every press,
  and every cell-address path now resolves via ``runtime_cell_address``
  unconditionally. The chord-driven sidcall1 path is now reliable
  across all six voices.
- **`field_setter.NOTE_KEYCODES` was mislabelled.** The pre-fix values
  ($30..$3B) were the note-byte values defMON stores in pattern memory
  after a successful note keypress — not the ``$0E44`` keycodes the
  scanner writes for those keys (which are $1A, $13, $18, …, $0D per
  the ``$0F90`` LUT). Every call to ``press_via_loop`` that used those
  constants as ``keycode=`` was dispatching the wrong key, so
  `write_note_chord("Z")` was writing `$4B` to pattern memory instead
  of `$30`. The smoke harness did not validate the resulting note
  bytes, so this regression shipped quietly. The new
  ``defmon_driver.smoke_note_chord`` catches the family of regressions.

### Followups (not addressed in this release)

- `Defmon.inject` cannot trigger multi-modifier chord *commands* like
  CTRL+S, CTRL+R, CTRL+W, switch_sid_chip, etc., because those are
  decoded by defMON's keyboard scanner ($0E47) before the mode handler
  is reached, and `press_via_loop` NOPs the scanner out. Documented at
  the top of `keyhandler.py`; chord *commands* stay on the matrix-tap
  path. See BUGS.md.
- `cursor_step_keypress` / `cursor_right_keypress` docstrings note
  these helpers don't reliably update the writer-dispatcher's
  ``$71CB/$71D2``; they remain "cosmetic" walks. The position-affecting
  writer-cursor API is `position_cursor()`.
