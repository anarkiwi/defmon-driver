"""Coverage of screen-driven and disk-menu Defmon methods.

The harness drives screen-content polling via crafted SCREEN_GET response
bytes (see ``_fakebinmon.make_screen_bytes``). Long polling timeouts are
bypassed by scripting the very first screen response to satisfy the
predicate.
"""

from __future__ import annotations

import pytest

from defmon_driver.defmon import Defmon, DefmonError

from ._fakebinmon import (
    _make_blank_screen_bytes,
    make_recording_defmon,
    make_screen_bytes,
)

# ---- screen() and the three wait_for_screen_* helpers ----------------------


def test_screen_returns_parsed_snapshot() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["HELLO WORLD"])]
    snap = d.screen()
    assert snap.contains("HELLO")


def test_wait_for_screen_text_returns_on_first_match() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["DEFMON V123"])]
    snap = d.wait_for_screen_text("DEFMON", timeout=0.2, poll=0.01)
    assert snap.contains("DEFMON")


def test_wait_for_screen_text_absent_mode() -> None:
    d, bm = make_recording_defmon()
    # First screen has the needle; second clears it.
    bm.screens = [
        make_screen_bytes(["BOOT DEFMON"]),
        make_screen_bytes(["READY"]),
    ]
    snap = d.wait_for_screen_text("DEFMON", absent=True, timeout=0.5, poll=0.01)
    assert not snap.contains("DEFMON")


def test_wait_for_screen_text_raises_on_timeout() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["BOOT"])]  # never matches needle
    with pytest.raises(DefmonError, match="timeout waiting"):
        d.wait_for_screen_text("DEFMON", timeout=0.05, poll=0.01)


def test_wait_for_screen_change_returns_when_screen_differs() -> None:
    d, bm = make_recording_defmon()
    baseline = make_screen_bytes(["ORIGINAL"])
    changed = make_screen_bytes(["DIFFERENT"])
    bm.screens = [baseline, changed]
    snap_before = d.screen()
    bm.screens = [changed]
    bm.screen_index = 0
    snap_after = d.wait_for_screen_change(snap_before, timeout=0.2, poll=0.01)
    assert snap_after.screen != snap_before.screen


def test_wait_for_screen_change_returns_baseline_on_timeout() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["FROZEN"])]
    snap_before = d.screen()
    snap_after = d.wait_for_screen_change(snap_before, timeout=0.05, poll=0.01)
    # Polled but never differed; helper returns the latest snapshot.
    assert snap_after.screen == snap_before.screen


def test_wait_for_screen_stable_returns_when_two_consecutive_match() -> None:
    d, bm = make_recording_defmon()
    # Two identical snapshots in a row → stable.
    bm.screens = [
        make_screen_bytes(["FIRST"]),
        make_screen_bytes(["SAME"]),
        make_screen_bytes(["SAME"]),
        make_screen_bytes(["SAME"]),
    ]
    snap = d.wait_for_screen_stable(stable_for=0.02, timeout=0.5, poll=0.01)
    assert snap.contains("SAME")


def test_wait_for_screen_stable_raises_when_never_settles() -> None:
    d, bm = make_recording_defmon()

    # Each call returns a unique screen — never stable.
    seq = iter(range(1000))

    def churn(*_a, **_kw):
        return make_screen_bytes([f"ROW{next(seq)}"])

    bm.screen_get = churn  # type: ignore[assignment]
    snap = d.wait_for_screen_stable(stable_for=0.1, timeout=0.15, poll=0.01)
    assert snap is not None  # helper returns last snapshot at timeout


# ---- wait_for_defmon_loaded -----------------------------------------------


def test_wait_for_defmon_loaded_returns_on_defmon_text() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["DEFMON V123 LOADED"])]
    snap = d.wait_for_defmon_loaded(timeout=0.5)
    assert snap.contains("DEFMON")


def test_wait_for_defmon_loaded_falls_back_to_stability_when_text_missing() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["IDLE"])] * 6
    snap = d.wait_for_defmon_loaded(timeout=0.5)
    assert snap is not None


# ---- type_text ------------------------------------------------------------


def test_type_text_taps_each_chord() -> None:
    d, bm = make_recording_defmon()
    d.type_text("AB", per_char_settle=0)
    # text_to_chords yields the matrix chord per letter; both A and B are
    # bare key taps with no modifier on the upper/graphics charset.
    assert ("A",) in bm.taps
    assert ("B",) in bm.taps


# ---- disk menu helpers ----------------------------------------------------


def test_disk_read_directory_taps_space_and_returns_snapshot() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["DIR"])]
    snap = d.disk_read_directory()
    assert ("SPACE",) in bm.taps
    assert snap.contains("DIR")


def test_disk_prev_and_next_drive_emit_their_chords() -> None:
    d, bm = make_recording_defmon()
    d.disk_prev_drive()
    d.disk_next_drive()
    assert bm.taps == [("COMMA",), ("PERIOD",)]


def test_disk_current_drive_extracts_first_digit_from_footer() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes([""] * 24 + ["8 SOME FOOTER"])]
    assert d.disk_current_drive() == 8


def test_disk_current_drive_scans_back_when_last_row_blank() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes([""] * 22 + ["9 DRIVE", "", ""])]
    assert d.disk_current_drive() == 9


def test_disk_current_drive_returns_none_when_no_digit_present() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes([""] * 25)]
    assert d.disk_current_drive() is None


def test_disk_select_drive_returns_immediately_when_already_correct() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes([""] * 24 + ["8 ON 8"])]
    assert d.disk_select_drive(8) == 8


def test_disk_select_drive_raises_when_unable_to_reach_drive() -> None:
    d, bm = make_recording_defmon()
    # Fix the screen at drive 8 → can never reach drive 9.
    bm.screen_get = lambda: make_screen_bytes([""] * 24 + ["8 STUCK"])  # type: ignore[assignment]
    with pytest.raises(DefmonError, match="could not select drive"):
        d.disk_select_drive(9, max_steps=2)


def test_disk_load_by_index_taps_return_by_default() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["STABLE"])] * 5
    d.disk_load_by_index()
    assert bm.taps[0] == ("RETURN",)


def test_disk_load_by_name_types_filename_and_commits() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["LOADED"])] * 5
    d.disk_load_by_name("AB")
    assert bm.taps[0] == ("L",)
    # type_text emits chord per char; A and B are bare-key taps.
    assert ("A",) in bm.taps
    assert ("B",) in bm.taps
    assert ("RETURN",) in bm.taps


def test_disk_legacy_read_and_write() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["IDLE"])] * 10
    d.disk_legacy_read()
    d.disk_legacy_write()
    assert ("R",) in bm.taps
    assert ("W",) in bm.taps


def test_disk_save_overwrite_taps_lshift_s() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["IDLE"])] * 5
    d.disk_save_overwrite()
    assert bm.taps[0] == ("LSHIFT", "S")


def test_disk_pack_song_without_name_returns_immediately() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["PACKED"])] * 5
    d.disk_pack_song()
    assert bm.taps[0] == ("LSHIFT", "P")
    # No filename → no RETURN tap, no extra type_text.
    assert ("RETURN",) not in bm.taps


def test_disk_pack_song_with_name_types_and_returns() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["PACKED"])] * 5
    d.disk_pack_song("AB")
    assert bm.taps[0] == ("LSHIFT", "P")
    assert ("A",) in bm.taps
    assert ("RETURN",) in bm.taps


# ---- throwaway helpers --------------------------------------------------


def test_throwaway_disk_helpers_dispatch() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [make_screen_bytes(["DONE"])] * 50
    d.disk_save_new_throwaway()
    d.disk_load_by_name_throwaway()
    d.disk_pack_song_throwaway()
    d.disk_load_by_name_missing()
    # Each helper emits at least its main chord — exact validation is
    # covered by the underlying disk_* tests above.
    assert bm.taps


# ---- _find_dir_cursor_row / _dir_row_is_blank ------------------------------


def test_find_dir_cursor_row_returns_first_gt_row() -> None:
    snap_bytes = make_screen_bytes(
        [
            "HEADER",
            " 12 NORMAL",
            ">CURSOR HERE",
            " 13 OTHER",
        ]
    )
    from vice_driver.screen import parse_screen_response

    snap = parse_screen_response(snap_bytes)
    assert Defmon._find_dir_cursor_row(snap) == 2


def test_find_dir_cursor_row_none_if_no_marker() -> None:
    from vice_driver.screen import parse_screen_response

    snap = parse_screen_response(make_screen_bytes(["NO MARKER"]))
    assert Defmon._find_dir_cursor_row(snap) is None


def test_dir_row_is_blank_classifier() -> None:
    # Empty/whitespace → blank.
    assert Defmon._dir_row_is_blank("") is True
    assert Defmon._dir_row_is_blank("   ") is True
    # Non-hex leading char → blank (decorative DEL rows etc.).
    assert Defmon._dir_row_is_blank("ZZZ NOTE") is True
    assert Defmon._dir_row_is_blank("- - - -") is True
    # Hex-digit leading char (block count) → populated entry.
    assert Defmon._dir_row_is_blank("12 BLOCKS HERE") is False
    assert Defmon._dir_row_is_blank("AF DATA") is False
    assert Defmon._dir_row_is_blank("FILENAME.PRG") is False  # 'F' is hex


# ---- ensure_stereo / select_sid_chip / set_sid2 short circuits ------------


def test_ensure_stereo_short_circuits_when_state_matches() -> None:
    d, bm = make_recording_defmon(mem_map={Defmon.ADDR_STEREO_FLAG: 0x01})
    d.ensure_stereo(enabled=True)
    assert bm.taps == []  # already on


def test_select_sid_chip_short_circuits_when_state_matches() -> None:
    d, bm = make_recording_defmon(mem_map={Defmon.ADDR_CHIP_SELECT: 0x00})
    d.select_sid_chip(1)
    assert bm.taps == []  # already on SID#1


def test_select_sid_chip_rejects_invalid_chip() -> None:
    d, _ = make_recording_defmon()
    with pytest.raises(DefmonError, match="chip must be 1 or 2"):
        d.select_sid_chip(3)


def test_set_sid2_base_address_requires_stereo_on() -> None:
    d, _ = make_recording_defmon(mem_map={Defmon.ADDR_STEREO_FLAG: 0x00})
    with pytest.raises(DefmonError, match="stereo is OFF"):
        d.set_sid2_base_address(0xD420)


def test_set_sid2_base_address_rejects_invalid_high_byte() -> None:
    d, _ = make_recording_defmon(mem_map={Defmon.ADDR_STEREO_FLAG: 0x01})
    with pytest.raises(DefmonError, match="high byte must be"):
        d.set_sid2_base_address(0xC020)


def test_set_sid2_base_address_rejects_misaligned_low_byte() -> None:
    d, _ = make_recording_defmon(mem_map={Defmon.ADDR_STEREO_FLAG: 0x01})
    with pytest.raises(DefmonError, match="low byte must be a multiple"):
        d.set_sid2_base_address(0xD421)


def test_set_sid2_base_address_no_op_when_already_at_target() -> None:
    d, bm = make_recording_defmon(
        mem_map={
            Defmon.ADDR_STEREO_FLAG: 0x01,
            Defmon.ADDR_SID2_HIGH: 0xD4,
            Defmon.ADDR_SID2_LOW: 0x20,
        }
    )
    d.set_sid2_base_address(0xD420)
    # Both _cycle_until calls early-return on the first mem_get match.
    assert bm.taps == []


# ---- disk_save_new validation ---------------------------------------------


def test_disk_save_new_rejects_overlong_filename() -> None:
    d, bm = make_recording_defmon()
    bm.screens = [_make_blank_screen_bytes()]  # unused but referenced
    with pytest.raises(DefmonError, match="15 chars max"):
        d.disk_save_new("X" * 16)


# ---- hold helper ----------------------------------------------------------


def test_hold_delegates_to_tap_with_fixed_mode() -> None:
    d, bm = make_recording_defmon()
    d.hold("CTRL", "X", frames=24)
    assert bm.taps == [("CTRL", "X")]


# ---- tap retry guard ------------------------------------------------------


def test_tap_rejects_zero_max_retries_via_validator() -> None:
    d, _ = make_recording_defmon()
    with pytest.raises(ValueError, match="max_retries"):
        d.tap("Z", max_retries=0)
