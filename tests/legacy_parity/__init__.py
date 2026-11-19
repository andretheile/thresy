"""Parity with the old bot's export (``COACH_LEGACY_DIR``).

Inputs are chosen dynamically from whatever the export contains, and results are compared with
the old bot's own store (Parquet files and ``runs.db``), never with numbers written here.
"""

from __future__ import annotations

from collections.abc import Sequence

PICKS = ("first", "middle", "last")


def pick[T](items: Sequence[T], which: str) -> T:
    """The first, middle or last item of a sorted sequence."""
    index = {"first": 0, "middle": len(items) // 2, "last": len(items) - 1}[which]
    return items[index]
