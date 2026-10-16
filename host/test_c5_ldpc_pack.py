"""Execute the copied C5 pack assembly against independent parity bit strings.

The model checks actual source, all q<=64, arbitrary partial output alignment,
both specialized gathers/packers, stack bounds and AAPCS state. It is instruction
semantics, not Arm execution or M33 timing. No device access is performed.
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import random
import re

from test_bch128_asm import operands, register, registers

ROOT = Path(__file__).resolve().parents[1]
MASK, END = 0xFFFFFFFF, 0xEEEEEEE0


def program():
    macros, code, labels = {}, [], {}
    name, params, body = None, [], []

    def emit(line):
        head = line.split(maxsplit=1)[0]
        if head in macros:
            names, lines = macros[head]
            args = operands(line.split(maxsplit=1)[1]) if names else []
            assert len(args) == len(names)
            for expanded in lines:
                for key, value in zip(names, args):
                    expanded = expanded.replace("\\" + key, value)
                emit(expanded)
        elif line.endswith(":"):
            assert line[:-1] not in labels
            labels[line[:-1]] = len(code)
        elif not line.startswith((".", "#")):
            code.append(line)

    for raw in (ROOT / "firmware/src/dvbs2_ldpc_c5.S").read_text().splitlines():
        line = raw.split("//")[0].strip()
        if not line:
            continue
        if line.startswith(".macro "):
            declaration = line.split(maxsplit=2)
            name = declaration[1]
            params = operands(declaration[2]) if len(declaration) > 2 else []
            body = []
        elif line == ".endm":
            macros[name] = params, body
            name = None
        elif name:
            body.append(line)
        else:
            emit(line)
    return code, labels


def execute(code, labels, matrix, prefix, q, k, initial, io):
    memory, writable = {}, set()
    guard, stack_base, stack_top = 0xCA85319D, 0x500000, 0x500400

    def region(base, words, write=False):
        for i, word in enumerate(words):
            at = base + i * 4
            memory[at] = word
            if write:
                writable.add(at)

    region(0x100000, matrix)
    region(0x200000, prefix)
    region(0x300000, [guard, *initial, guard], True)
    region(0x400000, [guard, *io, guard], True)
    region(stack_base, [0xA5A5A5A5] * 258, True)

    def access(at, value=None):
        assert at % 4 == 0 and at in memory, ("unmapped", hex(at))
        if value is not None:
            assert at in writable, ("read-only", hex(at))
            memory[at] = value & MASK
        return memory[at]

    r = [0x12340000 + i * 0x111 for i in range(16)]
    r[:4] = [0x100000, 0x300004, 0x200000, q]
    r[13:] = [stack_top, END | 1, labels["dvbs2_ldpc_pack_c5_pic"]]
    access(stack_top, k)
    access(stack_top + 4, 0x400004)
    saved, zero, lower, counts, minimum_sp = r[4:12].copy(), False, False, Counter(), stack_top

    def value(arg):
        if not arg.startswith("#"):
            return r[register(arg)]
        literal = arg[1:]
        assert re.fullmatch(r"[0-9xa-fA-F()+*\-]+", literal), literal
        return eval(literal, {"__builtins__": {}}, {})  # trusted assembly integer expressions

    def shifted(x, description):
        op, amount = description.split(maxsplit=1)
        n = value(amount)
        assert 0 <= n <= 32, (op, n)
        if op == "asr":
            x = x - (1 << 32) if x & (1 << 31) else x
        return ((x << n) if op == "lsl" else (x >> n)) & MASK

    while r[15] != END:
        assert sum(counts.values()) < 10000
        line = code[r[15]]
        r[15] += 1
        op, tail = line.split(maxsplit=1)
        op = op.removesuffix(".w")
        args = operands(tail)
        counts[op] += 1
        if op in ("push", "pop"):
            selected = registers(args[0])
            if op == "push":
                r[13] -= 4 * len(selected)
                for i, reg in enumerate(selected):
                    access(r[13] + i * 4, r[reg])
            else:
                for i, reg in enumerate(selected):
                    r[reg] = access(r[13] + i * 4)
                    if reg == 15:
                        r[reg] &= ~1
                r[13] += 4 * len(selected)
        elif op in ("ldr", "str"):
            address = operands(args[1].strip("[]"))
            base = register(address[0])
            offset = value(address[1]) if len(address) > 1 else 0
            if len(address) > 2:
                offset = shifted(offset, address[2])
            at = r[base] + offset
            if op == "str":
                access(at, value(args[0]))
            else:
                r[register(args[0])] = access(at)
            if len(args) == 3:
                r[base] += value(args[2])
        elif op in ("ldmia", "stmia"):
            base = register(args[0].rstrip("!"))
            selected = registers(args[1])
            at = r[base]
            for i, reg in enumerate(selected):
                if op == "ldmia":
                    r[reg] = access(at + 4 * i)
                else:
                    access(at + 4 * i, r[reg])
            if args[0].endswith("!"):
                r[base] += 4 * len(selected)
            counts["block_words"] += len(selected) - 1
        elif op in ("mov", "movs"):
            r[register(args[0])] = value(args[1])
            if op == "movs":
                zero = r[register(args[0])] == 0
        elif op in ("pkhbt", "pkhtb"):
            a, b = value(args[1]), shifted(value(args[2]), args[3])
            r[register(args[0])] = ((a & 0xFFFF) | (b & 0xFFFF0000) if op == "pkhbt" else
                                      (a & 0xFFFF0000) | (b & 0xFFFF))
        elif op in ("add", "adds", "sub", "subs", "rsb", "and", "orr", "eor", "lsl", "lsr", "lsrs"):
            dest = register(args[0])
            x, y = (r[dest], value(args[1])) if len(args) == 2 else (value(args[1]), value(args[2]))
            if len(args) == 4:
                y = shifted(y, args[3])
            if op in ("lsl", "lsr", "lsrs"):
                result = shifted(x, ("lsl" if op == "lsl" else "lsr") + " #" + str(y))
            elif op in ("add", "adds"):
                result = x + y
            elif op in ("sub", "subs"):
                result = x - y
            elif op == "rsb":
                result = y - x
            elif op == "and":
                result = x & y
            elif op == "orr":
                result = x | y
            else:
                result = x ^ y
            r[dest] = result & MASK
            if op in ("subs", "lsrs", "adds"):
                zero, lower = r[dest] == 0, x < y if op == "subs" else False
        elif op == "cmp":
            zero, lower = value(args[0]) == value(args[1]), value(args[0]) < value(args[1])
        elif op in ("b", "beq", "bne", "bls", "blo", "bl"):
            take = (op in ("b", "bl") or (op == "beq" and zero) or (op == "bne" and not zero) or
                    (op == "bls" and (lower or zero)) or (op == "blo" and lower))
            if take:
                if op == "bl":
                    # This model's instruction indices are not byte addresses.
                    r[14] = r[15] * 2 | 1
                r[15] = labels[args[0]]
        else:
            raise AssertionError(line)
        # Return addresses stored by bl use model halfword addresses.
        if op == "pop" and 15 in registers(args[0]) and r[15] != END:
            r[15] //= 2
        minimum_sp = min(minimum_sp, r[13])
    assert r[4:12] == saved and r[13] == stack_top, "AAPCS damage"
    assert minimum_sp == stack_top - 324, ("stack extent", minimum_sp)
    assert all(memory[at] == 0xA5A5A5A5 for at in range(stack_base, stack_top - 324, 4))
    assert access(0x300000) == access(0x300004 + 4 * len(initial)) == guard
    assert access(0x400000) == access(0x400010) == guard
    return ([access(0x300004 + i * 4) for i in range(len(initial))],
            [access(0x400004 + i * 4) for i in range(3)], counts)


def reference(matrix, prefix, q, k, initial, io):
    acc, fill, written = io
    bits = f"{acc:032b}"[:fill]
    for bit in range(8 if k == 11 else 32):
        bits += "".join(str(((matrix[row * 12 + k] ^ prefix[k]) >> (31 - bit)) & 1)
                        for row in range(q))
    out = initial.copy()
    while len(bits) >= 32:
        out[written] = int(bits[:32], 2)
        written += 1
        bits = bits[32:]
    return out, [int(bits.ljust(32, "0"), 2) if bits else 0, len(bits), written]


def main():
    code, labels = program()
    rng, cases = random.Random(23505), 0
    budgets = {}
    for q in range(1, 65):
        for fill in (0, 1, 16, 31):
            for k in (0, 5, 11):
                matrix = [rng.getrandbits(32) for _ in range(q * 12)]
                prefix = [rng.getrandbits(32) for _ in range(12)]
                initial = [rng.getrandbits(32) for _ in range(72)]
                io = [rng.getrandbits(32) & (MASK << (32 - fill) & MASK), fill, 3]
                got, state, counts = execute(code, labels, matrix, prefix, q, k, initial, io)
                expected, expected_state = reference(matrix, prefix, q, k, initial, io)
                assert (got, state) == (expected, expected_state), (q, k, fill, state, expected_state)
                cases += 1
                if (q, fill) in ((60, 0), (30, 16)):
                    budgets[q, k] = counts
    # Frame counts include eleven full 32-column tiles and the final eight columns.
    for q in (60, 30):
        counts = Counter({op: 11 * n for op, n in budgets[q, 0].items()}) + budgets[q, 11]
        instructions = counts.total() - counts["block_words"]
        print(f"C5 q={q}: {instructions} dynamic instructions/frame; "
              f"{counts['block_words']} additional LDM/STM words; "
              f"load/store/multiple={sum(counts[op] for op in ('ldr', 'str', 'ldmia', 'stmia'))}; "
              "no silicon timing")
    print(f"C5 copied pack assembly semantics: {cases} q/alignment/tile cases exact; "
          "q1..64, prefix fusion, AAPCS, 324-byte stack and guards")


if __name__ == "__main__":
    main()
