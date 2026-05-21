"""Unit tests for Expect / verify() and Defmon.inject's pre-dispatch
validation. No emulator required — uses a stub BinMon that only
implements the calls these paths exercise."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from defmon_driver.defmon import Defmon, DefmonError
from defmon_driver.keyhandler import Expect, verify


@dataclass
class FakeBinMon:
    """Minimal BinMon stand-in: scripts a sequence of byte values
    returned from mem_get(addr, addr) at the verify-loop poll address."""

    sequence: list[int] = field(default_factory=list)
    addr: int = 0x715D
    reads: int = 0

    def mem_get(self, start: int, end: int) -> bytes:
        # verify() only reads single bytes at expect.addr.
        assert start == end == self.addr, f"unexpected read at {start:#x}..{end:#x}"
        idx = min(self.reads, len(self.sequence) - 1)
        self.reads += 1
        return bytes([self.sequence[idx]])


def test_verify_exact_match_first_read() -> None:
    bm = FakeBinMon(sequence=[0x42])
    ok, observed = verify(bm, Expect(addr=0x715D, want=0x42, timeout=0.1))  # type: ignore[arg-type]
    assert ok is True
    assert observed == 0x42
    assert bm.reads == 1


def test_verify_exact_match_after_polls() -> None:
    bm = FakeBinMon(sequence=[0, 0, 0x42])
    ok, observed = verify(
        bm,  # type: ignore[arg-type]
        Expect(addr=0x715D, want=0x42, timeout=1.0, poll_interval=0.01),
    )
    assert ok is True
    assert observed == 0x42
    # Initial read + 2 polled reads.
    assert bm.reads >= 3


def test_verify_callable_predicate() -> None:
    bm = FakeBinMon(sequence=[0xAA, 0xBB])
    seen: list[int] = []

    def pred(v: int) -> bool:
        seen.append(v)
        return v == 0xBB

    ok, observed = verify(
        bm,  # type: ignore[arg-type]
        Expect(addr=0x715D, want=pred, timeout=1.0, poll_interval=0.01),
    )
    assert ok is True
    assert observed == 0xBB
    assert seen == [0xAA, 0xBB]


def test_verify_timeout_returns_false_with_last_value() -> None:
    bm = FakeBinMon(sequence=[0x11])  # never matches 0x42
    start = time.monotonic()
    ok, observed = verify(
        bm,  # type: ignore[arg-type]
        Expect(addr=0x715D, want=0x42, timeout=0.08, poll_interval=0.02),
    )
    elapsed = time.monotonic() - start
    assert ok is False
    assert observed == 0x11
    # We waited at least the timeout but not pathologically longer.
    assert 0.07 <= elapsed < 0.5


def test_verify_predicate_can_express_advanced_from_prior() -> None:
    # "advanced off the previous value" is the cycle_sid_high_byte pattern.
    bm = FakeBinMon(sequence=[0x10, 0x10, 0x20])
    ok, observed = verify(
        bm,  # type: ignore[arg-type]
        Expect(
            addr=0x715D,
            want=lambda v: v != 0x10,
            timeout=1.0,
            poll_interval=0.01,
        ),
    )
    assert ok is True
    assert observed == 0x20


# ---- Defmon.inject pre-dispatch validation ----------------------------------
#
# These exercise the pre-press-via-loop guards. We don't go all the way
# into press_via_loop (that needs a real CPU + checkpoint round-trip);
# the FakeBinMon below is sufficient to reach the validation branches
# inject() raises from.


@dataclass
class ModeOnlyBinMon:
    """BinMon stub that returns a configurable mode byte from $7167."""

    mode_byte: int = 0x01  # default to MODE_SEQED so current_mode_name -> 'seqed'

    def mem_get(self, start: int, end: int) -> bytes:
        if start == end == 0x7167:
            return bytes([self.mode_byte])
        raise AssertionError(f"unexpected mem_get({start:#x}, {end:#x})")


def test_inject_rejects_pure_modifier_chord() -> None:
    d = Defmon(ModeOnlyBinMon())  # type: ignore[arg-type]
    with pytest.raises(DefmonError, match="pure-modifier"):
        d.inject("CTRL")


def test_inject_rejects_unknown_mode() -> None:
    # $7167 = 0x99 → current_mode_name returns 'unknown:$99'; inject
    # must surface that with a clear "pass mode=" message rather than
    # letting press_via_loop raise a generic ValueError downstream.
    d = Defmon(ModeOnlyBinMon(mode_byte=0x99))  # type: ignore[arg-type]
    with pytest.raises(DefmonError, match="pass mode="):
        d.inject("Z")


def test_inject_rejects_unknown_mode_passed_explicitly() -> None:
    d = Defmon(ModeOnlyBinMon())  # type: ignore[arg-type]
    with pytest.raises(DefmonError, match="pass mode="):
        d.inject("Z", mode="splash")


def test_inject_rejects_unknown_key_with_keycode_table_pointer() -> None:
    # COLON is a real C64 key but maps to LUT sentinel $FE (voice-mute
    # modifier — $0E42, not $0E44) so it's excluded from STATIC_KEYCODES.
    # The error must point at bootstrap, not a silent failure deeper in
    # dispatch.
    d = Defmon(ModeOnlyBinMon())  # type: ignore[arg-type]
    with pytest.raises(KeyError, match="bootstrap"):
        d.inject("COLON")
