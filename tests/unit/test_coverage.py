"""Unit tests for the pure-python helpers in defmon_driver.coverage.

Covers ``Coverage.diff_hits`` (which is a static-ish hit-delta computer),
``aggregate``, ``union_pcs``, and the constructor's input validation.
All BinMon interactions are mocked out — no socket / no docker."""

from __future__ import annotations

import pytest

from defmon_driver.coverage import ActionCoverage, Coverage, aggregate, union_pcs


def _cov() -> Coverage:
    """Build a Coverage without invoking install(). bm is unused for the
    pure helpers below; pass None and rely on the helpers not touching it."""
    return Coverage(bm=None, granularity="page")  # type: ignore[arg-type]


# ---- diff_hits --------------------------------------------------------


def test_diff_hits_returns_positive_deltas_only() -> None:
    cov = _cov()
    before = {0x10: 5, 0x11: 10, 0x12: 0}
    after = {0x10: 8, 0x11: 10, 0x12: 7}
    diff = cov.diff_hits(before, after)
    assert diff == {0x10: 3, 0x12: 7}


def test_diff_hits_drops_zero_deltas() -> None:
    cov = _cov()
    diff = cov.diff_hits({0x42: 100}, {0x42: 100})
    assert diff == {}


def test_diff_hits_drops_negative_deltas() -> None:
    # Negative deltas should not appear (hit_counts are monotone) but if
    # the table churns the helper defends.
    cov = _cov()
    diff = cov.diff_hits({0x42: 5}, {0x42: 3})
    assert diff == {}


def test_diff_hits_handles_missing_after_key() -> None:
    cov = _cov()
    # Key missing from after = treated as 0; negative delta = dropped.
    diff = cov.diff_hits({0x42: 1}, {})
    assert diff == {}


# ---- constructor validation ------------------------------------------


def test_constructor_rejects_unknown_granularity() -> None:
    with pytest.raises(ValueError, match="granularity"):
        Coverage(bm=None, granularity="wrong")  # type: ignore[arg-type]


def test_constructor_rejects_misaligned_start_in_page_mode() -> None:
    with pytest.raises(ValueError, match="page-aligned"):
        Coverage(bm=None, start=0x1001, end=0x10FF, granularity="page")  # type: ignore[arg-type]


def test_constructor_rejects_misaligned_end_in_page_mode() -> None:
    # end=0x10FE is not page-aligned (end+1 must have low byte 0).
    with pytest.raises(ValueError, match="page-aligned"):
        Coverage(bm=None, start=0x1000, end=0x10FE, granularity="page")  # type: ignore[arg-type]


def test_constructor_accepts_page_aligned_byte_mode() -> None:
    # Byte mode doesn't require page alignment.
    cov = Coverage(bm=None, start=0x1234, end=0x12FF, granularity="byte")  # type: ignore[arg-type]
    assert cov.granularity == "byte"


def test_constructor_rejects_end_le_start() -> None:
    with pytest.raises(ValueError, match="exceed"):
        Coverage(bm=None, start=0x2000, end=0x2000, granularity="byte")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exceed"):
        Coverage(bm=None, start=0x2000, end=0x1FFF, granularity="byte")  # type: ignore[arg-type]


def test_constructor_rejects_oob_history_count() -> None:
    with pytest.raises(ValueError, match="history_count"):
        Coverage(bm=None, history_count=0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="history_count"):
        Coverage(bm=None, history_count=0x10000)  # type: ignore[arg-type]


# ---- page_count / page_ids in byte mode without install ----------------


def test_page_count_in_byte_mode_spans_address_range() -> None:
    cov = Coverage(bm=None, start=0x1000, end=0x10FF, granularity="byte")  # type: ignore[arg-type]
    # One page in the range $10xx.
    assert cov.page_count == 1


def test_page_count_in_byte_mode_multi_page() -> None:
    cov = Coverage(bm=None, start=0x1000, end=0x12FF, granularity="byte")  # type: ignore[arg-type]
    # Pages $10, $11, $12.
    assert cov.page_count == 3


# ---- aggregate / union_pcs --------------------------------------------


def _ac(name: str, page_hits: dict[int, int], pcs: set[int]) -> ActionCoverage:
    return ActionCoverage(
        name=name,
        page_hits=page_hits,
        total_hits=sum(page_hits.values()),
        cpuhistory_pcs=frozenset(pcs),
        cycles_elapsed=0,
        history_records=0,
        executed_pcs=frozenset(pcs),
    )


def test_aggregate_sums_page_hits_across_actions() -> None:
    out = aggregate(
        [
            _ac("a", {0x10: 1, 0x20: 4}, set()),
            _ac("b", {0x10: 2, 0x30: 5}, set()),
        ]
    )
    assert out == {0x10: 3, 0x20: 4, 0x30: 5}


def test_aggregate_empty() -> None:
    assert aggregate([]) == {}


def test_union_pcs_prefers_executed_pcs() -> None:
    a = _ac("a", {}, {0x1234, 0x5678})
    b = _ac("b", {}, {0x1234, 0x9ABC})
    assert union_pcs([a, b]) == frozenset({0x1234, 0x5678, 0x9ABC})


def test_union_pcs_falls_back_to_cpuhistory_when_executed_empty() -> None:
    # executed_pcs empty (page mode); cpuhistory_pcs must still be used.
    ac = ActionCoverage(
        name="x",
        page_hits={},
        total_hits=0,
        cpuhistory_pcs=frozenset({0xAA, 0xBB}),
        cycles_elapsed=0,
        history_records=0,
        executed_pcs=frozenset(),
    )
    assert union_pcs([ac]) == frozenset({0xAA, 0xBB})


def test_action_coverage_pages_touched_view() -> None:
    ac = _ac("x", {0x10: 1, 0x42: 9}, set())
    assert ac.pages_touched == frozenset({0x10, 0x42})
