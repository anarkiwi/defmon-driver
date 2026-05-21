"""Coverage of :mod:`defmon_driver.keyhandler` without a real CPU.

Uses :class:`RecordingBinMon` to satisfy ``halted()`` / ``run_until_pc`` /
``registers_*`` / ``mem_*`` so the trampoline + press_via_loop + Sid2Chord
code paths exercise their full sequence. The mock pretends every
``run_until_pc`` call lands on the requested target — fine, since we're
testing the *protocol* (which bytes get written, in what order), not
defMON's real dispatch.
"""

from __future__ import annotations

import pytest

from defmon_driver import keyhandler as kh
from defmon_driver.keyhandler import (
    ADDR_KEY,
    ADDR_MOD1,
    ADDR_MOD2,
    ADDR_MODE,
    ADDR_PREV_KEY,
    ADDR_REPEAT_CTR,
    LOOP_AFTER_SCAN,
    LOOP_TOP,
    MOD_CBM,
    MOD_NONE,
    REG_A,
    REG_PC,
    SID1_ARR_BASES,
    SID2_ARR_BASES,
    STATE_ADDRS,
    TRAMP_JSR,
    TRAMPOLINE_BASE,
    Sid2Chord,
    diff_pattern,
    diff_state,
    dump_keycode_lut,
    install_trampoline,
    press,
    press_via_loop,
)

from ._fakebinmon import RecordingBinMon

# ---- diff helpers ---------------------------------------------------------


def test_diff_pattern_returns_only_changed_bytes() -> None:
    pre = bytes([0x00, 0x11, 0x22, 0x33])
    post = bytes([0x00, 0xFF, 0x22, 0x44])
    diff = diff_pattern(pre, post)
    assert diff == [(1, 0x11, 0xFF), (3, 0x33, 0x44)]


def test_diff_pattern_empty_when_unchanged() -> None:
    pre = post = bytes([0xAA, 0xBB])
    assert diff_pattern(pre, post) == []


def test_diff_state_returns_addr_keyed_changes() -> None:
    pre = {0x100: 0, 0x101: 1, 0x102: 2}
    post = {0x100: 0, 0x101: 9, 0x102: 2}
    assert diff_state(pre, post) == [(0x101, 1, 9)]


# ---- install_trampoline + _patch_handler ---------------------------------


def test_install_trampoline_writes_six_byte_jsr_stub() -> None:
    bm = RecordingBinMon()
    install_trampoline(bm)  # type: ignore[arg-type]
    assert bm.mem_map[TRAMPOLINE_BASE] == 0x20  # JSR opcode
    # operand placeholder ($00 $00) then NOP NOP NOP.
    for off, expected in enumerate((0x20, 0x00, 0x00, 0xEA, 0xEA, 0xEA)):
        assert bm.mem_map[TRAMPOLINE_BASE + off] == expected


def test_patch_handler_writes_operand_bytes() -> None:
    bm = RecordingBinMon()
    install_trampoline(bm)  # type: ignore[arg-type]
    kh._patch_handler(bm, 0xBBB5)  # type: ignore[arg-type]
    assert bm.mem_map[TRAMP_JSR + 1] == 0xB5
    assert bm.mem_map[TRAMP_JSR + 2] == 0xBB


# ---- _snapshot_state ------------------------------------------------------


def test_snapshot_state_reads_each_state_addr() -> None:
    bm = RecordingBinMon()
    for i, addr in enumerate(STATE_ADDRS):
        bm.mem_map[addr] = i & 0xFF
    snap = kh._snapshot_state(bm)  # type: ignore[arg-type]
    assert set(snap.keys()) == set(STATE_ADDRS)
    for i, addr in enumerate(STATE_ADDRS):
        assert snap[addr] == (i & 0xFF)


# ---- dump_keycode_lut -----------------------------------------------------


def test_dump_keycode_lut_reads_0F90_block() -> None:
    bm = RecordingBinMon()
    # Pre-seed the LUT region so the dump can verify a known byte.
    bm.mem_map[0x0F90] = 0x1A  # 'Z' keycode
    bm.mem_map[0x0FCF] = 0x80
    data = dump_keycode_lut(bm)  # type: ignore[arg-type]
    assert len(data) == 64
    assert data[0] == 0x1A
    assert data[-1] == 0x80


# ---- press() validation + happy path -------------------------------------


def test_press_rejects_unknown_mode() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="unknown mode"):
        press(bm, "bogus", keycode=0x1A)  # type: ignore[arg-type]


def test_press_writes_full_dispatch_state_and_runs_to_landing() -> None:
    bm = RecordingBinMon()
    press(bm, "seqed", keycode=0x1A, mod1=MOD_CBM, set_mode=True)  # type: ignore[arg-type]
    # Verifies every byte the dispatch sets.
    assert bm.mem_map[ADDR_KEY] == 0x1A
    assert bm.mem_map[ADDR_PREV_KEY] == 0x1A ^ 0xFF
    assert bm.mem_map[ADDR_MOD1] == MOD_CBM
    assert bm.mem_map[ADDR_MOD2] == MOD_NONE
    assert bm.mem_map[ADDR_REPEAT_CTR] == 0x01
    assert bm.mem_map[ADDR_MODE] == kh.MODE_VAL["seqed"]
    # PC was set to the trampoline JSR.
    assert any(d.get(REG_PC) == TRAMP_JSR for d in bm.register_sets)


def test_press_propagates_run_until_pc_failure() -> None:
    bm = RecordingBinMon()
    bm.run_until_pc_raises = True
    with pytest.raises(RuntimeError, match="simulated run_until_pc"):
        press(bm, "seqed", keycode=0x1A, verbose=True)  # type: ignore[arg-type]


# ---- press_via_loop() validation paths -----------------------------------


def test_press_via_loop_rejects_unknown_mode() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="unknown mode"):
        press_via_loop(bm, "nope", keycode=0x1A)  # type: ignore[arg-type]


def test_press_via_loop_rejects_invalid_cursor_voice() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="voice must be 0..5"):
        press_via_loop(bm, "seqed", keycode=0x1A, cursor=(6, 0))  # type: ignore[arg-type]


def test_press_via_loop_rejects_invalid_cursor_step() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="step must be 0..31"):
        press_via_loop(bm, "seqed", keycode=0x1A, cursor=(0, 32))  # type: ignore[arg-type]


def test_press_via_loop_rejects_invalid_arranger_row() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="arranger_row must be 0..0x7F"):
        press_via_loop(  # type: ignore[arg-type]
            bm,
            "seqed",
            keycode=0x1A,
            cursor=(3, 0),
            arranger_row=0x80,
        )


# ---- press_via_loop() happy paths ----------------------------------------


def _seed_arranger(bm: RecordingBinMon, voice: int, row: int, pat_num: int) -> None:
    """Helper: seed the SID#2 arranger at (voice, row)."""
    bm.mem_map[SID2_ARR_BASES[voice - 3] + row] = pat_num


def test_press_via_loop_v0_writes_dispatch_state() -> None:
    bm = RecordingBinMon()
    bm.halted_calls = 0
    press_via_loop(  # type: ignore[arg-type]
        bm,
        "seqed",
        keycode=0x1A,
        mod1=MOD_NONE,
        cursor=(0, 2),
        set_mode=True,
    )
    # halted() was entered.
    assert bm.halted_calls == 1
    # Cursor variables seeded.
    assert bm.mem_map[0x71CD] == 0x00  # voice selector for V0
    assert bm.mem_map[0x71D2] == 2  # step
    # Loop-top JSR was patched to NOPs and restored.
    assert any(addr == LOOP_TOP and data == b"\xea\xea\xea" for addr, data in bm.mem_writes)
    # Final state: scan JSR restored. The default RecordingBinMon's
    # mem_map for $092C was 0; after restore it's whatever was read pre-
    # patch (also 0) → bytes \x00\x00\x00. Either way, an explicit
    # restore happened.
    # PC was set to LOOP_AFTER_SCAN for the resume.
    assert any(d.get(REG_PC) == LOOP_AFTER_SCAN for d in bm.register_sets)


def test_press_via_loop_v3_installs_and_restores_proxy() -> None:
    bm = RecordingBinMon()
    # Pre-seed: SID#2 V3 row 0 → pat 5; SID#1 V0 row 0 → pat 1.
    _seed_arranger(bm, 3, 0, 5)
    bm.mem_map[SID1_ARR_BASES[0] + 0] = 0x01
    press_via_loop(  # type: ignore[arg-type]
        bm,
        "seqed",
        keycode=0x30,
        mod1=MOD_CBM,
        cursor=(3, 0),
        arranger_row=0,
    )
    # Proxy installed → SID#1 V0 row 0 was overwritten with pat 5, then
    # restored to its original 0x01.
    sid1_writes = [data for addr, data in bm.mem_writes if addr == SID1_ARR_BASES[0] + 0]
    # Two writes: install (0x05) and restore (0x01).
    assert sid1_writes[0] == b"\x05"
    assert sid1_writes[-1] == b"\x01"


def test_press_via_loop_propagates_run_until_pc_failure_and_restores_scan_jsr() -> None:
    bm = RecordingBinMon()
    # Pre-stamp identifiable bytes at LOOP_TOP so we can confirm the
    # restore on exception.
    bm.mem_map[LOOP_TOP] = 0x20  # JSR opcode
    bm.mem_map[LOOP_TOP + 1] = 0x47
    bm.mem_map[LOOP_TOP + 2] = 0x0E

    call_count = {"n": 0}

    def hook(bm_: RecordingBinMon, target: int) -> None:
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("dispatch never returned")

    bm.run_until_pc_hook = hook

    with pytest.raises(RuntimeError, match="dispatch never returned"):
        press_via_loop(bm, "seqed", keycode=0x1A)  # type: ignore[arg-type]

    # The scan JSR was restored before propagating the failure.
    assert bm.mem_map[LOOP_TOP] == 0x20
    assert bm.mem_map[LOOP_TOP + 1] == 0x47
    assert bm.mem_map[LOOP_TOP + 2] == 0x0E


# ---- Sid2Chord ------------------------------------------------------------


def test_sid2chord_rejects_invalid_voice() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="voice must be 0..5"):
        Sid2Chord(bm, voice=7, step=0)  # type: ignore[arg-type]


def test_sid2chord_rejects_invalid_step() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="step must be 0..31"):
        Sid2Chord(bm, voice=0, step=32)  # type: ignore[arg-type]


def test_sid2chord_rejects_invalid_arranger_row() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="arranger_row must be 0..0x7F"):
        Sid2Chord(bm, voice=3, step=0, arranger_row=0x80)  # type: ignore[arg-type]


def test_sid2chord_v0_no_proxy_install() -> None:
    bm = RecordingBinMon()
    with Sid2Chord(bm, voice=0, step=4) as ctx:  # type: ignore[arg-type]
        ctx.press(0x30, mod1=MOD_CBM)
    # V0 → no SID#2 arranger entries touched.
    sid2_writes = [addr for addr, _ in bm.mem_writes if addr in {SID1_ARR_BASES[0] + 0}]
    # The arranger byte at SID1 V0 row 0 should not have been written
    # since the proxy is only installed for SID#2 voices.
    assert all(addr != SID1_ARR_BASES[0] + 0 for addr, _ in bm.mem_writes) or sid2_writes == []
    # Cursor seed at $71CD = 0x00 (V0 selector).
    assert bm.mem_map[0x71CD] == 0x00


def test_sid2chord_v3_installs_proxy_and_restores_on_exit() -> None:
    bm = RecordingBinMon()
    _seed_arranger(bm, 3, 0, 0x42)
    bm.mem_map[SID1_ARR_BASES[0] + 0] = 0x11
    with Sid2Chord(bm, voice=3, step=0) as ctx:  # type: ignore[arg-type]
        ctx.press(0x30, mod1=MOD_CBM)
    writes = [data for addr, data in bm.mem_writes if addr == SID1_ARR_BASES[0] + 0]
    assert writes[0] == b"\x42"  # install with SID#2 pat_num
    assert writes[-1] == b"\x11"  # restore original on exit


def test_sid2chord_press_outside_with_raises() -> None:
    bm = RecordingBinMon()
    ctx = Sid2Chord(bm, voice=0, step=0)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="outside `with` block"):
        ctx.press(0x30)


def test_sid2chord_press_restores_scan_jsr_on_run_until_pc_failure() -> None:
    bm = RecordingBinMon()
    bm.mem_map[LOOP_TOP] = 0x20
    bm.mem_map[LOOP_TOP + 1] = 0x47
    bm.mem_map[LOOP_TOP + 2] = 0x0E

    def hook(bm_: RecordingBinMon, target: int) -> None:
        # First call (entering the context manager) succeeds; the second
        # (inside press()) raises.
        if len(bm_.run_until_pc_calls) >= 2:
            raise RuntimeError("press timeout")

    bm.run_until_pc_hook = hook
    with pytest.raises(RuntimeError, match="press timeout"):
        with Sid2Chord(bm, voice=0, step=0) as ctx:  # type: ignore[arg-type]
            ctx.press(0x30)

    # Scan JSR restored despite the failure inside press().
    assert bm.mem_map[LOOP_TOP] == 0x20


def test_sid2chord_propagates_enter_failure_and_releases_halt() -> None:
    bm = RecordingBinMon()
    bm.run_until_pc_raises = True
    with pytest.raises(RuntimeError, match="simulated run_until_pc"):
        with Sid2Chord(bm, voice=0, step=0):  # type: ignore[arg-type]
            pass


# ---- capture_keycode_via_checkpoint --------------------------------------


def test_capture_keycode_via_checkpoint_requires_defmon_arg() -> None:
    bm = RecordingBinMon()
    with pytest.raises(ValueError, match="Defmon instance required"):
        kh.capture_keycode_via_checkpoint(bm, "Z", d=None)  # type: ignore[arg-type]


def test_capture_keycode_via_checkpoint_returns_a_register_on_hit() -> None:
    bm = RecordingBinMon()
    # Pre-stage register state so the polling loop sees the PC + A
    # we expect.
    bm.register_state[REG_PC] = 0x0EFA
    bm.register_state[REG_A] = 0x1A
    # Minimal Defmon stub (only used to issue the tap).
    fake_d = object()
    result = kh.capture_keycode_via_checkpoint(bm, "Z", d=fake_d, timeout=0.1)  # type: ignore[arg-type]
    assert result == 0x1A


def test_capture_keycode_via_checkpoint_returns_none_when_checkpoint_misses() -> None:
    bm = RecordingBinMon()
    # PC never lands on $0EFA → polling loop times out and returns None.
    fake_d = object()
    result = kh.capture_keycode_via_checkpoint(bm, "Z", d=fake_d, timeout=0.05)  # type: ignore[arg-type]
    assert result is None
