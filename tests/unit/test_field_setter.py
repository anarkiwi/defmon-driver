"""Unit tests for the pure-python helpers in defmon_driver.field_setter.

These functions compute pattern / arranger / sidTAB addresses and validate
inputs without touching a BinMon socket. The chord-driven and direct-mem
paths that DO hit BinMon are exercised separately via the smoke harness
(out of unit-test scope)."""

from __future__ import annotations

import pytest

from defmon_driver.field_setter import (
    ARRANGER_BASE,
    ARRANGER_MAX_STEP,
    BYTES_PER_STEP,
    BYTES_PER_VOICE,
    NOTE_KEYCODES,
    NOTE_PATTERN_BYTES,
    PATTERN_BASE,
    SUB_FIELD_OFFSET,
    arranger_cell_address,
    cell_address,
)
from defmon_driver.keycode_table import STATIC_KEYCODES


def test_pattern_layout_constants() -> None:
    # V0/V1/V2 × 4 bytes (flag/slot_a/slot_b/note) per step.
    assert BYTES_PER_VOICE == 4
    assert BYTES_PER_STEP == 12
    assert PATTERN_BASE == 0x1F00


def test_sub_field_offset_mapping() -> None:
    # Order is exactly flag, slot_a, slot_b, note.
    assert SUB_FIELD_OFFSET == {"speed": 0, "sidcall1": 1, "sidcall2": 2, "note": 3}


def test_cell_address_v0_step0_note() -> None:
    # Step 0 / voice 0 / note byte = $1F00 + 0 + 0 + 3.
    assert cell_address(0, 0, "note") == 0x1F03


def test_cell_address_v2_step1() -> None:
    # Step 1 starts at $1F00 + 12 = $1F0C. V2 offset = 8.
    # sub_field=speed → +0.
    assert cell_address(2, 1, "speed") == 0x1F14
    # sub_field=note → +3.
    assert cell_address(2, 1, "note") == 0x1F17


def test_cell_address_last_visible_step() -> None:
    # Step 31 is the last in a $80-byte pattern: $1F00 + 31*12 = $1F00 + 372 = $207C.
    addr = cell_address(0, 31, "speed")
    assert addr == PATTERN_BASE + 31 * BYTES_PER_STEP


def test_cell_address_rejects_sid2_voices() -> None:
    for voice in (3, 4, 5):
        with pytest.raises(ValueError, match="voice"):
            cell_address(voice, 0, "speed")


def test_cell_address_rejects_unknown_sub_field() -> None:
    with pytest.raises(ValueError, match="sub_field"):
        cell_address(0, 0, "not_a_field")


def test_arranger_cell_address_sid1_voices() -> None:
    assert arranger_cell_address(0, 0) == 0x1B00
    assert arranger_cell_address(1, 0) == 0x1C00
    assert arranger_cell_address(2, 0) == 0x1D00
    # Step is added directly as a byte offset.
    assert arranger_cell_address(0, 5) == 0x1B05


def test_arranger_cell_address_sid2_voices() -> None:
    assert arranger_cell_address(3, 0) == 0x6E00
    assert arranger_cell_address(4, 0) == 0x6F00
    assert arranger_cell_address(5, 0) == 0x7000
    assert arranger_cell_address(5, 0x7F) == 0x707F


def test_arranger_cell_address_rejects_invalid_voice() -> None:
    with pytest.raises(ValueError, match="voice"):
        arranger_cell_address(6, 0)


def test_arranger_cell_address_rejects_oob_step() -> None:
    # ARRANGER_MAX_STEP is 0x7F; one past it must raise.
    with pytest.raises(ValueError, match="step"):
        arranger_cell_address(0, ARRANGER_MAX_STEP + 1)
    with pytest.raises(ValueError, match="step"):
        arranger_cell_address(0, -1)


def test_arranger_base_table_keys_complete() -> None:
    # All six voices must have an arranger base entry, no extras.
    assert set(ARRANGER_BASE) == {0, 1, 2, 3, 4, 5}


# ---- NOTE_KEYCODES / NOTE_PATTERN_BYTES invariants --------------------------
#
# The chord-driven note write at field_setter._set_field_seqed_note_chord
# reverse-looks the requested note byte through NOTE_PATTERN_BYTES to get a
# key name, then forward-looks the key name through NOTE_KEYCODES to get
# the $0E44 keycode to inject. If the two dicts ever drift apart on key
# names (or NOTE_KEYCODES diverges from the LUT-derived STATIC_KEYCODES)
# the chord-driven path silently writes the wrong note.


def test_note_keycodes_and_pattern_bytes_share_key_set() -> None:
    assert set(NOTE_KEYCODES) == set(NOTE_PATTERN_BYTES)


def test_note_pattern_bytes_are_sequential_lower_octave() -> None:
    # defMON encodes Z..M as $30..$3B in pattern memory.
    expected = {k: 0x30 + i for i, k in enumerate("ZSXDCVGBHNJM")}
    assert NOTE_PATTERN_BYTES == expected


def test_note_keycodes_match_lut_derived_keycodes() -> None:
    # NOTE_KEYCODES holds $0E44 keycodes (the bytes defMON's scanner
    # writes when each note key is pressed); they MUST match the
    # LUT-derived STATIC_KEYCODES, otherwise injecting them via
    # press_via_loop writes the wrong note.
    for name, kc in NOTE_KEYCODES.items():
        assert STATIC_KEYCODES[name] == kc, (
            f"{name}: NOTE_KEYCODES says 0x{kc:02X} but STATIC_KEYCODES "
            f"says 0x{STATIC_KEYCODES[name]:02X}"
        )
