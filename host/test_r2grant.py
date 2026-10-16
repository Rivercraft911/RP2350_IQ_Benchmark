"""Native finite count lists plus independent four-reader DMA/PIO order model.

This checks representation and finite-prefix safety, not RP2350 arbitration or
grant-seam timing. All tests run offline and never open a device.
"""
from __future__ import annotations

import ctypes
from collections import deque
from pathlib import Path
import random
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
BATCH, RING = 512, 4096


def source(axis, word):
    return ((0xA5C30000 if axis else 0x5A3C0000) ^ (word * 0x9E3779B9)) & 0xFFFFFFFF


class Reader:
    """One independent normal-mode input -> reload -> input chain.

    A reload read and its trigger write are distinct events. Source words are
    read only under a nonzero grant; after an abort an already-read final word
    may retire, but all completion chains have been disabled before retirement.
    """
    def __init__(self, counts, axis, words, rng):
        self.counts, self.axis, self.words, self.rng = counts, axis, words, rng
        self.count_cursor = self.cursor = self.remaining = 0
        self.tx, self.received = deque(), []
        self.pending = None
        self.reload_value = None
        self.reload_due = 0
        self.null = self.aborted = False
        self.pulls = self.stalls = 0

    def step(self, cycle):
        if self.pending is not None:
            value, due = self.pending
            if cycle >= due and len(self.tx) < 4:
                self.tx.append(value)
                self.pending = None
                self.remaining -= 1
        if not self.aborted:
            if self.reload_value is not None and cycle >= self.reload_due:
                self.remaining = self.reload_value
                self.null = self.reload_value == 0
                self.reload_value = None
            if not self.null and self.remaining == 0 and self.pending is None and self.reload_value is None:
                assert self.count_cursor < len(self.counts), "read past sentinel"
                self.reload_value = self.counts[self.count_cursor]
                self.count_cursor += 1
                # Independent delay models a heavily skewed reload, beyond the
                # FIFO runway too: safety must survive even when timing fails.
                self.reload_due = cycle + self.rng.randrange(1, 800)
            if self.remaining and self.pending is None and len(self.tx) < 4:
                assert self.cursor < sum(self.counts), "read beyond published prefix"
                self.pending = (source(self.axis, self.cursor), cycle + self.rng.randrange(1, 5))
                self.cursor += 1
        if cycle % 128 == 0 and self.pulls < self.words:
            if self.tx:
                self.received.append(self.tx.popleft())
                self.pulls += 1
            else:
                self.stalls += 1

    def abort(self):
        self.aborted = True
        self.reload_value = None


def verify_native(lib):
    lib.r2grant_plan.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_uint, ctypes.c_uint]
    lib.r2grant_plan.restype = ctypes.c_bool
    lib.r2grant_source.argtypes = [ctypes.c_uint, ctypes.c_uint]
    lib.r2grant_source.restype = ctypes.c_uint32
    for axis in (0, 1):
        for i in range(3 * RING):
            assert lib.r2grant_source(axis, i) == source(axis, i)
    canary = 0xDEADBABE
    checked = 0
    for words in range(RING + 1025):
        for capacity in range(10):
            array = (ctypes.c_uint32 * 11)(*[canary] * 11)
            ok = lib.r2grant_plan(ctypes.cast(ctypes.byref(array, 4), ctypes.POINTER(ctypes.c_uint32)),
                                 capacity, words)
            want = words <= RING and words % BATCH == 0 and capacity >= words // BATCH + 1
            assert ok == want, (words, capacity)
            expected = [canary] * 11
            if want:
                expected[1:words // BATCH + 2] = [BATCH] * (words // BATCH) + [0]
            assert list(array) == expected, (words, capacity)
            checked += 1
    assert not lib.r2grant_plan(None, 9, RING)
    assert not lib.r2grant_plan((ctypes.c_uint32 * 9)(), 9, 0xFFFFFFFF)
    return checked


def verify_chains(lib):
    total, aborts = 0, 0
    for batches in range(1, 9):
        words = batches * BATCH
        for seed in range(4):
            counts = (ctypes.c_uint32 * 9)()
            assert lib.r2grant_plan(counts, 9, words)
            plan = list(counts)[:batches + 1]
            readers = [Reader(plan, j & 1, words, random.Random(100 * seed + j)) for j in range(4)]
            # Each parity reads the complete axis, with independent progress.
            for cycle in range((words + 64) * 128):
                for reader in readers:
                    reader.step(cycle)
                if all(r.null and len(r.received) == words for r in readers):
                    break
            assert all(r.null and r.count_cursor == batches + 1 for r in readers)
            for j, reader in enumerate(readers):
                assert reader.cursor == words
                assert reader.received == [source(j & 1, k) for k in range(words)]
                total += words
            # Null is stable under arbitrarily long CPU absence: no extra read
            # or replay after the final controller read/write completes.
            cursors = [(r.cursor, r.count_cursor) for r in readers]
            for cycle in range(cycle + 1, cycle + 2048):
                for reader in readers:
                    reader.step(cycle)
            assert cursors == [(r.cursor, r.count_cursor) for r in readers]
        # Abort at offsets around count/data seams, including an in-flight read.
        for when in (0, 1, 511 * 128, 512 * 128, words * 128 // 2):
            reader = Reader(plan, 1, words, random.Random(when))
            for cycle in range(when + 1):
                reader.step(cycle)
            reader.abort()
            cursor, count_cursor = reader.cursor, reader.count_cursor
            for cycle in range(when + 1, when + 4096):
                reader.step(cycle)
            assert (reader.cursor, reader.count_cursor) == (cursor, count_cursor)
            assert reader.received == [source(1, k) for k in range(len(reader.received))]
            aborts += 1
    # A shortened published prefix on just the final parity reader must halt
    # there despite the other three progressing. No shared completion barrier.
    counts = (ctypes.c_uint32 * 9)()
    assert lib.r2grant_plan(counts, 9, BATCH)
    reader = Reader(list(counts)[:2], 1, 2 * BATCH, random.Random(3))
    for cycle in range(2 * BATCH * 128 + 8192):
        reader.step(cycle)
    assert reader.null and reader.cursor == BATCH and reader.stalls
    assert len(reader.received) == BATCH
    return total, aborts


def main():
    with tempfile.TemporaryDirectory(prefix="r2grant-native-") as build:
        library = Path(build) / "r2grant.so"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        str(ROOT / "firmware/src/output/r2grant.c"), "-o", str(library)], check=True)
        lib = ctypes.CDLL(str(library))
        checked = verify_native(lib)
        words, aborts = verify_chains(lib)
    print(f"Passed R2 native prefix planner: {checked} guarded publication/capacity cases; "
          f"24576 independent C/Python source words; {words} four-reader exact echo words; "
          f"independent delayed reloads/null stop, {aborts} abort offsets, shortened-prefix control. "
          "No hardware timing inference; rolling publication/ring wrap are not implemented.")


if __name__ == "__main__":
    main()
