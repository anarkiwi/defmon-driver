"""Unit tests for defmon_driver.tune_manifest."""

from __future__ import annotations

from defmon_driver.tune_manifest import TUNES, TuneEntry, tunes_for_image


def test_tunes_are_unique_per_image() -> None:
    for image in {t.image for t in TUNES}:
        names = [t.name for t in tunes_for_image(image)]
        assert len(names) == len(set(names)), f"duplicate tune name in {image}: {names}"


def test_tunes_for_image_filters() -> None:
    out = tunes_for_image("defmon-withtunes.d64")
    assert all(t.image == "defmon-withtunes.d64" for t in out)
    assert len(out) > 0


def test_tunes_for_image_unknown_image() -> None:
    assert tunes_for_image("nonsense.d64") == ()


def test_dir_indices_are_strictly_increasing_per_image() -> None:
    # Within a single d64 image, dir_index must be strictly increasing
    # — otherwise the disk-menu cursor walk would loop back.
    for image in {t.image for t in TUNES}:
        idxs = [t.dir_index for t in tunes_for_image(image)]
        assert idxs == sorted(
            set(idxs)
        ), f"dir_index not strictly increasing in {image}: {idxs}"


def test_tune_entry_fields_present() -> None:
    for t in TUNES:
        assert isinstance(t, TuneEntry)
        assert t.name.startswith(".")
        assert 1 <= t.track <= 35
        assert 0 <= t.sector <= 20
        assert t.blocks > 0
        assert t.dir_index >= 0
