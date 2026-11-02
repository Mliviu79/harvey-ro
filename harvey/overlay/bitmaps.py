"""Register signals' firms kept per load: bitmaps over firm ids.

A cohort condition on a register signal (VAT registered, a turnover range,
filed accounts) is a set of firms in Harvey: those the register has a
value for, and of them those whose value reads yes. Worked out from the
registers on a page, each would be a walk of every key in Harvey (1.8M),
probed in the register, and sets of a million ids built in Python: seconds
on every first visit. So the register counts job, which walks them anyway
once per load of each register (register_tally), keeps them too
(``register_set``): one bitmap over firm ids per (signal, module, part),
compressed, a few hundred kB at most.

A page reads them as they are when nothing changed since, and patches
them when something did: a record added or given an identifier since the
walk (``firm_ident.since``) is read again from the register by its keys,
a merged record counts on its survivor, and a removed firm's bit is
cleared as it is removed (store.remove_company). A set kept for another
load of the register isn't used: the page works the set out from the
register instead, and the worker queues the job again
(reads.register_tally_stale), as it does once so many records changed
since that patching costs more than the walk (``PATCH_MAX``).
"""

from __future__ import annotations

import sys
import zlib
from array import array

# register_set.part: the records with a value, those whose value reads yes;
# a year (a register that keeps each year's figures, for a cohort range that
# names the year): the records whose statement that year has the figure.
VALUED, YES = 0, 1


def bits_of(ids) -> int:
    """The bitmap of a collection of ids."""
    ids = ids if isinstance(ids, (set, frozenset, list, tuple, array)) else list(ids)
    if not ids:
        return 0
    buf = bytearray((max(ids) >> 3) + 1)
    for i in ids:
        buf[i >> 3] |= 1 << (i & 7)
    return int.from_bytes(buf, "little")


def ids_of(bits: int) -> list[int]:
    """The ids a bitmap holds, lowest first."""
    if bits <= 0:
        return []
    size = ((bits.bit_length() + 63) // 64) * 8
    words = array("Q")
    words.frombytes(bits.to_bytes(size, "little"))
    if sys.byteorder == "big":
        words.byteswap()
    out: list[int] = []
    for n, word in enumerate(words):
        if not word:
            continue
        base = n << 6
        while word:
            low = word & -word
            out.append(base + low.bit_length() - 1)
            word ^= low
    return out


def pack(bits: int) -> bytes:
    return zlib.compress(bits.to_bytes((bits.bit_length() + 7) // 8, "little"), 6)


def unpack(blob: bytes) -> int:
    return int.from_bytes(zlib.decompress(blob), "little")


def clear(blob: bytes, record: int) -> bytes:
    """A packed bitmap with ``record``'s bit cleared."""
    return pack(unpack(blob) & ~(1 << record))


class Lookup:
    """Membership tests on a bitmap (a bytes copy: testing a bit of a big
    int would shift the whole of it)."""

    __slots__ = ("raw", "size")

    def __init__(self, bits: int):
        self.raw = bits.to_bytes((bits.bit_length() + 7) // 8, "little") if bits > 0 else b""
        self.size = len(self.raw)

    def __contains__(self, i: int) -> bool:
        at = i >> 3
        return at < self.size and (self.raw[at] >> (i & 7)) & 1 == 1
