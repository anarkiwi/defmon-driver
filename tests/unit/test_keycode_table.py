"""Unit tests for defmon_driver.keycode_table — name → keycode resolution
and JSON persistence. No emulator required."""

from __future__ import annotations

import json

import pytest

from defmon_driver.keycode_table import (
    LUT_LEN,
    MOD1_BITS,
    STATIC_KEYCODES,
    ResolvedChord,
    decode_lut,
    load_table,
    resolve_chord,
    save_table,
)


def test_static_table_matches_lut() -> None:
    # Values are derived from defMON's $0F90 matrix-slot LUT and
    # verified by direct $0E44 observation. Z is $1A (PETSCII-ish
    # letter-index pattern); $30 is digit '0', not Z.
    assert STATIC_KEYCODES["Z"] == 0x1A
    assert STATIC_KEYCODES["0"] == 0x30
    assert STATIC_KEYCODES["1"] == 0x31
    # Function keys are at $B1/$B3/$B5/$B7 in the LUT.
    assert STATIC_KEYCODES["F1"] == 0xB1
    assert STATIC_KEYCODES["F7"] == 0xB7
    # Modifier keys are NOT in the table (LUT $FF sentinel).
    assert "CTRL" not in STATIC_KEYCODES
    assert "LSHIFT" not in STATIC_KEYCODES


def test_resolve_single_key() -> None:
    r = resolve_chord(("Z",))
    assert r == ResolvedChord(mod1=0, mod2=0, key="Z", keycode=0x1A)


def test_resolve_case_insensitive() -> None:
    assert resolve_chord(("z",)) == resolve_chord(("Z",))


def test_resolve_ctrl_modifier_promoted() -> None:
    r = resolve_chord(("CTRL", "Z"))
    assert r.mod1 == MOD1_BITS["CTRL"]
    assert r.key == "Z"
    assert r.keycode == STATIC_KEYCODES["Z"]


def test_resolve_multi_modifier_or() -> None:
    r = resolve_chord(("CTRL", "CBM", "Z"))
    assert r.mod1 == MOD1_BITS["CTRL"] | MOD1_BITS["CBM"]
    assert r.keycode == STATIC_KEYCODES["Z"]


def test_resolve_shift_alias_matches_lshift() -> None:
    r1 = resolve_chord(("SHIFT", "Z"))
    r2 = resolve_chord(("LSHIFT", "Z"))
    assert r1.mod1 == r2.mod1 == MOD1_BITS["LSHIFT"]
    # RSHIFT shares the same flag bit.
    r3 = resolve_chord(("RSHIFT", "Z"))
    assert r3.mod1 == r1.mod1


def test_resolve_pure_modifier_chord_has_no_key() -> None:
    r = resolve_chord(("CTRL",))
    assert r.mod1 == MOD1_BITS["CTRL"]
    assert r.key is None
    assert r.keycode is None


def test_resolve_two_non_modifiers_rejected() -> None:
    with pytest.raises(ValueError, match="more than one non-modifier"):
        resolve_chord(("Z", "X"))


def test_resolve_unknown_key_raises() -> None:
    # canonical_name rejects unmapped names before we ever look at the
    # keycode table.
    with pytest.raises(KeyError):
        resolve_chord(("DEFINITELY_NOT_A_KEY",))


def test_resolve_known_key_with_no_static_entry_raises() -> None:
    # COLON is a real C64 key but maps to LUT sentinel $FE (voice-mute
    # modifier dispatched via $0E42) so it's excluded from STATIC_KEYCODES.
    # resolve_chord must give a clear KeyError pointing at the bootstrap
    # command, not a silent zero keycode.
    assert "COLON" not in STATIC_KEYCODES
    with pytest.raises(KeyError, match="bootstrap"):
        resolve_chord(("COLON",))


def test_resolve_with_custom_table() -> None:
    r = resolve_chord(("COLON",), table={"COLON": 0xAB})
    assert r.keycode == 0xAB
    # Custom table doesn't have to include any of the static entries —
    # callers explicitly opting in get exactly what they pass.
    with pytest.raises(KeyError):
        resolve_chord(("Z",), table={"COLON": 0xAB})


def test_save_load_roundtrip(tmp_path) -> None:
    path = tmp_path / "kc.json"
    save_table(path, {"COLON": 0x99, "Z": 0x77})
    loaded = load_table(path)
    # User-provided values override the static fallback...
    assert loaded["COLON"] == 0x99
    assert loaded["Z"] == 0x77
    # ...and static entries the JSON omits are still present.
    assert loaded["M"] == STATIC_KEYCODES["M"]


def test_save_table_is_deterministic(tmp_path) -> None:
    path = tmp_path / "kc.json"
    save_table(path, {"M": 0x0D, "Z": 0x1A, "A": 0x01})
    text = path.read_text()
    # Sorted-key output makes git diffs review-friendly.
    parsed = json.loads(text)
    assert list(parsed.keys()) == sorted(parsed.keys())


def test_load_table_missing_path_falls_back(tmp_path, caplog) -> None:
    missing = tmp_path / "nope.json"
    caplog.set_level("WARNING")
    loaded = load_table(missing)
    assert loaded == STATIC_KEYCODES
    assert any("not found" in r.message for r in caplog.records)


def test_load_table_none_returns_static_copy() -> None:
    loaded = load_table(None)
    assert loaded == STATIC_KEYCODES
    # Must be a copy so callers can mutate it freely.
    loaded["Z"] = 0xFF
    assert STATIC_KEYCODES["Z"] == 0x1A


def test_load_table_rejects_bad_value(tmp_path) -> None:
    path = tmp_path / "kc.json"
    path.write_text(json.dumps({"Z": 999}))
    with pytest.raises(ValueError, match="not a u8"):
        load_table(path)


def test_load_table_rejects_non_object(tmp_path) -> None:
    path = tmp_path / "kc.json"
    path.write_text(json.dumps(["Z", 0x30]))
    with pytest.raises(ValueError, match="JSON object"):
        load_table(path)


# ---- decode_lut --------------------------------------------------------------
#
# Captured $0F90..$0FCF dump from a stock defMON build. The byte at
# slot s = row*8 + (7-col) is the post-LUT $0E44 keycode for the C64
# matrix key at (row, col). $FE/$FF are sentinels (voice-mute keys
# dispatched via $0E42 and pure-modifier keys, respectively).
_LIVE_LUT = bytes(
    int(b, 16)
    for b in (
        "6F B5 B3 B1 B7 6A 92 84 "
        "FF 05 13 1A 34 01 17 33 "
        "18 14 06 03 36 04 12 35 "
        "16 15 08 02 38 07 19 37 "
        "0E 0F 0B 0D 30 0A 09 39 "
        "2C 1D FE 2E 3D 0C 10 2D "
        "2F 1E FE FF 88 FE 00 3A "
        "93 11 FF 20 32 FF 1F 31 "
    ).split()
)


def test_decode_lut_round_trips_static_table() -> None:
    # The on-disk STATIC_KEYCODES dict must be exactly what decode_lut
    # produces from the live LUT dump — otherwise the seeded values
    # are out of sync with the indexing we ship.
    decoded = decode_lut(_LIVE_LUT)
    assert decoded == STATIC_KEYCODES


def test_decode_lut_rejects_wrong_length() -> None:
    with pytest.raises(ValueError, match=f"{LUT_LEN} bytes"):
        decode_lut(b"\x00" * (LUT_LEN - 1))


def test_decode_lut_excludes_sentinel_slots() -> None:
    decoded = decode_lut(_LIVE_LUT)
    # Modifier rows: LSHIFT/RSHIFT/CTRL/CBM are $FF; voice-mute keys
    # COLON/SEMICOLON/EQUALS are $FE. None should appear in the table.
    assert "LSHIFT" not in decoded
    assert "RSHIFT" not in decoded
    assert "CTRL" not in decoded
    assert "CBM" not in decoded
    assert "COLON" not in decoded
    assert "SEMICOLON" not in decoded
    assert "EQUALS" not in decoded


def test_decode_lut_indexing_handles_col_reversal() -> None:
    # Within a row the LUT bytes are stored col-reversed:
    # slot 0 of row 0 holds the keycode for col 7 (CRSRUD), and
    # slot 7 of row 0 holds the keycode for col 0 (INSTDEL).
    decoded = decode_lut(_LIVE_LUT)
    assert decoded["CRSRUD"] == _LIVE_LUT[0]  # row 0 col 7 -> slot 0
    assert decoded["INSTDEL"] == _LIVE_LUT[7]  # row 0 col 0 -> slot 7
    assert decoded["Z"] == _LIVE_LUT[1 * 8 + (7 - 4)]  # row 1 col 4 -> slot 11
    assert decoded["S"] == _LIVE_LUT[1 * 8 + (7 - 5)]  # row 1 col 5 -> slot 10
