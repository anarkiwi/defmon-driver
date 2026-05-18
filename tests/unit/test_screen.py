"""Unit tests for defmon_driver.screen — SCREEN_GET parser + screencode
→ ASCII mapping. No emulator required; synthetic payloads are built
in-process to exercise the parser."""

from __future__ import annotations

import struct

import pytest

from defmon_driver.screen import (
    CHARSET_BYTES,
    CHARSET_ROM_LOWER,
    CHARSET_ROM_UPPER,
    COLOR_BYTES,
    HEADER_BYTES,
    PAYLOAD_BYTES,
    SCREEN_BYTES,
    parse_screen_response,
    screencode_to_ascii,
)


def _build_screen_body(
    screen: bytes,
    color: bytes | None = None,
    charset: bytes | None = None,
    charset_kind: int = CHARSET_ROM_UPPER,
) -> bytes:
    """Build a minimum-viable SCREEN_GET response body."""
    if len(screen) != SCREEN_BYTES:
        raise AssertionError(f"screen must be {SCREEN_BYTES} bytes")
    color_b: bytes = color if color is not None else b"\x00" * COLOR_BYTES
    charset_b: bytes = charset if charset is not None else b"\x00" * CHARSET_BYTES

    header = bytearray(HEADER_BYTES)
    header[0] = 0  # vic_mode = text
    header[1] = 25  # rows
    header[2] = 40  # cols
    header[3] = charset_kind
    header[4] = 0  # vic_bank
    header[5] = 14  # border colour
    header[6:10] = b"\x06\x00\x00\x00"  # bg colour quad
    header[10] = 0x1B  # d011
    header[11] = 0x08  # d016
    header[12] = 0x15  # d018
    header[13] = 0
    struct.pack_into("<H", header, 14, 0x0400)
    struct.pack_into("<H", header, 16, 0xD000)
    struct.pack_into("<H", header, 18, 0)
    struct.pack_into("<I", header, 20, PAYLOAD_BYTES)
    return bytes(header) + screen + color_b + charset_b


def test_parse_screen_response_round_trips_dimensions() -> None:
    screen = b" " * SCREEN_BYTES
    snap = parse_screen_response(_build_screen_body(screen))
    assert snap.rows == 25
    assert snap.cols == 40
    assert snap.charset_kind == CHARSET_ROM_UPPER
    assert snap.payload_len == PAYLOAD_BYTES
    assert len(snap.screen) == SCREEN_BYTES
    assert len(snap.color) == COLOR_BYTES
    assert len(snap.charset) == CHARSET_BYTES


def test_parse_screen_response_short_header_raises() -> None:
    with pytest.raises(ValueError):
        parse_screen_response(b"\x00" * (HEADER_BYTES - 1))


def test_parse_screen_response_short_payload_raises() -> None:
    # Header announces full payload but body cuts off.
    header = bytearray(HEADER_BYTES)
    struct.pack_into("<I", header, 20, PAYLOAD_BYTES)
    truncated = bytes(header) + b"\x00" * (SCREEN_BYTES // 2)
    with pytest.raises(ValueError):
        parse_screen_response(truncated)


def test_screencode_upper_letters() -> None:
    # Upper/graphics ROM: codes 1..26 map to A..Z.
    for i, ch in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ", start=1):
        assert screencode_to_ascii(i, CHARSET_ROM_UPPER) == ch


def test_screencode_lower_letters() -> None:
    # Upper/lower ROM: codes 1..26 map to a..z.
    for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz", start=1):
        assert screencode_to_ascii(i, CHARSET_ROM_LOWER) == ch
    # Codes 65..90 in lowercase ROM are the shifted uppercase letters.
    assert screencode_to_ascii(65, CHARSET_ROM_LOWER) == "A"


def test_screencode_digits_and_punctuation() -> None:
    # Codes $30..$39 are digits in both charsets.
    for i in range(10):
        assert screencode_to_ascii(0x30 + i, CHARSET_ROM_UPPER) == str(i)
    # SPACE is $20.
    assert screencode_to_ascii(0x20) == " "
    # '!' is $21.
    assert screencode_to_ascii(0x21) == "!"


def test_screencode_reverse_video_folds() -> None:
    # High bit set should mirror the unshifted glyph.
    assert screencode_to_ascii(0x81, CHARSET_ROM_UPPER) == "A"


def test_screencode_unmappable_returns_dot() -> None:
    # Code 0x60 is in the graphics range — should fall through to '.'.
    assert screencode_to_ascii(0x60, CHARSET_ROM_UPPER) == "."


def test_text_render_renders_top_left_message() -> None:
    # Put "HELLO" at the start of row 0 (codes 8, 5, 12, 12, 15 in
    # upper-charset screencodes).
    screen = bytearray(b" " * SCREEN_BYTES)
    for col, code in enumerate((8, 5, 12, 12, 15)):
        screen[col] = code
    body = _build_screen_body(bytes(screen))
    snap = parse_screen_response(body)
    assert snap.lines()[0].startswith("HELLO")
    assert snap.contains("HELLO")
    assert snap.find_text("HELLO") == (0, 0)
