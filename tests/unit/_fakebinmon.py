"""Recording BinMon stub shared across the offline unit suite.

Captures keymatrix_tap chords, satisfies mem_get reads against a
scripted byte map, and returns a deterministic ``keymatrix_get`` outcome
so :meth:`defmon.Defmon.tap` exits its release loop immediately.

The stub does NOT model the real CIA1 sampling state — tests that care
about retry/quiesce sequencing should keep using the specialised fakes
in ``test_inject.py`` / ``test_tap_retry.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class FakeKMOutcome:
    """Mimics the asid-vice keymatrix_get response."""

    release_reason: int = 1  # any non-zero value satisfies wait_release
    cia1_reads_total: int = 0
    cia1_reads_sampling: int = 0


def _make_blank_screen_bytes(screen: bytes | None = None) -> bytes:
    """24-byte header + 1000 screen + 1000 color + 2048 charset.

    ``screen`` overrides the 1000-byte screen-RAM region (left-padded with
    spaces / right-padded with zeros to 1000 bytes). The header is the
    minimum required by ``parse_screen_response``: payload_len at h[20:24].
    """
    import struct

    if screen is None:
        screen_bytes = b"\x20" * 1000  # screencode 0x20 = space
    else:
        screen_bytes = (screen + b"\x20" * 1000)[:1000]
    payload = screen_bytes + b"\x00" * 1000 + b"\x00" * 2048  # screen + color + charset
    header = bytearray(24)
    header[1] = 25  # rows
    header[2] = 40  # cols
    struct.pack_into("<H", header, 14, 0x0400)  # screen_addr
    struct.pack_into("<H", header, 16, 0x1000)  # charset_addr
    struct.pack_into("<I", header, 20, len(payload))  # payload_len
    return bytes(header) + payload


def make_screen_bytes(lines: list[str]) -> bytes:
    """Build a SCREEN_GET response whose decoded ``text()`` matches the
    supplied lines. Each line is padded/truncated to 40 chars; lines are
    rendered in the upper/graphics charset (A..Z → screencodes 1..26).
    """
    screen = bytearray(1000)
    for r, line in enumerate(lines[:25]):
        padded = (line.ljust(40))[:40]
        for c, ch in enumerate(padded):
            code = _ascii_to_screencode(ch)
            screen[r * 40 + c] = code
    return _make_blank_screen_bytes(bytes(screen))


def _ascii_to_screencode(ch: str) -> int:
    """Inverse of vice_driver.screen.screencode_to_ascii for the printable
    subset used by the test fixtures (uppercase letters, digits, common
    punctuation in the upper/graphics charset)."""
    if "A" <= ch <= "Z":
        return ord(ch) - ord("A") + 1
    if "0" <= ch <= "9":
        return ord(ch)  # 0x30..0x39 round-trips
    # Common punctuation in the upper/graphics charset:
    mapping = {
        " ": 0x20,
        "!": 0x21,
        '"': 0x22,
        "#": 0x23,
        "$": 0x24,
        "%": 0x25,
        "&": 0x26,
        "'": 0x27,
        "(": 0x28,
        ")": 0x29,
        "*": 0x2A,
        "+": 0x2B,
        ",": 0x2C,
        "-": 0x2D,
        ".": 0x2E,
        "/": 0x2F,
        ":": 0x3A,
        ";": 0x3B,
        "<": 0x3C,
        "=": 0x3D,
        ">": 0x3E,
        "?": 0x3F,
        "@": 0x00,
    }
    return mapping.get(ch, 0x20)


@dataclass
class RecordingBinMon:
    """Captures every method call against a BinMon-shaped object.

    Provided so unit tests can assert *which chord* a Defmon wrapper
    emitted without spinning up a real container. Default mem_get/screen
    behaviour is a constant zero / blank-screen response; tests that
    care about specific bytes pass ``mem_map`` (address → byte).
    """

    taps: list[tuple[str, ...]] = field(default_factory=list)
    mem_map: dict[int, int] = field(default_factory=dict)
    mem_writes: list[tuple[int, bytes]] = field(default_factory=list)
    screens: list[bytes] = field(default_factory=list)
    screen_index: int = 0
    keymatrix_outcomes: list[FakeKMOutcome] = field(default_factory=list)
    # Tests can install a "side effect" that mutates state per tap (e.g.
    # set mem_map values after a play_from_cursor was issued).
    on_tap: Callable[["RecordingBinMon", tuple[str, ...]], None] | None = None
    halted_calls: int = 0
    exit_calls: int = 0
    checkpoint_set_calls: list[dict[str, Any]] = field(default_factory=list)
    checkpoint_delete_calls: list[int] = field(default_factory=list)
    register_sets: list[dict[int, int]] = field(default_factory=list)
    register_state: dict[int, int] = field(default_factory=dict)
    run_until_pc_calls: list[tuple[int, float]] = field(default_factory=list)
    run_until_pc_raises: bool = False
    run_until_pc_hook: Callable[["RecordingBinMon", int], None] | None = None

    # ---- keymatrix -------------------------------------------------------
    def keymatrix_tap(self, rc, mode=None, frames=None):  # noqa: ARG002
        # ``rc`` is a list of (row, col); the test cares about which
        # symbolic names produced it. The Defmon.tap caller passed
        # ``names`` (the symbolic chord) into _tap_once before reducing
        # to rc; tests assert against ``taps`` populated via the
        # ``on_tap`` callback set up by :meth:`record_taps`. Here we
        # simply record the rc list so a test can inspect it directly.
        self.taps.append(tuple(f"r{r}c{c}" for r, c in rc))

    def keymatrix_get(self):
        if self.keymatrix_outcomes:
            return self.keymatrix_outcomes.pop(0)
        return FakeKMOutcome()

    # ---- memory ----------------------------------------------------------
    def mem_get(self, start: int, end: int) -> bytes:
        if start == end:
            return bytes([self.mem_map.get(start, 0)])
        return bytes([self.mem_map.get(a, 0) for a in range(start, end + 1)])

    def mem_set(self, addr: int, data: bytes) -> None:
        self.mem_writes.append((addr, bytes(data)))
        for i, b in enumerate(data):
            self.mem_map[addr + i] = b

    # ---- screen ----------------------------------------------------------
    def screen_get(self) -> bytes:
        if self.screens:
            idx = min(self.screen_index, len(self.screens) - 1)
            self.screen_index += 1
            return self.screens[idx]
        return _make_blank_screen_bytes()

    # ---- CPU / connection ------------------------------------------------
    def exit(self) -> None:
        self.exit_calls += 1

    def halted(self):
        # Context-manager stub: yields nothing, restores nothing.
        bm = self

        class _Ctx:
            def __enter__(self_inner):
                bm.halted_calls += 1
                return self_inner

            def __exit__(self_inner, *_exc):
                return False

        return _Ctx()

    # ---- checkpoints (minimal) ------------------------------------------
    def checkpoint_set(self, *args, **kw):
        # Both shapes used in the codebase: positional addr (single-byte
        # exec checkpoint) and the kwarg-only start/end/op form.
        if args:
            kw.setdefault("start", args[0])
            kw.setdefault("end", args[0])
        self.checkpoint_set_calls.append(kw)

        @dataclass
        class _CP:
            checknum: int = 1
            start: int = kw.get("start", 0)
            end: int = kw.get("end", 0)
            op: int = kw.get("op", 0)
            enabled: bool = kw.get("enabled", True)
            hit_count: int = 0

        return _CP()

    def checkpoint_delete(self, checknum: int) -> None:
        self.checkpoint_delete_calls.append(checknum)

    # ---- CPU registers ---------------------------------------------------
    def registers_set(self, values: dict[int, int]) -> None:
        self.register_sets.append(dict(values))
        self.register_state.update(values)

    def registers_get(self) -> dict[int, int]:
        return dict(self.register_state)

    def run_until_pc(self, target_pc: int, timeout: float = 2.0) -> None:
        self.run_until_pc_calls.append((target_pc, timeout))
        if self.run_until_pc_raises:
            raise RuntimeError("simulated run_until_pc timeout")
        if self.run_until_pc_hook is not None:
            self.run_until_pc_hook(self, target_pc)
        # Pretend the CPU reached the target.
        self.register_state[3] = target_pc  # REG_PC == 3 in vice_driver.binmon


def make_recording_defmon(
    mem_map: dict[int, int] | None = None,
) -> tuple["Any", RecordingBinMon]:
    """Wire a :class:`RecordingBinMon` into a real :class:`Defmon` and
    install a ``names``-recording hook on ``tap``.

    Returns (defmon_instance, recording_bm). After every call to
    ``defmon.tap(*names)`` the symbolic chord is appended to
    ``recording_bm.taps`` (overwriting the rc-form that the raw stub
    records — tests only care about the symbolic chord).
    """
    from defmon_driver.defmon import Defmon

    bm = RecordingBinMon()
    if mem_map:
        bm.mem_map.update(mem_map)
    d = Defmon(bm)  # type: ignore[arg-type]
    original_tap = d.tap

    def recording_tap(*names: str, **kw):
        bm.taps.append(tuple(names))
        # Replace the rc-form record above with the symbolic names.
        # Defmon.tap will internally call keymatrix_tap, which appends
        # the rc-form; we then pop that synthetic entry.
        result = original_tap(*names, **kw)
        if (
            len(bm.taps) >= 2
            and isinstance(bm.taps[-1], tuple)
            and bm.taps[-1]
            and bm.taps[-1][0].startswith("r")
        ):
            bm.taps.pop()
        return result

    d.tap = recording_tap  # type: ignore[method-assign]
    return d, bm
