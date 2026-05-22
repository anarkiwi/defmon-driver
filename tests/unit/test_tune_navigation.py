"""Unit tests for :mod:`defmon_driver.tune_navigation`.

Mocks the Defmon surface (open_disk_menu, tap, disk_load_by_index,
screen) to exercise cursor_load_tune's retry + verify logic without
spinning up a container.
"""

from __future__ import annotations

import pytest

from defmon_driver.defmon import DefmonError
from defmon_driver.tune_manifest import TuneEntry
from defmon_driver.tune_navigation import cursor_load_tune, state_reset


class StubScreen:
    """ScreenSnapshot-shaped stub: only ``text()`` is exercised."""

    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class StubDefmon:
    """Mock Defmon recording tap chords and yielding scripted screens."""

    def __init__(self, screens: list[str], fail_open: int = 0) -> None:
        self.taps: list[tuple[str, ...]] = []
        self.open_calls = 0
        self.load_calls = 0
        self._screens = list(screens)
        self._screen_index = 0
        self.fail_open_remaining = fail_open

    # ---- methods called by tune_navigation ---------------------------
    def open_disk_menu(self) -> None:
        self.open_calls += 1
        if self.fail_open_remaining > 0:
            self.fail_open_remaining -= 1
            raise DefmonError("simulated open_disk_menu failure")

    def tap(self, *names: str, **_kw) -> None:
        self.taps.append(tuple(names))

    def disk_load_by_index(self) -> None:
        self.load_calls += 1

    def screen(self) -> StubScreen:
        if self._screens:
            idx = min(self._screen_index, len(self._screens) - 1)
            self._screen_index += 1
            return StubScreen(self._screens[idx])
        return StubScreen("")


TUNE = TuneEntry(
    image="test.d64",
    name="Test",
    blocks=10,
    track=4,
    sector=0,
    author="Test",
    dir_index=3,
)


# ---- state_reset -----------------------------------------------------------


def test_state_reset_emits_super_exit_and_stop() -> None:
    d = StubDefmon(screens=[])
    state_reset(d)  # type: ignore[arg-type]
    # super_exit (CTRL+RETURN), then stop_playback (F7).
    assert d.taps == [("CTRL", "RETURN"), ("F7",)]


def test_state_reset_swallows_chord_exceptions() -> None:
    class FlakyDefmon(StubDefmon):
        def tap(self, *names: str, **_kw) -> None:  # noqa: ARG002
            raise RuntimeError("simulated chord failure")

    d = FlakyDefmon(screens=[])
    # Must not raise — both taps are wrapped in try/except.
    state_reset(d)  # type: ignore[arg-type]


# ---- cursor_load_tune ------------------------------------------------------


def test_cursor_load_tune_happy_path_emits_dir_index_walks() -> None:
    d = StubDefmon(screens=["VOC0 VOC1 VOC2 placeholder"])
    cursor_load_tune(d, TUNE, load_timeout=1.0)  # type: ignore[arg-type]
    # tune.dir_index == 3 → exactly 3 CRSRUD taps.
    assert d.taps == [("CRSRUD",)] * 3
    assert d.open_calls == 1
    assert d.load_calls == 1


def test_cursor_load_tune_retries_when_voc_markers_absent() -> None:
    # Two non-VOC screens (timeout-each) then a VOC screen on attempt 2.
    d = StubDefmon(
        screens=["BOOT SCREEN"] + ["VOC0"] * 5,
    )
    cursor_load_tune(d, TUNE, max_attempts=3, load_timeout=0.05)  # type: ignore[arg-type]
    assert d.open_calls >= 2  # at least one retry
    assert d.load_calls >= 2


def test_cursor_load_tune_recovers_after_open_disk_menu_failure() -> None:
    d = StubDefmon(screens=["VOC0"], fail_open=1)
    cursor_load_tune(d, TUNE, max_attempts=3, load_timeout=0.5)  # type: ignore[arg-type]
    # First attempt raises in open_disk_menu; second succeeds.
    assert d.open_calls == 2
    assert d.load_calls == 1


def test_cursor_load_tune_raises_after_exhausting_attempts() -> None:
    d = StubDefmon(screens=["BOOT SCREEN"] * 50)
    with pytest.raises(DefmonError, match="attempts failed"):
        cursor_load_tune(d, TUNE, max_attempts=2, load_timeout=0.05)  # type: ignore[arg-type]
    assert d.open_calls == 2  # max_attempts


def test_cursor_load_tune_swallows_screen_exceptions_during_wait() -> None:
    class FlakyScreenDefmon(StubDefmon):
        def __init__(self) -> None:
            super().__init__(screens=["VOC0"])
            self.screen_calls = 0

        def screen(self) -> StubScreen:
            self.screen_calls += 1
            if self.screen_calls == 1:
                raise RuntimeError("transient screen read failure")
            return super().screen()

    d = FlakyScreenDefmon()
    cursor_load_tune(d, TUNE, max_attempts=1, load_timeout=2.0)  # type: ignore[arg-type]
    # First screen call raised, second returned VOC0 — load succeeded.
    assert d.screen_calls >= 2
