"""Unit tests for defmon_driver.keys — name → (row, col) resolution and
text → chord conversion. No emulator required."""

from __future__ import annotations

import pytest

from defmon_driver import keys
from defmon_driver.keys import (
    chord_to_keys,
    lookup,
    text_to_chords,
)


def test_lookup_canonical_keys() -> None:
    # Row 0 col 1 is RETURN on every C64 keymap reference.
    assert lookup("RETURN") == (0, 1)
    # SPACE is row 7 col 4.
    assert lookup("SPACE") == (7, 4)


def test_lookup_is_case_insensitive() -> None:
    assert lookup("return") == lookup("RETURN")
    assert lookup("Space") == (7, 4)


def test_lookup_aliases() -> None:
    # Aliases must resolve to the same (row, col) as their canonical name.
    assert lookup("LSHIFT") == lookup("SHIFT")
    assert lookup("BACKSPACE") == lookup("DEL")
    assert lookup("DEL") == lookup("INSTDEL")


def test_lookup_unknown_raises() -> None:
    with pytest.raises(KeyError):
        lookup("DEFINITELY_NOT_A_KEY")


def test_chord_to_keys_round_trips() -> None:
    chord = chord_to_keys("LSHIFT", "X")
    assert chord == [lookup("LSHIFT"), lookup("X")]
    assert len(chord) == 2


def test_text_to_chords_letters_and_digits() -> None:
    chords = text_to_chords("ABC123")
    assert chords == [("A",), ("B",), ("C",), ("1",), ("2",), ("3",)]


def test_text_to_chords_lowercase_folded_to_upper() -> None:
    # Lowercase typing is folded so defMON's upper/graphics charset
    # renders glyphs correctly.
    assert text_to_chords("hi") == text_to_chords("HI")


def test_text_to_chords_shifted_punctuation() -> None:
    # '!' is SHIFT+1, '?' is SHIFT+SLASH.
    assert text_to_chords("!?") == [("LSHIFT", "1"), ("LSHIFT", "SLASH")]


def test_text_to_chords_unmappable_raises() -> None:
    with pytest.raises(KeyError):
        # Backtick has no matrix path on the C64 layout.
        text_to_chords("`")


def test_key_class_has_attributes() -> None:
    # KEY.X and KEY.LSHIFT should be set as attributes by package import.
    assert hasattr(keys.KEY, "X")
    assert hasattr(keys.KEY, "LSHIFT")
    assert hasattr(keys.KEY, "RETURN")
    # Each attribute is a (row, col) tuple.
    assert keys.KEY.RETURN == (0, 1)
