"""Check C5's actual PIC group source against independent bit extraction.

The checked model expands the assembly macros and executes instruction semantics.
It covers the full 64-row x 361-rotation x four-input-alignment domain, repeated
edges, guard regions and AAPCS state. Instruction counts are not silicon timing.
"""
from __future__ import annotations

import random

from test_ldpc_asm import MASK, execute, program, reference


def main():
    code, labels = program(c5=True)
    rng, cases, instructions = random.Random(235005), 0, 0

    def check(info, shift, initial, rows, offsets):
        nonlocal cases, instructions
        actual, steps = execute(code, labels, info, shift, initial, rows, offsets, c5=True)
        assert actual == reference(info, shift, initial, rows, offsets), (shift, rows[:4], offsets[:4])
        cases += 1
        instructions += steps

    # Every addressable concurrent-workspace row sees every rotation at all
    # four byte alignments. Repeated edges must XOR correctly into the same row.
    for shift in (0, 8, 16, 24):
        for row in range(64):
            pattern = (0, MASK, 0xAAAAAAAA, None)[row % 4]
            info = [pattern if pattern is not None else rng.getrandbits(32) for _ in range(13)]
            initial = [rng.getrandbits(32) for _ in range(64 * 12)]
            check(info, shift, initial, [row] * 361, list(range(361)))
        for pattern in (0, MASK, 0xAAAAAAAA, None):
            info = [pattern if pattern is not None else rng.getrandbits(32) for _ in range(13)]
            for count in (0, 1, 4, 12, 361):
                offsets = list(range(361)) if count == 361 else [rng.randrange(361) for _ in range(count)]
                rows = [rng.randrange(64) for _ in offsets]
                initial = [rng.getrandbits(32) for _ in range(64 * 12)]
                check(info, shift, initial, rows, offsets)

    # Decode counts separately from timing. The aligned C5 edge executes
    # 12 bursts transferring 36 words: 35 decoded instructions become at
    # least 59 execution cycles if a burst takes exactly one cycle per word.
    counts = []
    for c5 in (False, True):
        leaf, symbols = program(c5=c5)
        initial, info = [0] * 12, [MASK] * 13
        for shift in (0, 8, 16, 24):
            _, overhead = execute(leaf, symbols, info, shift, initial, [], [], c5=c5)
            edges = []
            for rotation in (0, 1):
                _, steps = execute(leaf, symbols, info, shift, initial, [0], [rotation], c5=c5)
                edges.append(steps - overhead)
            counts.append((overhead, *edges))
    assert counts == [(200, 60, 72)] * 4 + [(77, 35, 72), (79, 35, 72),
                                                  (100, 35, 72), (99, 35, 72)], counts
    print(f"C5 LDPC assembly semantics: {cases} cases/{instructions} instructions; "
          "64 rows x 361 rotations x four alignments, repeated edges, guards, AAPCS and "
          "136-byte stack exact. C4 -> C5 setup/return decoded instructions "
          "200 -> 77/79/100/99; shifted edge remains 72. Aligned edge "
          "60 -> 35 decoded instructions / at least 59 word-transfer cycles "
          "under a one-cycle-per-word burst assumption (no silicon timing)")


if __name__ == "__main__":
    main()
