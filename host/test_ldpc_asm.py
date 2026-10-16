"""Check the actual copied LDPC source against independent bit extraction.

This instruction-semantics model checks immediate-shift specialization, all361
rotation offsets, stack bounds and AAPCS state. It is not Arm execution/timing.
"""
from __future__ import annotations

from pathlib import Path
import random
import re

from test_bch128_asm import operands, register, registers

ROOT = Path(__file__).resolve().parents[1]
MASK, END = 0xFFFFFFFF, 0xEEEEEEE0


def program(c5=False):
    source = [(line.split("//")[0]).strip() for line in
              (ROOT / "firmware/src/dvbs2/dvbs2_ldpc_asm.S").read_text().splitlines()]
    # Each copied leaf is deliberately tested independently; C5 adds GAS .if
    # and .rept directives, while C4's original source remains unchanged.
    marker = next(i for i, line in enumerate(source) if line.startswith(".macro C5_EXTRACT"))
    source = source[marker:] if c5 else source[:marker]
    code, labels, macros, dispatch, extract_dispatch = [], {}, {}, [], []

    def emit(lines):
        cursor = 0
        while cursor < len(lines):
            line = lines[cursor]
            cursor += 1
            if not line:
                continue
            if line.startswith((".macro ", ".irp ", ".if ", ".rept ")):
                declaration = line.split(maxsplit=2)
                directive = declaration[0]
                name = declaration[1].rstrip(",")
                args = operands(declaration[2]) if len(declaration) > 2 else []
                end = {".macro": ".endm", ".irp": ".endr",
                       ".if": ".endif", ".rept": ".endr"}[directive]
                body = []
                while lines[cursor] != end:
                    body.append(lines[cursor])
                    cursor += 1
                cursor += 1
                if directive == ".macro":
                    macros[name] = args, body
                elif directive == ".irp":
                    for value in args:
                        emit([item.replace("\\" + name, value) for item in body])
                elif directive == ".rept":
                    for _ in range(int(name)):
                        emit(body)
                else:
                    match = re.fullmatch(r"(\d+) (==|>|<) (\d+)", line[4:])
                    assert match, line
                    left, comparison, right = match.groups()
                    condition = {"==": int(left) == int(right), ">": int(left) > int(right),
                                 "<": int(left) < int(right)}[comparison]
                    if condition:
                        emit(body)
            elif line.endswith(":"):
                labels[line[:-1]] = len(code)
            elif line.startswith(".hword "):
                match = re.fullmatch(r"\.hword \(\.L(?:ldpc|c5)_rot(\d+) - \.L(?:ldpc|c5)_dispatch\) / 2", line)
                if match:
                    dispatch.append(int(match[1]))
                else:
                    match = re.fullmatch(r"\.hword \(\.Lc5_extract(0|8|16|24) - \.Lc5_extract_dispatch\) / 2", line)
                    assert match, line
                    extract_dispatch.append(int(match[1]))
            elif line.split()[0] in macros:
                name, tail = line.split(maxsplit=1)
                names, body = macros[name]
                args = operands(tail)
                assert len(names) == len(args)
                expanded = []
                for item in body:
                    for key, value in zip(names, args):
                        item = item.replace("\\" + key, value)
                    expanded.append(item)
                emit(expanded)
            elif not line.startswith((".", "#")):
                code.append(line)
    emit(source)
    assert dispatch == list(range(32))
    assert extract_dispatch == ([0, 8, 16, 24] if c5 else [])
    return code, labels


def execute(code, labels, info, shift, initial, rows, offsets, c5=False, counts=None):
    memory, writable = {}, set()

    def region(base, values, size, write):
        for i, value in enumerate(values):
            for j in range(size):
                at = base + i * size + j
                memory[at] = (value >> (8 * j)) & 255
                if write:
                    writable.add(at)

    guard = 0xCA85319D
    region(0x100000, info, 4, False)
    region(0x200000, [guard, *initial, guard], 4, True)
    region(0x300000, rows, 2, False)
    region(0x400000, offsets, 2, False)
    region(0x500000, [0xA5] * 512, 1, True)

    def access(at, size=4, value=None):
        assert at % size == 0, (hex(at), size)
        assert all(at + j in memory for j in range(size)), (hex(at), size)
        if value is not None:
            assert all(at + j in writable for j in range(size))
            for j in range(size):
                memory[at + j] = (value >> (8 * j)) & 255
        return sum(memory[at + j] << (8 * j) for j in range(size))

    r = [0x12340000 + i * 0x111 for i in range(16)]
    r[:4] = [0x200004, 0x100000, shift, 0x300000]
    r[13:] = [0x500100, END | 1, labels["dvbs2_ldpc_group_c5_pic" if c5 else "dvbs2_ldpc_group_pic"]]
    access(r[13], value=0x400000)
    access(r[13] + 4, value=len(rows))
    saved, zero, steps = r[4:12].copy(), False, 0

    def value(arg):
        if not arg.startswith("#"):
            return r[register(arg)]
        literal = arg[1:].strip("()")
        if re.fullmatch(r"\d+[+-]\d+", literal):
            return sum(int(part) for part in re.findall(r"[+-]?\d+", literal))
        return int(literal, 0)

    def shifted(x, description):
        op, amount = description.split(maxsplit=1)
        n = value(amount)
        assert 0 <= n <= 32
        return ((x << n) if op == "lsl" else (x >> n)) & MASK

    while r[15] != END:
        assert steps < 100000
        line = code[r[15]]
        r[15] += 1
        steps += 1
        op, tail = line.split(maxsplit=1)
        args = operands(tail)
        if counts is not None:
            counts[op] = counts.get(op, 0) + 1
        if op in ("push", "pop"):
            selected = registers(args[0])
            if op == "push":
                r[13] -= 4 * len(selected)
                for i, k in enumerate(selected):
                    access(r[13] + i * 4, value=r[k])
            else:
                for i, k in enumerate(selected):
                    r[k] = access(r[13] + i * 4)
                    if k == 15:
                        r[k] &= ~1
                r[13] += 4 * len(selected)
        elif op in ("ldmia", "stmia"):
            base = register(args[0].rstrip("!"))
            selected = registers(args[1])
            address = r[base]
            for i, k in enumerate(selected):
                if op == "ldmia":
                    r[k] = access(address + i * 4)
                else:
                    access(address + i * 4, value=r[k])
            if args[0].endswith("!"):
                r[base] += 4 * len(selected)
        elif op in ("ldr", "ldrh", "str"):
            address = operands(args[1].strip("[]"))
            base = register(address[0])
            at = r[base] + (value(address[1]) if len(address) > 1 else 0)
            if op == "str":
                access(at, value=value(args[0]))
            else:
                r[register(args[0])] = access(at, 2 if op == "ldrh" else 4)
            if len(args) == 3:
                r[base] += value(args[2])
        elif op in ("mov", "movs"):
            r[register(args[0])] = value(args[1])
        elif op in ("add", "sub", "subs", "rsb", "and", "orr", "eor", "eors", "lsl", "lsr", "lsrs"):
            dest = register(args[0])
            x, y = (r[dest], value(args[1])) if len(args) == 2 else (value(args[1]), value(args[2]))
            if len(args) == 4:
                y = shifted(y, args[3])
            if op in ("lsl", "lsr", "lsrs"):
                result = shifted(x, ("lsl" if op == "lsl" else "lsr") + " #" + str(y))
            elif op == "add":
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
            if op in ("subs", "lsrs", "eors"):
                zero = r[dest] == 0
        elif op == "cmp":
            zero = value(args[0]) == value(args[1])
        elif op in ("b", "beq", "bne"):
            if op == "b" or zero == (op == "beq"):
                r[15] = labels[args[0]]
        elif op == "tbh":
            assert args == ["[pc, r2, lsl #1]"]
            if c5 and r[15] == labels[".Lc5_extract_dispatch"]:
                r[15] = labels[f".Lc5_extract{r[2] * 8}"]
            else:
                r[15] = labels[(".Lc5_rot" if c5 else ".Lldpc_rot") + str(r[2])]
        else:
            raise AssertionError(line)
    assert r[4:12] == saved and r[13] == 0x500100, "AAPCS damage"
    assert access(0x200000) == access(0x200004 + 4 * len(initial)) == guard
    assert all(memory[at] == 0xA5 for at in range(0x500000, 0x500078)), "stack >136 bytes"
    return [access(0x200004 + i * 4) for i in range(len(initial))], steps


def reference(info, shift, initial, rows, offsets):
    bits = "".join(f"{word:032b}" for word in info)[shift:shift + 360]
    doubled = bits * 2 + "0" * 64
    out = initial.copy()
    for row, offset in zip(rows, offsets):
        value = doubled[offset:offset + 384]
        for word in range(12):
            out[row * 12 + word] ^= int(value[32 * word:32 * word + 32], 2)
    return out


def main():
    code, labels = program()
    rng, cases, instructions = random.Random(2350), 0, 0
    for shift in (0, 8, 16, 24):
        for pattern in (0, MASK, 0xAAAAAAAA, None):
            info = [pattern if pattern is not None else rng.getrandbits(32) for _ in range(13)]
            for count in (0, 1, 4, 12, 361):
                offsets = list(range(361)) if count == 361 else [rng.randrange(361) for _ in range(count)]
                rows = [rng.randrange(60) for _ in offsets]
                initial = [rng.getrandbits(32) for _ in range(60 * 12)]
                got, steps = execute(code, labels, info, shift, initial, rows, offsets)
                assert got == reference(info, shift, initial, rows, offsets), (shift, pattern, count)
                cases += 1
                instructions += steps
    print(f"LDPC copied assembly semantics: {cases} cases/{instructions} instructions; "
          "all361 rotations, four group alignments, AAPCS and136-byte stack exact (no silicon timing)")


if __name__ == "__main__":
    main()
