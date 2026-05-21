"""Read defMON's ``$0F90`` keycode LUT from a live container and dump it as JSON.

Boots defMON in a one-shot container, mem_gets the 64-byte matrix-slot
LUT at ``$0F90``, decodes it (slot = ``row*8 + (7-col)``), and writes
the resulting ``{key_name: $0E44_value}`` mapping as JSON suitable for
:func:`defmon_driver.keycode_table.load_table`. Modifier keys
(``$FF``) and voice-mute keys (``$FE``) are excluded.

Usage::

    python -m defmon_driver.bootstrap_keycodes \\
        --d64 /path/to/defmon.d64 \\
        --out keycode_table.json
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from pathlib import Path

from ._smoke_support import smoke_session
from .keycode_table import bootstrap_keycodes, save_table

log = logging.getLogger("bootstrap_keycodes")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--port", type=int, default=6711)
    p.add_argument("--d64", required=True, help="path to a bootable defMON d64 image")
    p.add_argument("--out", default="keycode_table.json")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with smoke_session(
            Path(args.d64),
            port=args.port,
            prefix="bootstrap-keycodes-",
            connect_timeout=15.0,
            connect_attempts=120,
            wait_timeout=120.0,
        ) as s:
            table = bootstrap_keycodes(s.bm, s.d)
            save_table(out_path, table)

            print()
            print(f"== captured {len(table)} keycodes -> {out_path}")
            for name in sorted(table):
                print(f"  {name:<12s} 0x{table[name]:02X}")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        print(f"FATAL: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
