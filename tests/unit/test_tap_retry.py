"""Unit tests for the retry+verify layer on Defmon.tap.

No emulator required — uses a fake BinMon that scripts the byte
sequence returned from mem_get(expect.addr, …) and counts how many
keymatrix_tap calls were made.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from defmon_driver.defmon import Defmon, DefmonError
from defmon_driver.keyhandler import Expect


@dataclass
class FakeKMOutcome:
    release_reason: int = 1  # non-zero so wait_release returns quickly
    cia1_reads_total: int = 0
    cia1_reads_sampling: int = 0


@dataclass
class FakeBinMon:
    """Scripts mem_get($715D) values and counts taps.

    After each successful keymatrix_tap the scripted sequence advances
    by one — so retry behaviour can be tested by setting up a sequence
    like [stale, stale, want] and asserting the third tap is the one
    that satisfied the predicate.
    """

    addr: int = 0x715D
    sequence: list[int] = field(default_factory=list)
    taps: int = 0
    quiesce_chords: list[tuple[str, ...]] = field(default_factory=list)

    def keymatrix_tap(self, rc, mode=None, frames=None):  # noqa: ARG002
        self.taps += 1

    def keymatrix_get(self):
        return FakeKMOutcome()

    def mem_get(self, start: int, end: int) -> bytes:
        if start == end == self.addr:
            # Each verify() call reads mem; advance the sequence by tap.
            idx = min(self.taps - 1 if self.taps else 0, len(self.sequence) - 1)
            return bytes([self.sequence[idx]])
        # Unrelated read (e.g. inside _stereo_quiesce): not under test here.
        raise AssertionError(f"unexpected mem_get({start:#x}, {end:#x})")


def _defmon_with(sequence: list[int]) -> tuple[Defmon, FakeBinMon]:
    bm = FakeBinMon(sequence=sequence)
    return Defmon(bm), bm  # type: ignore[arg-type]


def test_tap_with_expect_returns_on_first_match() -> None:
    d, bm = _defmon_with([0x01])
    out = d.tap(
        "CTRL",
        "LSHIFT",
        "LEFTARROW",
        expect=Expect(addr=0x715D, want=0x01, timeout=0.5),
        max_retries=3,
        settle=0,
        wait_release=False,
    )
    assert out.release_reason == 1
    assert bm.taps == 1  # no retry needed


def test_tap_with_expect_retries_until_match() -> None:
    # First two taps "miss" (byte still 0); third tap succeeds.
    d, bm = _defmon_with([0x00, 0x00, 0x01])
    d.tap(
        "CTRL",
        "LSHIFT",
        "LEFTARROW",
        expect=Expect(addr=0x715D, want=0x01, timeout=0.1, poll_interval=0.01),
        max_retries=3,
        settle=0,
        wait_release=False,
    )
    assert bm.taps == 3


def test_tap_with_expect_raises_on_exhausted_retries() -> None:
    d, bm = _defmon_with([0x00, 0x00, 0x00])
    with pytest.raises(DefmonError, match="after 3 attempts.*final 0x00"):
        d.tap(
            "CTRL",
            "LSHIFT",
            "LEFTARROW",
            expect=Expect(addr=0x715D, want=0x01, timeout=0.05, poll_interval=0.01),
            max_retries=3,
            settle=0,
            wait_release=False,
        )
    assert bm.taps == 3


def test_tap_without_expect_does_not_retry() -> None:
    d, bm = _defmon_with([0x00])
    d.tap(
        "CTRL",
        "LSHIFT",
        "LEFTARROW",
        max_retries=5,  # ignored when expect is None
        settle=0,
        wait_release=False,
    )
    assert bm.taps == 1


def test_tap_rejects_zero_max_retries() -> None:
    d, _ = _defmon_with([0x01])
    with pytest.raises(ValueError, match="max_retries"):
        d.tap("Z", max_retries=0)


def test_tap_callable_predicate_for_advance_off_prior() -> None:
    # "advanced off prior" is the _cycle_until pattern: tap until the
    # byte changes from its starting value.
    prior = 0xD4
    d, bm = _defmon_with([0xD4, 0xD4, 0xD5])
    d.tap(
        "CTRL",
        "LSHIFT",
        "UPARROW",
        expect=Expect(
            addr=0x715D,
            want=lambda v, _p=prior: v != _p,
            timeout=0.05,
            poll_interval=0.01,
        ),
        max_retries=3,
        settle=0,
        wait_release=False,
    )
    assert bm.taps == 3
