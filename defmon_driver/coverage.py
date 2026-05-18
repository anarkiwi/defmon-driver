"""Coverage harvester for defMON live execution.

Two coverage layers, selected by ``granularity`` at construction time:

1. ``"byte"`` (default) — one CHECK_EXEC checkpoint per byte in [start, end].
   ``hit_count`` per checkpoint is monotone and immune to cpuhistory ring-
   buffer rollover, so the snapshot diff directly yields *every PC that
   executed at least once*. This unblocks dark regions (writer band
   $B3xx, dispatcher $844C, sidTAB $BB00-$BDFF, disk-menu $C1xx-$C4xx,
   encoder $7F00-$80FF) that the warp-mode player IRQ used to drown out
   in cpuhistory. Install is held under ``bm.halted()`` because each
   newly-installed watchpoint adds per-instruction comparison overhead
   to warp emulation between calls — auto-resumed installs are O(N²)
   vs ~11 s for the full 45,056-byte range under halt.

2. ``"page"`` — legacy: one checkpoint per 256-byte page. Cheap (~200 ms
   install) but gives only page-level resolution; the per-PC question
   "which bytes in this page are code?" requires cpuhistory drains,
   which the player IRQ pump can evict before drain.

The cpuhistory drain stays in ``measure()`` regardless of granularity;
its PC set is now secondary to ``executed_pcs`` (the byte-mode hits)
but the register values it captures are still useful for ad-hoc
investigation.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from .binmon import (
    CHECK_EXEC,
    MEMSPACE_MAIN,
    BinMon,
)

Granularity = Literal["byte", "page"]


@dataclass(frozen=True)
class ActionCoverage:
    """Coverage attributable to a single named action.

    ``page_hits`` is a sparse mapping (only pages whose hit_count
    advanced during this action) regardless of granularity. In byte
    mode it's aggregated from the per-byte deltas.

    ``executed_pcs`` is the set of distinct PCs whose per-byte
    hit_count advanced during this action. Empty in page mode.

    ``cpuhistory_pcs`` is the union of PCs in the post-action drains
    (immediate + post-settle). Subject to ring rollover — kept for
    register-state context only; for "did this PC run" use
    ``executed_pcs`` (byte mode) or ``pages_touched`` (either mode).
    """

    name: str
    page_hits: dict[int, int]
    total_hits: int
    cpuhistory_pcs: frozenset[int]
    cycles_elapsed: int
    history_records: int
    executed_pcs: frozenset[int] = field(default_factory=frozenset)

    @property
    def pages_touched(self) -> frozenset[int]:
        return frozenset(self.page_hits)


class Coverage:
    """Byte- or page-granular checkpoint harvester.

    Typical use::

        with Coverage(bm) as cov:               # byte-granular by default
            for name, fn in d.all_documented_actions():
                ac = cov.measure(fn, name, settle=0.2)
                ...

    Construction is cheap; ``install()`` (called by ``__enter__``) does
    the bulk work. In byte mode that's ~11 s over the default
    $1000-$BFFF band (45,056 checkpoints) under ``bm.halted()``. In
    page mode it's ~200 ms over the same range (176 checkpoints).
    """

    def __init__(
        self,
        bm: BinMon,
        start: int = 0x1000,
        end: int = 0xBFFF,
        granularity: Granularity = "byte",
        history_count: int = 0x1000,
        memspace: int = MEMSPACE_MAIN,
    ) -> None:
        if granularity not in ("byte", "page"):
            raise ValueError(f"unknown granularity: {granularity!r}")
        if granularity == "page":
            if start & 0xFF:
                raise ValueError(f"start ${start:04x} not page-aligned (page mode)")
            if (end + 1) & 0xFF:
                raise ValueError(f"end ${end:04x} not page-aligned (page mode)")
        if end <= start:
            raise ValueError(f"end ${end:04x} must exceed start ${start:04x}")
        if not 1 <= history_count <= 0xFFFF:
            raise ValueError(f"history_count out of range: {history_count}")
        self.bm = bm
        self.start = start
        self.end = end
        self.granularity: Granularity = granularity
        self.history_count = history_count
        self.memspace = memspace
        # In byte mode: addr -> checknum. In page mode: page (hi byte) -> checknum.
        self._key_to_checknum: dict[int, int] = {}

    @property
    def checkpoint_count(self) -> int:
        return len(self._key_to_checknum)

    @property
    def page_count(self) -> int:
        """Number of distinct 256-byte pages covered."""
        if self.granularity == "page":
            return len(self._key_to_checknum)
        return ((self.end & ~0xFF) - (self.start & ~0xFF)) // 0x100 + 1

    @property
    def page_ids(self) -> tuple[int, ...]:
        if self.granularity == "page":
            return tuple(sorted(self._key_to_checknum))
        # In byte mode, derive page set from address keys.
        pages = {a >> 8 for a in self._key_to_checknum}
        return tuple(sorted(pages))

    # ---- lifecycle ----------------------------------------------------

    def install(self) -> None:
        if self._key_to_checknum:
            raise RuntimeError("coverage already installed")
        if self.granularity == "byte":
            self._install_byte()
        else:
            self._install_page()

    def _install_byte(self) -> None:
        # Halted install: with N watchpoints, every emulated instruction does
        # ~N comparisons. Auto-resumed installs blow up to O(N²) — smoke
        # measured 560 s for 4096 cps (137 ms/cp). Halted installs run
        # socket-RTT-bound (~0.25 ms/cp → ~11 s for 45,056 cps).
        #
        # silent=True (asid-vice extension): VICE increments hit_count on
        # hit but emits no CHECKPOINT_INFO event and runs no per-hit
        # trace/disassemble. Coverage only needs the cumulative hit set,
        # which `checkpoint_list` reads via `snapshot_hits()`. Without
        # silent the per-hit event emission throttles VICE itself under
        # warp playback (~10⁷ events/s for 45K cps) and the resulting
        # backlog wedges the binmon pipeline.
        with self.bm.halted():
            for addr in range(self.start, self.end + 1):
                cp = self.bm.checkpoint_set(
                    start=addr,
                    end=addr,
                    op=CHECK_EXEC,
                    stop_when_hit=False,
                    enabled=True,
                    temporary=False,
                    memspace=self.memspace,
                    silent=True,
                )
                self._key_to_checknum[addr] = cp.checknum

    def _install_page(self) -> None:
        addr = self.start
        while addr <= self.end:
            page_end = min(addr + 0xFF, self.end)
            cp = self.bm.checkpoint_set(
                start=addr,
                end=page_end,
                op=CHECK_EXEC,
                stop_when_hit=False,
                enabled=True,
                temporary=False,
                memspace=self.memspace,
                silent=True,
            )
            self._key_to_checknum[addr >> 8] = cp.checknum
            addr += 0x100

    def remove(self, *, drop_only: bool = False) -> None:
        """Delete every checkpoint we installed. Idempotent.

        Held under ``bm.halted()`` because per-delete VICE-side work
        is non-trivial; smoke measured 62 s for 4096 deletes under halt
        (15 ms/delete vs 0.25 ms/install — VICE's checkpoint table
        delete path is slower than its insert path).

        ``drop_only=True`` skips the VICE round-trips entirely and only
        clears the Python-side checknum map. Use this when the VICE
        process is about to be torn down (the container is about to
        exit) — the checkpoints die with VICE, and the 45K serial
        deletes under byte-granular coverage otherwise wedge for 30+
        min behind the warp-playback event backlog."""
        if not self._key_to_checknum:
            return
        if drop_only:
            self._key_to_checknum.clear()
            return
        with self.bm.halted():
            for checknum in list(self._key_to_checknum.values()):
                try:
                    self.bm.checkpoint_delete(checknum)
                except Exception:  # noqa: BLE001
                    pass
        self._key_to_checknum.clear()

    def __enter__(self) -> "Coverage":
        self.install()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.remove()

    # ---- snapshots ----------------------------------------------------

    def snapshot_hits(self) -> dict[int, int]:
        """Read every installed checkpoint's cumulative hit_count in one
        batched ``checkpoint_list`` call.

        Returned dict key is **address** in byte mode, **page (hi byte)**
        in page mode. ``diff_hits`` and downstream consumers don't care
        about the key meaning, but ``measure()`` derives ``page_hits``
        / ``executed_pcs`` from the granularity-dependent shape.

        ~50 ms for the default $1000-$BFFF page range (176 entries);
        ~250 ms for the byte-granular equivalent (45,056 entries). Both
        are a single binmon round-trip.
        """
        checknum_to_key = {cn: k for k, cn in self._key_to_checknum.items()}
        out: dict[int, int] = {}
        for cp in self.bm.checkpoint_list():
            key = checknum_to_key.get(cp.checknum)
            if key is None:
                continue  # foreign checkpoint; not ours
            out[key] = cp.hit_count
        if len(out) != len(self._key_to_checknum):
            seen = set(out)
            missing = set(self._key_to_checknum) - seen
            label = "address" if self.granularity == "byte" else "page"
            raise RuntimeError(
                f"checkpoint_list returned {len(out)} of our "
                f"{len(self._key_to_checknum)} installed checkpoints; "
                f"missing {label}s: "
                f"{sorted(f'0x{k:04x}' for k in missing)[:5]}"
                f"{'...' if len(missing) > 5 else ''}"
            )
        return out

    def diff_hits(
        self,
        before: dict[int, int],
        after: dict[int, int],
    ) -> dict[int, int]:
        """Return key -> positive delta. Pages/bytes with zero/negative
        delta are dropped. VICE hit_counts are monotone — negative
        shouldn't happen but we defend against checkpoint table churn."""
        out: dict[int, int] = {}
        for key, b in before.items():
            d = after.get(key, 0) - b
            if d > 0:
                out[key] = d
        return out

    # ---- measure -------------------------------------------------------

    def measure(
        self,
        action: Callable[[], Any],
        name: str,
        settle: float = 0.2,
    ) -> ActionCoverage:
        """Snapshot before, run action, drain cpuhistory, settle, drain again.

        ``settle``: seconds of wall time to wait after ``action()`` returns
        before the after-snapshot. defMON actions queue work in the
        background; warp x64sc burns plenty of cycles in 200 ms. Pass a
        larger value (e.g. 0.6) for play/stop transitions that need an
        IRQ frame to settle.

        Byte mode: ``executed_pcs`` is the authoritative PC set; the two
        cpuhistory drains are kept for register-state inspection but do
        not contribute to coverage.

        Page mode: ``executed_pcs`` is empty; ``cpuhistory_pcs`` is the
        only PC-granular signal (subject to ring rollover).
        """
        if not self._key_to_checknum:
            raise RuntimeError("coverage not installed; call install() first")
        before = self.snapshot_hits()
        before_hist = self.bm.cpuhistory_get(count=1, memspace=self.memspace)
        cycle_before = before_hist[0].cycle if before_hist else 0

        action()
        immediate_hist = self.bm.cpuhistory_get(
            count=self.history_count, memspace=self.memspace
        )
        if settle > 0:
            time.sleep(settle)

        after_hist = self.bm.cpuhistory_get(
            count=self.history_count, memspace=self.memspace
        )
        after = self.snapshot_hits()

        diff = self.diff_hits(before, after)
        if self.granularity == "byte":
            executed_pcs = frozenset(diff)
            page_hits: dict[int, int] = {}
            for addr, h in diff.items():
                page = addr >> 8
                page_hits[page] = page_hits.get(page, 0) + h
        else:
            executed_pcs = frozenset()
            page_hits = dict(diff)
        total_hits = sum(page_hits.values())

        cpu_pcs = frozenset(rec.pc for rec in immediate_hist) | frozenset(
            rec.pc for rec in after_hist
        )

        last_rec = (
            after_hist[-1]
            if after_hist
            else (immediate_hist[-1] if immediate_hist else None)
        )
        cycle_after = last_rec.cycle if last_rec else cycle_before
        cycles_elapsed = max(0, cycle_after - cycle_before)

        return ActionCoverage(
            name=name,
            page_hits=page_hits,
            total_hits=total_hits,
            cpuhistory_pcs=cpu_pcs,
            cycles_elapsed=cycles_elapsed,
            history_records=len(immediate_hist) + len(after_hist),
            executed_pcs=executed_pcs,
        )

    def measure_idle(self, duration: float = 0.5) -> ActionCoverage:
        """Baseline: hit deltas across ``duration`` seconds with no action.

        Reveals defMON's always-on PCs (editor main loop, IRQ handler)
        so callers can subtract them from per-action coverage if desired.
        """
        return self.measure(lambda: None, "<idle>", settle=duration)


def aggregate(actions: Iterable[ActionCoverage]) -> dict[int, int]:
    """Sum page_hits across many ActionCoverage records. Returned dict is
    page -> total hit count over the inputs."""
    out: dict[int, int] = {}
    for ac in actions:
        for page, hits in ac.page_hits.items():
            out[page] = out.get(page, 0) + hits
    return out


def union_pcs(actions: Iterable[ActionCoverage]) -> frozenset[int]:
    """Union of distinct PCs across many ActionCoverage records. Uses
    ``executed_pcs`` when populated (byte mode); falls back to
    ``cpuhistory_pcs`` (page mode)."""
    pcs: set[int] = set()
    for ac in actions:
        pcs |= ac.executed_pcs if ac.executed_pcs else ac.cpuhistory_pcs
    return frozenset(pcs)
