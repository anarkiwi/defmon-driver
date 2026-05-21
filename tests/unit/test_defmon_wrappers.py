"""Coverage of every documented :class:`Defmon` wrapper method.

Each wrapper is a thin shim around ``self.tap(*chord)``; the goal here
is to (a) prove each emits the right chord and (b) execute the body so
coverage sees it. Uses :class:`RecordingBinMon` from ``_fakebinmon`` —
no emulator required.
"""

from __future__ import annotations

import pytest

from defmon_driver.defmon import Defmon, DefmonError

from ._fakebinmon import make_recording_defmon

# ---- Mode introspection -----------------------------------------------------


def test_current_mode_reads_mode_byte() -> None:
    d, bm = make_recording_defmon(mem_map={Defmon.ADDR_MODE: 0x01})
    assert d.current_mode() == 0x01


def test_current_mode_name_translates_known_modes() -> None:
    for byte, name in (
        (0x01, "seqed"),
        (0x02, "seqlist"),
        (0x04, "sidtab"),
        (0x20, "disk"),
    ):
        d, _ = make_recording_defmon(mem_map={Defmon.ADDR_MODE: byte})
        assert d.current_mode_name() == name


def test_current_mode_name_falls_back_to_unknown() -> None:
    d, _ = make_recording_defmon(mem_map={Defmon.ADDR_MODE: 0x99})
    assert d.current_mode_name() == "unknown:$99"


def test_is_stereo_enabled_reads_flag_byte() -> None:
    d_on, _ = make_recording_defmon(mem_map={Defmon.ADDR_STEREO_FLAG: 0x01})
    d_off, _ = make_recording_defmon(mem_map={Defmon.ADDR_STEREO_FLAG: 0x00})
    assert d_on.is_stereo_enabled() is True
    assert d_off.is_stereo_enabled() is False


def test_current_sid_chip_maps_715D_byte() -> None:
    d1, _ = make_recording_defmon(mem_map={Defmon.ADDR_CHIP_SELECT: 0x00})
    d2, _ = make_recording_defmon(mem_map={Defmon.ADDR_CHIP_SELECT: 0x01})
    assert d1.current_sid_chip() == 1
    assert d2.current_sid_chip() == 2


def test_current_sid2_base_address_packs_lo_hi() -> None:
    d, _ = make_recording_defmon(
        mem_map={
            Defmon.ADDR_SID2_LOW: 0x20,
            Defmon.ADDR_SID2_HIGH: 0xD4,
        }
    )
    assert d.current_sid2_base_address() == 0xD420


# ---- Hex-byte typing --------------------------------------------------------


def test_hex_digits_splits_byte() -> None:
    assert Defmon._hex_digits(0x00) == ("0", "0")
    assert Defmon._hex_digits(0xAB) == ("A", "B")
    assert Defmon._hex_digits(0xFF) == ("F", "F")


@pytest.mark.parametrize("value", [-1, 0x100, 0x1234])
def test_hex_digits_out_of_range_raises(value: int) -> None:
    with pytest.raises(DefmonError, match="byte out of range"):
        Defmon._hex_digits(value)


def test_type_sound_program_chords_lshift_cbm_then_digits() -> None:
    d, bm = make_recording_defmon()
    d.type_sound_program(0xAB, per_digit_settle=0)
    assert bm.taps == [("LSHIFT", "CBM", "A"), ("LSHIFT", "CBM", "B")]


def test_type_speed_chords_ctrl_cbm_then_digits() -> None:
    d, bm = make_recording_defmon()
    d.type_speed(0x12, per_digit_settle=0)
    assert bm.taps == [("CTRL", "CBM", "1"), ("CTRL", "CBM", "2")]


def test_type_hex_byte_taps_bare_digits() -> None:
    d, bm = make_recording_defmon()
    d.type_hex_byte(0x0F, per_digit_settle=0)
    assert bm.taps == [("0",), ("F",)]


# ---- Cursor walks -----------------------------------------------------------


@pytest.mark.parametrize(
    "method, expected",
    [
        ("cursor_right", ("CRSRLR",)),
        ("cursor_left", ("LSHIFT", "CRSRLR")),
        ("cursor_down", ("CRSRUD",)),
        ("cursor_up", ("LSHIFT", "CRSRUD")),
    ],
)
def test_cursor_walks_emit_correct_chord(method: str, expected: tuple[str, ...]) -> None:
    d, bm = make_recording_defmon()
    getattr(d, method)(count=3, settle=0)
    assert bm.taps == [expected] * 3


def test_cursor_walk_count_zero_emits_no_taps() -> None:
    d, bm = make_recording_defmon()
    d.cursor_right(count=0)
    assert bm.taps == []


# ---- Single-chord wrappers --------------------------------------------------

# Method-name → expected chord sequence (one chord per ``self.tap`` call).
SINGLE_CHORD_WRAPPERS: list[tuple[str, tuple[str, ...]]] = [
    ("toggle_seqed_seqlist", ("RUNSTOP",)),
    ("jump_arranger_position", ("CBM", "RUNSTOP")),
    ("enter_sidtab", ("LEFTARROW",)),
    ("jump_sidtab_position", ("LSHIFT", "LEFTARROW")),
    ("jump_sidtab_position_alt", ("CBM", "LEFTARROW")),
    ("switch_sid_chip", ("CTRL", "LEFTARROW")),
    ("toggle_stereo", ("CTRL", "LSHIFT", "LEFTARROW")),
    ("cycle_sid_high_byte", ("CTRL", "LSHIFT", "UPARROW")),
    ("adjust_sid_low_byte", ("CTRL", "CBM", "UPARROW")),
    ("play_from_cursor", ("F1",)),
    ("play_from_start", ("F3",)),
    ("toggle_follow", ("F5",)),
    ("stop_playback", ("F7",)),
    ("reset_bpm", ("LSHIFT", "F1")),
    ("insert_step", ("RETURN",)),
    ("remove_step", ("LSHIFT", "RETURN")),
    ("value_decrement", ("LSHIFT", "COMMA")),
    ("value_increment", ("LSHIFT", "PERIOD")),
    ("delete_value", ("INSTDEL",)),
    ("delete_advance", ("SPACE",)),
    ("clone_pattern_new", ("LSHIFT", "N")),
    ("clone_pattern_unused", ("LSHIFT", "U")),
    ("edit_instrument_columns", ("CBM", "LSHIFT")),
    ("edit_speed_column", ("CTRL", "CBM")),
    ("insert_pattern_break", ("CTRL", "CBM", "SPACE")),
    ("shift_octave_up", ("CTRL", "LSHIFT", "PERIOD")),
    ("shift_octave_down", ("CTRL", "LSHIFT", "COMMA")),
    ("chunk_size_decrease", ("CBM", "SLASH")),
    ("chunk_size_increase", ("LSHIFT", "SLASH")),
    ("cursor_pos_0", ("CTRL", "G")),
    ("cursor_pos_4", ("CTRL", "H")),
    ("cursor_pos_8", ("CTRL", "J")),
    ("cursor_pos_c", ("CTRL", "K")),
    ("super_exit", ("CTRL", "RETURN")),
]


@pytest.mark.parametrize("method, chord", SINGLE_CHORD_WRAPPERS)
def test_single_chord_wrappers(method: str, chord: tuple[str, ...]) -> None:
    d, bm = make_recording_defmon()
    getattr(d, method)()
    assert bm.taps == [chord]


# ---- Multi-tap super / chained --------------------------------------------


def test_super_steps_taps_ctrl_s_then_digits() -> None:
    d, bm = make_recording_defmon()
    d.super_steps(16)
    assert bm.taps == [("CTRL", "S"), ("1",), ("6",)]


def test_super_repeat_taps_ctrl_r_then_digits() -> None:
    d, bm = make_recording_defmon()
    d.super_repeat(2)
    assert bm.taps == [("CTRL", "R"), ("2",)]


def test_super_width_taps_ctrl_w_then_digits() -> None:
    d, bm = make_recording_defmon()
    d.super_width(24)
    assert bm.taps == [("CTRL", "W"), ("2",), ("4",)]


def test_super_zone_all_chains_ctrl_z_then_a() -> None:
    d, bm = make_recording_defmon()
    d.super_zone_all()
    assert bm.taps == [("CTRL", "Z"), ("A",)]


def test_cursor_first_step_twice_double_taps_ctrl_g() -> None:
    d, bm = make_recording_defmon()
    d.cursor_first_step_twice()
    assert bm.taps == [("CTRL", "G"), ("CTRL", "G")]


def test_super_chain_steps_repeat_emits_full_chain() -> None:
    d, bm = make_recording_defmon()
    d.super_chain_steps_repeat()
    assert bm.taps == [
        ("CTRL", "S"),
        ("4",),
        ("CTRL", "R"),
        ("2",),
    ]


def test_super_zero_arg_helpers_delegate() -> None:
    d, bm = make_recording_defmon()
    d.super_steps_4()
    d.super_steps_16()
    d.super_repeat_2()
    d.super_repeat_12()
    d.super_width_3()
    d.super_width_24()
    # 1 + 2 + 1 + 2 + 1 + 2 = 9 component-tap blocks. We don't need to
    # spell every chord here — single-digit/multi-digit branches are
    # already covered above. Just confirm each helper actually drove
    # the underlying super_* method.
    assert bm.taps[0] == ("CTRL", "S")  # super_steps_4
    assert bm.taps[1] == ("4",)
    assert bm.taps[2] == ("CTRL", "S")  # super_steps_16
    assert bm.taps[3] == ("1",)
    assert bm.taps[4] == ("6",)
    assert bm.taps[5] == ("CTRL", "R")
    assert bm.taps[6] == ("2",)
    # …remainder validated indirectly via the super_*(n) coverage above.


# ---- Mute / multispeed / BPM ------------------------------------------------


@pytest.mark.parametrize(
    "track, expected",
    [
        (1, ("LSHIFT", "COLON")),
        (2, ("LSHIFT", "SEMICOLON")),
        (3, ("EQUALS",)),
    ],
)
def test_mute_track_chord_per_track(track: int, expected: tuple[str, ...]) -> None:
    d, bm = make_recording_defmon()
    d.mute_track(track)
    assert bm.taps == [expected]


def test_mute_track_rejects_out_of_range() -> None:
    d, _ = make_recording_defmon()
    with pytest.raises(ValueError, match="track must be 1..3"):
        d.mute_track(4)


@pytest.mark.parametrize(
    "speed, base",
    [(1, "F1"), (2, "F3"), (4, "F5"), (8, "F7")],
)
def test_set_multispeed_maps_speed_to_fn_key(speed: int, base: str) -> None:
    d, bm = make_recording_defmon()
    d.set_multispeed(speed)
    assert bm.taps == [("LSHIFT", base)]


def test_set_multispeed_unknown_raises_keyerror() -> None:
    d, _ = make_recording_defmon()
    with pytest.raises(KeyError):
        d.set_multispeed(3)


@pytest.mark.parametrize("fkey", ["F1", "F3", "F5", "F7"])
def test_bump_bpm_chords_cbm_with_fkey(fkey: str) -> None:
    d, bm = make_recording_defmon()
    d.bump_bpm(fkey)
    assert bm.taps == [("CBM", fkey)]


# ---- multi_insert / type_note / tap_chord ----------------------------------


def test_multi_insert_chords_ctrl_digit() -> None:
    d, bm = make_recording_defmon()
    d.multi_insert("5")
    assert bm.taps == [("CTRL", "5")]


def test_type_note_taps_bare_key() -> None:
    d, bm = make_recording_defmon()
    d.type_note("Z")
    assert bm.taps == [("Z",)]


def test_tap_chord_is_public_alias_for_tap() -> None:
    d, bm = make_recording_defmon()
    d.tap_chord("CTRL", "X")
    assert bm.taps == [("CTRL", "X")]


# ---- documented-action indexes ---------------------------------------------


def test_all_documented_actions_is_a_complete_index() -> None:
    d, _ = make_recording_defmon()
    actions = d.all_documented_actions()
    names = [name for name, _ in actions]
    # Spot-check that the index spans every category.
    for needed in (
        "toggle_seqed_seqlist",
        "play_from_cursor",
        "super_exit",
        "cursor_pos_c",
        "shift_octave_up",
    ):
        assert needed in names
    # All targets are actually callable instance methods.
    for _name, fn in actions:
        assert callable(fn)


def test_all_documented_actions_each_emits_at_least_one_tap() -> None:
    d, bm = make_recording_defmon()
    for name, fn in d.all_documented_actions():
        bm.taps.clear()
        outcome = fn()
        # Most return TapOutcome; a few super_* return None.
        if outcome is not None:
            assert hasattr(outcome, "release_reason"), name
        assert bm.taps, f"{name} produced no taps"


def test_all_documented_disk_actions_index_is_callable() -> None:
    d, _ = make_recording_defmon()
    actions = d.all_documented_disk_actions()
    assert actions, "expected non-empty disk action index"
    for _name, fn in actions:
        assert callable(fn)


# ---- ensure_seqed fast path -------------------------------------------------


def test_ensure_seqed_returns_immediately_when_already_seqed() -> None:
    d, bm = make_recording_defmon(mem_map={Defmon.ADDR_MODE: Defmon.MODE_SEQED})
    d.ensure_seqed(settle=0)
    # Always-safe quiet (F7) + super_exit (CTRL+RETURN) precede the
    # mode check; once mode is SEQED the helper returns.
    assert bm.taps == [("F7",), ("CTRL", "RETURN")]


def test_ensure_seqed_falls_back_to_mem_set_if_chords_dont_converge() -> None:
    # Mode byte stays non-SEQED through every chord — helper must
    # eventually call mem_set($7167, MODE_SEQED) and verify.
    bm_state = {Defmon.ADDR_MODE: Defmon.MODE_SIDTAB}
    d, bm = make_recording_defmon(mem_map=bm_state)

    # Once the helper writes via mem_set, the bm_state will reflect it
    # because RecordingBinMon.mem_set updates mem_map in-place. So the
    # final mode-check will pass and ensure_seqed returns cleanly.
    d.ensure_seqed(max_steps=2, settle=0)
    # The mem_set fallback was reached: at least one entry writes
    # ADDR_MODE = MODE_SEQED.
    seqed_writes = [
        (addr, data)
        for addr, data in bm.mem_writes
        if addr == Defmon.ADDR_MODE and data == bytes([Defmon.MODE_SEQED])
    ]
    assert seqed_writes, "expected mem_set fallback to write MODE_SEQED"
