"""Unit tests for :class:`defmon_driver.sidtab.SidTab`.

Uses a mock Defmon (only ``tap`` and ``enter_sidtab`` are needed) so
the navigation deltas and column-resolution logic can be exercised
without a running emulator.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from defmon_driver.sidtab import SidTab, SidTabError

CALIBRATION_SAMPLE: dict = {
    "tune": "TEST",
    "header_row": 4,
    "data_row": 6,
    "header_text": "JP DL WG AD SR",
    "cursor_entry_cell": [6, 3],
    "crsrlr_to_wrap": 24,
    "cells_in_order": [],
    "column_map": {
        "JP": {
            "first_screen_col": 3,
            "n_digits": 2,
            "nav_idx_to_first_digit": 0,
            "cell_cols": [3, 4],
        },
        "DL": {
            "first_screen_col": 6,
            "n_digits": 2,
            "nav_idx_to_first_digit": 2,
            "cell_cols": [6, 7],
        },
        "WG": {
            "first_screen_col": 9,
            "n_digits": 4,
            "nav_idx_to_first_digit": 4,
            "cell_cols": [9, 10, 11, 12],
        },
        "AD": {
            "first_screen_col": 14,
            "n_digits": 2,
            "nav_idx_to_first_digit": 8,
            "cell_cols": [14, 15],
        },
        "SR": {
            "first_screen_col": 17,
            "n_digits": 2,
            "nav_idx_to_first_digit": 10,
            "cell_cols": [17, 18],
        },
        "ACID": {
            "first_screen_col": 32,
            "n_digits": 4,
            "nav_idx_to_first_digit": 20,
            "cell_cols": [32, 33, 34, 35],
        },
    },
}


class MockDefmon:
    """Bare-bones Defmon stub recording tap chords and enter_sidtab calls."""

    def __init__(self) -> None:
        self.taps: list[tuple[str, ...]] = []
        self.entered_sidtab = 0

    def tap(self, *names: str, **_kw) -> None:
        self.taps.append(tuple(names))

    def enter_sidtab(self) -> None:
        self.entered_sidtab += 1


def _make() -> tuple[SidTab, MockDefmon]:
    d = MockDefmon()
    st = SidTab(d, CALIBRATION_SAMPLE)  # type: ignore[arg-type]
    return st, d


# ---- construction ----------------------------------------------------------


def test_constructor_loads_column_map() -> None:
    st, _ = _make()
    cols = st.columns()
    # Original columns plus WG_wave / WG_gate split.
    for needed in ("JP", "DL", "WG", "AD", "SR", "ACID", "WG_wave", "WG_gate"):
        assert needed in cols


def test_wg_is_split_into_wave_and_gate() -> None:
    st, _ = _make()
    wave = st.column("WG_wave")
    gate = st.column("WG_gate")
    assert wave.n_digits == 2
    assert wave.cell_cols == [9, 10]
    assert gate.n_digits == 2
    assert gate.cell_cols == [11, 12]
    # Gate's nav_idx is 2 past wave's (one cell per digit).
    assert gate.nav_idx_to_first_digit == wave.nav_idx_to_first_digit + 2


def test_constructor_skips_wg_split_when_wg_absent() -> None:
    cal = {**CALIBRATION_SAMPLE, "column_map": dict(CALIBRATION_SAMPLE["column_map"])}
    cal["column_map"].pop("WG")
    st = SidTab(MockDefmon(), cal)  # type: ignore[arg-type]
    assert "WG_wave" not in st.column_map
    assert "WG_gate" not in st.column_map


def test_from_calibration_reads_json(tmp_path: Path) -> None:
    p = tmp_path / "cal.json"
    p.write_text(json.dumps(CALIBRATION_SAMPLE))
    st = SidTab.from_calibration(MockDefmon(), p)  # type: ignore[arg-type]
    assert "JP" in st.columns()


# ---- entry / state ---------------------------------------------------------


def test_set_before_enter_raises() -> None:
    st, _ = _make()
    with pytest.raises(SidTabError, match="not entered"):
        st.set(row=0, column="JP", value=0x12)


def test_enter_resets_cursor_state() -> None:
    st, d = _make()
    st.enter()
    assert d.entered_sidtab == 1
    assert st.current_row == 0
    assert st.current_nav_idx == 0


# ---- column lookup ---------------------------------------------------------


def test_column_returns_info() -> None:
    st, _ = _make()
    ci = st.column("AD")
    assert ci.n_digits == 2
    assert ci.first_screen_col == 14


def test_column_unknown_raises() -> None:
    st, _ = _make()
    with pytest.raises(SidTabError, match="unknown column"):
        st.column("NOPE")


# ---- write path ------------------------------------------------------------


def test_set_writes_digits_high_to_low() -> None:
    st, d = _make()
    st.enter()
    st.set(row=0, column="JP", value=0xAB)
    # Cursor moves from nav_idx 0 (JP's first digit) — zero delta — then
    # types 'A' then 'B'.
    assert d.taps == [("A",), ("B",)]
    # Nav tracker advanced by JP.n_digits == 2.
    assert st.current_nav_idx == 2


def test_set_walks_to_target_column_right() -> None:
    st, d = _make()
    st.enter()
    # AD is at nav_idx 8 — 8 right-walks of CRSRLR.
    st.set(row=0, column="AD", value=0x00)
    crsrlr_count = sum(1 for t in d.taps if t == ("CRSRLR",))
    assert crsrlr_count == 8


def test_set_walks_left_when_shorter() -> None:
    st, d = _make()
    st.enter()
    # Place cursor at nav_idx 5; target ACID (nav_idx 20). Delta = +15;
    # abs(15) > wrap//2 (12) so the wrap-aware path flips to delta = -9
    # (left walk).
    st.current_nav_idx = 5
    d.taps.clear()
    st.set(row=0, column="ACID", value=0x0000)
    lefts = sum(1 for t in d.taps if t == ("LSHIFT", "CRSRLR"))
    rights = sum(1 for t in d.taps if t == ("CRSRLR",))
    assert lefts == 9 and rights == 0


def test_set_no_wrap_when_crsrlr_to_wrap_zero() -> None:
    # Calibration without crsrlr_to_wrap → never flips delta, always
    # walks the literal delta direction.
    cal = {**CALIBRATION_SAMPLE, "crsrlr_to_wrap": 0}
    st = SidTab(MockDefmon(), cal)  # type: ignore[arg-type]
    st.enter()
    st.current_nav_idx = 5
    d = st.d  # type: ignore[assignment]
    d.taps.clear()  # type: ignore[attr-defined]
    st.set(row=0, column="ACID", value=0x0000)  # delta = +15
    rights = sum(1 for t in d.taps if t == ("CRSRLR",))  # type: ignore[attr-defined]
    assert rights == 15


def test_set_walks_to_target_row() -> None:
    st, d = _make()
    st.enter()
    st.set(row=3, column="JP", value=0x00)
    downs = sum(1 for t in d.taps if t == ("CRSRUD",))
    assert downs == 3


def test_set_row_walks_up_when_negative_delta() -> None:
    st, d = _make()
    st.enter()
    st.current_row = 4
    d.taps.clear()
    st.set(row=1, column="JP", value=0x00)
    ups = sum(1 for t in d.taps if t == ("LSHIFT", "CRSRUD"))
    assert ups == 3


def test_set_supports_4_digit_value() -> None:
    st, d = _make()
    st.enter()
    st.set(row=0, column="ACID", value=0x1234)
    digit_taps = [t for t in d.taps if t in (("1",), ("2",), ("3",), ("4",))]
    assert digit_taps == [("1",), ("2",), ("3",), ("4",)]


def test_set_rejects_out_of_range_value() -> None:
    st, _ = _make()
    st.enter()
    with pytest.raises(SidTabError, match="out of range"):
        st.set(row=0, column="JP", value=0x100)  # 2-digit max is 0xFF


def test_set_rejects_unknown_column() -> None:
    st, _ = _make()
    st.enter()
    with pytest.raises(SidTabError, match="unknown column"):
        st.set(row=0, column="NOPE", value=0)


# ---- clear / goto ----------------------------------------------------------


def test_clear_taps_space_per_digit() -> None:
    st, d = _make()
    st.enter()
    st.clear(row=0, column="JP")
    spaces = sum(1 for t in d.taps if t == ("SPACE",))
    assert spaces == 2  # JP.n_digits


def test_clear_rejects_unknown_column() -> None:
    st, _ = _make()
    st.enter()
    with pytest.raises(SidTabError, match="unknown column"):
        st.clear(row=0, column="NOPE")


def test_goto_only_walks_no_digits() -> None:
    st, d = _make()
    st.enter()
    st.goto(row=2, column="DL")
    assert ("CRSRLR",) in d.taps  # nav delta = 2
    assert ("CRSRUD",) in d.taps  # row delta = 2
    # No digit keys typed.
    digits = [t for t in d.taps if len(t) == 1 and t[0] in "0123456789ABCDEF"]
    assert digits == []


def test_goto_rejects_unknown_column() -> None:
    st, _ = _make()
    st.enter()
    with pytest.raises(SidTabError, match="unknown column"):
        st.goto(row=0, column="NOPE")


# ---- column introspection --------------------------------------------------


def test_columns_returns_sorted_list() -> None:
    st, _ = _make()
    cols = st.columns()
    assert cols == sorted(cols)
