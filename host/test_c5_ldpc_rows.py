"""Execute C5 copied row-pass source against independent word recurrences.

This checks bounded clear/accumulate calls, continuation across call boundaries,
AAPCS registers, guarded matrix bounds and maximum 32-byte stack. No device use.
"""
from __future__ import annotations

from pathlib import Path
import random

from test_bch128_asm import END, MASK, operands, register, registers

ROOT = Path(__file__).resolve().parents[1]


def program():
    source = [line.split("//")[0].strip() for line in
              (ROOT / "firmware/src/dvbs2_ldpc_c5_rows.S").read_text().splitlines()]
    code, labels = [], {}

    def emit(lines):
        cursor = 0
        while cursor < len(lines):
            line = lines[cursor]
            cursor += 1
            if not line:
                continue
            if line.startswith(".rept "):
                body = []
                while lines[cursor] != ".endr":
                    body.append(lines[cursor])
                    cursor += 1
                cursor += 1
                for _ in range(int(line.split()[1])):
                    emit(body)
            elif line.endswith(":"):
                labels[line[:-1]] = len(code)
            elif not line.startswith((".", "#")):
                code.append(line)
    emit(source)
    return code, labels


def execute(code, labels, entry, matrix, first, count):
    guard, data, stack = 0xCA85319D, [0xCA85319D, *matrix, 0xCA85319D], [0xA5A5A5A5] * 64

    def access(address, value=None):
        assert address % 4 == 0, hex(address)
        for base, values in ((0x100000, data), (0x200000, stack)):
            offset = (address - base) // 4
            if 0 <= offset < len(values):
                if value is not None:
                    values[offset] = value & MASK
                return values[offset]
        raise AssertionError(("unmapped access", hex(address)))

    r = [0x12340000 + i * 0x111 for i in range(16)]
    r[:2] = [0x100004 + first * 48, count]
    r[13:] = [0x200100, END | 1, labels[entry]]
    saved, zero, steps = r[4:12].copy(), False, 0

    def value(arg):
        return int(arg[1:], 0) if arg.startswith("#") else r[register(arg)]

    while r[15] != END:
        assert steps < 10000
        line = code[r[15]]
        r[15] += 1
        steps += 1
        op, tail = line.split(maxsplit=1)
        args = operands(tail)
        if op in ("push", "pop"):
            selected = registers(args[0])
            if op == "push":
                r[13] -= 4 * len(selected)
                for i, k in enumerate(selected):
                    access(r[13] + 4 * i, r[k])
            else:
                for i, k in enumerate(selected):
                    r[k] = access(r[13] + 4 * i)
                    if k == 15:
                        r[k] &= ~1
                r[13] += 4 * len(selected)
        elif op in ("ldm", "stm"):
            base = register(args[0].rstrip("!"))
            selected, address = registers(args[1]), r[base]
            for i, k in enumerate(selected):
                if op == "ldm":
                    r[k] = access(address + 4 * i)
                else:
                    access(address + 4 * i, r[k])
            if args[0].endswith("!"):
                r[base] += 4 * len(selected)
        elif op in ("mov", "movs"):
            r[register(args[0])] = value(args[1])
        elif op in ("sub", "subs", "eor"):
            dest = register(args[0])
            x, y = (r[dest], value(args[1])) if len(args) == 2 else (value(args[1]), value(args[2]))
            r[dest] = ((x ^ y) if op == "eor" else (x - y)) & MASK
            if op == "subs":
                zero = r[dest] == 0
        elif op == "bne":
            if not zero:
                r[15] = labels[args[0]]
        else:
            raise AssertionError(line)
    assert r[4:12] == saved and r[13] == 0x200100, "AAPCS damage"
    assert data[0] == data[-1] == guard, "matrix guard damage"
    assert stack[:-8] == [0xA5A5A5A5] * 56, "stack exceeds 32 bytes"
    return data[1:-1], steps


def main():
    code, labels = program()
    rng, cases, instructions = random.Random(235051), 0, 0
    for count in range(1, 9):
        for first in (0, 1, 7, 55, 64 - count):
            for pattern in (0, MASK, 0xAAAAAAAA, None):
                initial = [pattern if pattern is not None else rng.getrandbits(32) for _ in range(64 * 12)]
                got, steps = execute(code, labels, "dvbs2_ldpc_rows_c5_pic", initial, first, count)
                want = initial.copy()
                want[first * 12:(first + count) * 12] = [0] * (count * 12)
                assert got == want, ("clear", count, first, pattern)
                cases += 1
                instructions += steps
                accumulate_first = max(1, first)
                if accumulate_first + count > 64:
                    continue
                got, steps = execute(code, labels, "dvbs2_ldpc_accumulate_c5_pic", initial, accumulate_first, count)
                want = initial.copy()
                for row in range(accumulate_first, accumulate_first + count):
                    for word in range(12):
                        want[row * 12 + word] ^= want[(row - 1) * 12 + word]
                assert got == want, ("accumulate", count, first, pattern)
                # A bounded call must be equivalent to one row at a time. This
                # catches using pre-call previous rows after continuation.
                continued = initial.copy()
                for row in range(accumulate_first, accumulate_first + count):
                    continued, _ = execute(code, labels, "dvbs2_ldpc_accumulate_c5_pic", continued, row, 1)
                assert got == continued, ("continuation", count, first)
                cases += 1
                instructions += steps
    print(f"C5 LDPC copied row-pass semantics: {cases} cases/{instructions} instructions; "
          "1..8 rows, continuations, guards, AAPCS and 32-byte stack exact (no silicon timing)")


if __name__ == "__main__":
    main()
