"""Unit tests for defmon_driver.binmon constants + the
``_parse_checkpoint_info`` static parser. No socket is opened."""

from __future__ import annotations

import struct

import pytest

from defmon_driver.binmon import (
    API_VERSION,
    CHECK_EXEC,
    CHECK_LOAD,
    CHECK_STORE,
    MEMSPACE_DRIVE8,
    MEMSPACE_MAIN,
    OPCODE,
    RELEASE_NONE,
    RELEASE_OBSERVED,
    RELEASE_TIMEOUT,
    REQ_HEADER_LEN,
    RESP_HEADER_LEN,
    STX,
    TAP_MODE_FIXED,
    TAP_MODE_OBSERVED,
    BinMon,
    BinmonError,
)


def test_protocol_constants() -> None:
    # These are wire-protocol invariants; bumping them breaks compatibility
    # with the asid-vice binary monitor.
    assert STX == 0x02
    assert API_VERSION == 0x02
    assert REQ_HEADER_LEN == 11
    assert RESP_HEADER_LEN == 12


def test_checkpoint_op_flags_are_orthogonal() -> None:
    # CHECK_LOAD/STORE/EXEC are independent bits — must be combinable.
    assert CHECK_LOAD & CHECK_STORE == 0
    assert CHECK_LOAD & CHECK_EXEC == 0
    assert CHECK_STORE & CHECK_EXEC == 0
    combined = CHECK_LOAD | CHECK_STORE | CHECK_EXEC
    assert combined == 0x07


def test_tap_mode_constants() -> None:
    assert TAP_MODE_OBSERVED == 0
    assert TAP_MODE_FIXED == 1


def test_release_reason_constants() -> None:
    assert RELEASE_NONE == 0
    assert RELEASE_OBSERVED == 1
    assert RELEASE_TIMEOUT == 2


def test_memspace_constants() -> None:
    assert MEMSPACE_MAIN == 0
    assert MEMSPACE_DRIVE8 == 1


def test_opcodes_match_protocol() -> None:
    # A representative sample. These IDs are dictated by asid-vice's
    # monitor_binary.c and must not be renumbered.
    assert OPCODE.MEM_GET == 0x01
    assert OPCODE.MEM_SET == 0x02
    assert OPCODE.CHECKPOINT_SET == 0x12
    assert OPCODE.KEYMATRIX_TAP == 0x75
    assert OPCODE.KEYMATRIX_GET == 0x76
    assert OPCODE.SCREEN_GET == 0x77


def _build_checkpoint_body(
    *,
    checknum: int = 7,
    hit: bool = False,
    start: int = 0x1000,
    end: int = 0xBFFF,
    stop_when_hit: bool = False,
    enabled: bool = True,
    op: int = CHECK_EXEC,
    temporary: bool = False,
    hit_count: int = 0,
    ignore_count: int = 0,
    has_condition: bool = False,
    memspace: int = MEMSPACE_MAIN,
) -> bytes:
    body = bytearray(23)
    struct.pack_into("<I", body, 0, checknum)
    body[4] = 1 if hit else 0
    struct.pack_into("<H", body, 5, start)
    struct.pack_into("<H", body, 7, end)
    body[9] = 1 if stop_when_hit else 0
    body[10] = 1 if enabled else 0
    body[11] = op
    body[12] = 1 if temporary else 0
    struct.pack_into("<I", body, 13, hit_count)
    struct.pack_into("<I", body, 17, ignore_count)
    body[21] = 1 if has_condition else 0
    body[22] = memspace
    return bytes(body)


def test_parse_checkpoint_info_round_trip() -> None:
    body = _build_checkpoint_body(
        checknum=12,
        start=0x4000,
        end=0x4FFF,
        op=CHECK_LOAD | CHECK_STORE,
        hit_count=42,
        enabled=True,
        stop_when_hit=True,
        memspace=MEMSPACE_MAIN,
    )
    cp = BinMon._parse_checkpoint_info(body)
    assert cp.checknum == 12
    assert cp.start == 0x4000
    assert cp.end == 0x4FFF
    assert cp.op == CHECK_LOAD | CHECK_STORE
    assert cp.hit_count == 42
    assert cp.enabled is True
    assert cp.stop_when_hit is True
    assert cp.hit is False
    assert cp.memspace == MEMSPACE_MAIN


def test_parse_checkpoint_info_short_body_raises() -> None:
    with pytest.raises(BinmonError):
        BinMon._parse_checkpoint_info(b"\x00" * 10)


def test_parse_checkpoint_info_hit_flag() -> None:
    body = _build_checkpoint_body(hit=True)
    cp = BinMon._parse_checkpoint_info(body)
    assert cp.hit is True
