"""Coverage of :mod:`defmon_driver.field_setter` paths that DON'T require
a real emulator — direct-memory writes, runtime address resolution, set_field
mode dispatch, and validation errors. The chord-driven paths
(``Sid2Chord``, ``press_via_loop``) need a halted CPU and are exercised
by the live smoke tests instead.
"""

from __future__ import annotations

import pytest

from defmon_driver import field_setter as fs
from defmon_driver.field_setter import (
    ADDR_CURSOR_STEP,
    ADDR_DIGIT_PHASE,
    ADDR_PAT_BASE_HI,
    ADDR_PAT_BASE_LO,
    ADDR_RANGE_FILL,
    ADDR_SUPER_FLAGS,
    ADDR_VOICE_SELECTOR,
    ADDR_WRITE_COUNT,
    ADDR_WRITE_STRIDE,
    ARRANGER_BASE,
    PATTERN_BASE,
    SUB_FIELD_OFFSET,
    VOICE_SELECTOR_VALUES,
    FieldWriteResult,
    current_mode,
    cursor_state,
    pattern_base_for,
    position_cursor,
    read_arranger_cell,
    read_cell,
    runtime_cell_address,
    set_field,
    set_mode_direct,
    voice_pattern_base,
    write_arranger_cell_direct,
    write_cell_direct,
    write_pattern_block_direct,
)

from ._fakebinmon import RecordingBinMon


def _bm_with_pat_table(pat_num: int, base_addr: int) -> RecordingBinMon:
    """Build a BinMon stub whose $1A00/$1A80 entries point pat_num at
    base_addr. Used to test runtime address resolution."""
    bm = RecordingBinMon()
    bm.mem_map[ADDR_PAT_BASE_LO + pat_num] = base_addr & 0xFF
    bm.mem_map[ADDR_PAT_BASE_HI + pat_num] = (base_addr >> 8) & 0xFF
    return bm


# ---- pattern_base_for ------------------------------------------------------


def test_pattern_base_for_packs_lo_hi() -> None:
    bm = _bm_with_pat_table(pat_num=1, base_addr=0x1F80)
    assert pattern_base_for(bm, 1) == 0x1F80  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [-1, 0x80, 0x100])
def test_pattern_base_for_rejects_out_of_range(bad: int) -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="0..0x7F"):
        pattern_base_for(bm, bad)  # type: ignore[arg-type]


# ---- voice_pattern_base + runtime_cell_address ----------------------------


def test_voice_pattern_base_resolves_via_arranger() -> None:
    bm = RecordingBinMon()
    # V0 row 0 → pat 5, pat 5 → base $2000.
    bm.mem_map[ARRANGER_BASE[0] + 0] = 5
    bm.mem_map[ADDR_PAT_BASE_LO + 5] = 0x00
    bm.mem_map[ADDR_PAT_BASE_HI + 5] = 0x20
    assert voice_pattern_base(bm, 0, arranger_row=0) == 0x2000  # type: ignore[arg-type]


def test_runtime_cell_address_combines_arranger_and_pattern_base() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ARRANGER_BASE[1] + 2] = 7  # V1 row 2 → pat 7
    bm.mem_map[ADDR_PAT_BASE_LO + 7] = 0x80
    bm.mem_map[ADDR_PAT_BASE_HI + 7] = 0x1F
    # pat 7 base = $1F80; step 3, sub_field=note (offset 3) → $1F80 + 3*4 + 3 = $1F8F
    addr = runtime_cell_address(bm, 1, 3, "note", arranger_row=2)  # type: ignore[arg-type]
    assert addr == 0x1F80 + 3 * 4 + SUB_FIELD_OFFSET["note"]


def test_runtime_cell_address_rejects_bad_voice() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="voice must be 0..5"):
        runtime_cell_address(bm, 6, 0, "note")  # type: ignore[arg-type]


def test_runtime_cell_address_rejects_bad_sub_field() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="sub_field must be"):
        runtime_cell_address(bm, 0, 0, "bogus")  # type: ignore[arg-type]


# ---- read helpers ----------------------------------------------------------


def test_read_cell_returns_runtime_value() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ARRANGER_BASE[0] + 0] = 0  # pat 0
    bm.mem_map[ADDR_PAT_BASE_LO + 0] = 0x00
    bm.mem_map[ADDR_PAT_BASE_HI + 0] = 0x1F  # pat 0 → $1F00
    bm.mem_map[PATTERN_BASE + 0 * 4 + SUB_FIELD_OFFSET["speed"]] = 0xAB
    assert read_cell(bm, 0, 0, "speed") == 0xAB  # type: ignore[arg-type]


def test_read_arranger_cell_returns_pattern_number() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ARRANGER_BASE[2] + 5] = 0x42
    assert read_arranger_cell(bm, 2, 5) == 0x42  # type: ignore[arg-type]


# ---- write_cell_direct ----------------------------------------------------


def test_write_cell_direct_writes_and_reports_ok() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ARRANGER_BASE[0] + 0] = 0
    bm.mem_map[ADDR_PAT_BASE_LO + 0] = 0x00
    bm.mem_map[ADDR_PAT_BASE_HI + 0] = 0x1F
    res = write_cell_direct(bm, 0, 0, "note", value=0x5C)  # type: ignore[arg-type]
    assert res.ok
    assert res.post_value == 0x5C
    assert res.method.startswith("direct_mem:runtime")


# ---- write_arranger_cell_direct ------------------------------------------


def test_write_arranger_cell_direct_writes_pattern_number() -> None:
    bm = RecordingBinMon()
    res = write_arranger_cell_direct(bm, 1, 4, value=0x12)  # type: ignore[arg-type]
    assert res.ok
    assert bm.mem_map[ARRANGER_BASE[1] + 4] == 0x12


# ---- write_pattern_block_direct ------------------------------------------


def test_write_pattern_block_direct_writes_full_voice() -> None:
    bm = RecordingBinMon()
    write_pattern_block_direct(bm, step=2, voice=1, bytes_data=bytes([0x10, 0x20, 0x30, 0x40]))  # type: ignore[arg-type]
    base = PATTERN_BASE + 2 * 12 + 1 * 4
    for i, expected in enumerate((0x10, 0x20, 0x30, 0x40)):
        assert bm.mem_map[base + i] == expected


def test_write_pattern_block_direct_rejects_wrong_size() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="need exactly 4 bytes"):
        write_pattern_block_direct(bm, step=0, voice=0, bytes_data=b"\x10\x20")  # type: ignore[arg-type]


# ---- current_mode / set_mode_direct --------------------------------------


def test_current_mode_reads_7167() -> None:
    bm = RecordingBinMon()
    bm.mem_map[0x7167] = 0x04
    assert current_mode(bm) == 0x04  # type: ignore[arg-type]


def test_set_mode_direct_writes_mode_byte() -> None:
    bm = RecordingBinMon()
    set_mode_direct(bm, "seqed")  # type: ignore[arg-type]
    assert bm.mem_map[0x7167] == 0x01  # MODE_VAL["seqed"]


# ---- cursor_state ---------------------------------------------------------


def test_cursor_state_snapshots_all_writer_vars() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ADDR_VOICE_SELECTOR] = 0x09  # voice 1
    bm.mem_map[ADDR_CURSOR_STEP] = 0x05
    state = cursor_state(bm)  # type: ignore[arg-type]
    assert state["voice"] == 1
    assert state["voice_selector"] == 0x09
    assert state["step"] == 0x05


def test_cursor_state_voice_unknown_for_non_canonical_selector() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ADDR_VOICE_SELECTOR] = 0x77  # garbage
    state = cursor_state(bm)  # type: ignore[arg-type]
    assert state["voice"] is None


# ---- position_cursor -----------------------------------------------------


def test_position_cursor_writes_all_writer_vars_and_verifies() -> None:
    bm = RecordingBinMon()
    position_cursor(bm, voice=2, step=7)  # type: ignore[arg-type]
    assert bm.mem_map[ADDR_VOICE_SELECTOR] == VOICE_SELECTOR_VALUES[2]
    assert bm.mem_map[ADDR_CURSOR_STEP] == 7
    assert bm.mem_map[ADDR_WRITE_COUNT] == 1
    assert bm.mem_map[ADDR_WRITE_STRIDE] == 1
    assert bm.mem_map[ADDR_RANGE_FILL] == 0
    assert bm.mem_map[ADDR_SUPER_FLAGS] == 0
    assert bm.mem_map[ADDR_DIGIT_PHASE] == 0


def test_position_cursor_can_skip_digit_phase_reset() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ADDR_DIGIT_PHASE] = 0x07
    position_cursor(bm, voice=0, step=0, reset_digit_phase=False)  # type: ignore[arg-type]
    assert bm.mem_map[ADDR_DIGIT_PHASE] == 0x07  # untouched


def test_position_cursor_rejects_bad_voice() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="voice must be 0/1/2"):
        position_cursor(bm, voice=3, step=0)  # type: ignore[arg-type]


def test_position_cursor_rejects_bad_step() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="step must be 0..31"):
        position_cursor(bm, voice=0, step=32)  # type: ignore[arg-type]


def test_position_cursor_verify_raises_when_writes_drop() -> None:
    # Simulate a BinMon whose mem_set silently drops voice selector writes.
    bm = RecordingBinMon()

    def lossy_mem_set(addr: int, data: bytes) -> None:
        if addr == ADDR_VOICE_SELECTOR:
            return  # drop
        bm.mem_writes.append((addr, bytes(data)))
        for i, b in enumerate(data):
            bm.mem_map[addr + i] = b

    bm.mem_set = lossy_mem_set  # type: ignore[assignment,method-assign]
    with pytest.raises(RuntimeError, match="verify failed"):
        position_cursor(bm, voice=1, step=2)  # type: ignore[arg-type]


# ---- set_field dispatch ---------------------------------------------------


def test_set_field_direct_writes_seqed_cell() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ARRANGER_BASE[0] + 0] = 0
    bm.mem_map[ADDR_PAT_BASE_LO + 0] = 0x00
    bm.mem_map[ADDR_PAT_BASE_HI + 0] = 0x1F
    res = set_field(bm, "seqed", voice=0, step=0, sub_field="note", value=0x42)  # type: ignore[arg-type]
    assert res.ok
    assert res.post_value == 0x42


def test_set_field_seqlist_writes_arranger_cell() -> None:
    bm = RecordingBinMon()
    res = set_field(bm, "seqlist", voice=1, step=3, sub_field="pattern_num", value=0x10)  # type: ignore[arg-type]
    assert res.ok
    assert bm.mem_map[ARRANGER_BASE[1] + 3] == 0x10


def test_set_field_seqlist_rejects_wrong_sub_field() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="must be 'pattern_num'"):
        set_field(bm, "seqlist", voice=0, step=0, sub_field="note", value=0)  # type: ignore[arg-type]


def test_set_field_seqlist_rejects_out_of_range_pattern_num() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="0..0x7F"):
        set_field(bm, "seqlist", voice=0, step=0, sub_field="pattern_num", value=0x80)  # type: ignore[arg-type]


def test_set_field_sidtab_raises_not_implemented() -> None:
    bm = RecordingBinMon()
    with pytest.raises(NotImplementedError, match="SidTab"):
        set_field(bm, "sidtab", voice=0, step=0, sub_field="anything", value=0)  # type: ignore[arg-type]


def test_set_field_disk_raises_not_implemented() -> None:
    bm = RecordingBinMon()
    with pytest.raises(NotImplementedError, match="disk_save_new"):
        set_field(bm, "disk", voice=0, step=0, sub_field="anything", value=0)  # type: ignore[arg-type]


def test_set_field_unknown_mode_raises() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="unknown mode"):
        set_field(bm, "nope", voice=0, step=0, sub_field="x", value=0)  # type: ignore[arg-type]


def test_set_field_seqed_rejects_voice_out_of_range_for_chord() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="voice must be 0..5"):
        set_field(
            bm,  # type: ignore[arg-type]
            "seqed",
            voice=6,
            step=0,
            sub_field="note",
            value=0x30,
            prefer_direct=False,
        )


def test_set_field_seqed_note_chord_rejects_unknown_byte() -> None:
    # Chord-driven note write only accepts NOTE_PATTERN_BYTES values.
    bm = RecordingBinMon()
    bm.mem_map[ARRANGER_BASE[0] + 0] = 0
    bm.mem_map[ADDR_PAT_BASE_LO + 0] = 0x00
    bm.mem_map[ADDR_PAT_BASE_HI + 0] = 0x1F
    with pytest.raises(NotImplementedError, match="only handles values"):
        set_field(
            bm,  # type: ignore[arg-type]
            "seqed",
            voice=0,
            step=0,
            sub_field="note",
            value=0x99,  # not in NOTE_PATTERN_BYTES
            prefer_direct=False,
        )


def test_set_field_seqed_chord_rejects_bad_sub_field() -> None:
    bm = RecordingBinMon()
    bm.mem_map[ARRANGER_BASE[0] + 0] = 0
    bm.mem_map[ADDR_PAT_BASE_LO + 0] = 0x00
    bm.mem_map[ADDR_PAT_BASE_HI + 0] = 0x1F
    with pytest.raises(ValueError, match="must be one of"):
        set_field(
            bm,  # type: ignore[arg-type]
            "seqed",
            voice=0,
            step=0,
            sub_field="bogus",
            value=0,
            prefer_direct=False,
        )


# ---- module-level constants -----------------------------------------------


def test_field_write_result_fields_are_populated() -> None:
    res = FieldWriteResult(ok=True, pre_value=0, post_value=1, method="direct_mem")
    assert res.ok and res.method == "direct_mem"


def test_voice_selector_table_matches_voice_from_selector_inverse() -> None:
    # Round-trip: VOICE_SELECTOR_VALUES[v] should map back to v via
    # VOICE_FROM_SELECTOR.
    for v, sel in enumerate(VOICE_SELECTOR_VALUES):
        assert fs.VOICE_FROM_SELECTOR[sel] == v
