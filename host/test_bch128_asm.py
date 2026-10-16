"""Execute actual t=8 and t=12 assembly source against bit-serial BCH recurrences.

This checked instruction-semantics model covers both placements, table indices,
continuation boundaries and AAPCS state. It is not Arm execution or cycle timing.
"""
from __future__ import annotations

import ctypes as ct
from pathlib import Path
import random
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MASK = 0xFFFFFFFF
END = 0xEEEEEEE0


def operands(line):
    return [x.strip() for x in re.split(r",\s*(?![^\[{]*[\]}])", line)]


def register(name):
    return {"sp": 13, "lr": 14, "pc": 15}.get(name, int(name[1:]) if name.startswith("r") else -1)


def registers(value):
    result = []
    for part in value.strip("{}").split(","):
        if "-" in part:
            first, last = map(register, part.split("-"))
            result.extend(range(first, last + 1))
        else:
            result.append(register(part.strip()))
    assert result == sorted(result)
    return result


def program():
    macros, code, labels = {}, [], {}
    name, params, body = None, [], []

    def emit(line):
        head = line.split(maxsplit=1)[0]
        if head in macros:
            args = operands(line.split(maxsplit=1)[1])
            names, lines = macros[head]
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

    for raw in (ROOT / "firmware/src/dvbs2_asm.S").read_text().splitlines():
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


def execute(code, labels, entry, words, table, parity):
    # Every region has an exact extent; accidental table/input reads and stores fail.
    regions = [(0x100000, list(words), False), (0x200000, list(table), False),
               (0x300000, [0xCA85319D, *parity, 0xCA85319D], True),
               (0x400000, [0] * 64, True)]

    def access(address, value=None):
        assert address % 4 == 0
        for base, data, writable in regions:
            offset = (address - base) // 4
            if 0 <= offset < len(data):
                if value is not None:
                    assert writable
                    data[offset] = value & MASK
                return data[offset]
        raise AssertionError(("unmapped access", hex(address)))

    r = [0x12340000 + i * 0x111 for i in range(16)]
    r[:4] = [0x100000, 0x300004, 0x200000, len(words)]
    r[13:16] = [0x400100, END | 1, labels[entry]]
    saved, zero, steps = r[4:12].copy(), False, 0

    def value(arg):
        return int(arg[1:], 0) if arg.startswith("#") else r[register(arg)]

    while r[15] != END:
        assert steps < 200000
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
                    access(r[13] + i * 4, r[k])
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
                    access(address + i * 4, r[k])
            if args[0].endswith("!"):
                r[base] += 4 * len(selected)
        elif op in ("ldr", "str"):
            address = operands(args[1].strip("[]"))
            base = register(address[0])
            target = r[base] + (value(address[1]) if len(address) > 1 else 0)
            if op == "ldr":
                r[register(args[0])] = access(target)
            else:
                access(target, value(args[0]))
            if len(args) == 3:
                r[base] += value(args[2])
        elif op in ("eor", "add", "mov", "movs", "uxtb", "ubfx"):
            dest = register(args[0])
            x = value(args[1])
            if op == "eor":
                result = x ^ value(args[2])
            elif op == "add":
                y = value(args[2])
                if len(args) == 4:
                    assert args[3].startswith("lsl #")
                    y <<= int(args[3][5:])
                result = x + y
            elif op == "uxtb":
                result = x & 255
            elif op == "ubfx":
                result = (x >> value(args[2])) & ((1 << value(args[3])) - 1)
            else:
                result = x
            r[dest] = result & MASK
        elif op in ("cmp", "subs"):
            x, y = (value(args[0]), value(args[1])) if len(args) == 2 else (value(args[1]), value(args[2]))
            zero = x == y
            if op == "subs":
                r[register(args[0])] = (x - y) & MASK
        elif op in ("b", "beq", "bne"):
            if op == "b" or zero == (op == "beq"):
                r[15] = labels[args[0]]
        else:
            raise AssertionError(("unsupported instruction", line))
    output = regions[2][1]
    assert r[4:12] == saved and r[13] == 0x400100, "AAPCS register/stack damage"
    assert output[0] == output[-1] == 0xCA85319D, "parity overwrite"
    stack_words = 12 if entry == "dvbs2_bch192_continue_y" else 10
    assert regions[3][1][:-stack_words] == [0] * (64 - stack_words), "stack extent"
    return output[1:-1], steps


def serial(words, parity, generator, degree=128):
    count = degree // 32
    state = int.from_bytes(b"".join(x.to_bytes(4, "big") for x in parity[:count]), "big")
    for word in words:
        for bit in range(31, -1, -1):
            feedback = (state >> (degree - 1)) ^ ((word >> bit) & 1)
            state = (state << 1) & ((1 << degree) - 1)
            if feedback:
                state ^= generator
    return [(state >> shift) & MASK for shift in range(degree - 32, -1, -32)] + [0] * (6 - count)


def main():
    code, labels = program()
    bridge = '''
#include "dvbs2.c"
#include "dvbs2_codes.h"
void setup(int ci, uint32_t *table, uint32_t *generator) {
    dvbs2_init(&DVBS2_CODES[ci], false);
    memcpy(table, bch_tab, sizeof bch_tab);
    memcpy(generator, bch_g, sizeof bch_g);
}
'''
    rng, cases, instructions = random.Random(910), 0, 0
    with tempfile.TemporaryDirectory(prefix="bch128-asm-") as folder:
        source, binary = Path(folder) / "bridge.c", Path(folder) / "bridge.so"
        source.write_text(bridge)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        f"-I{ROOT / 'firmware/src'}", str(source), "-o", str(binary)], check=True)
        lib = ct.CDLL(str(binary))
        for ci, nw, degree in ((9, 1796, 128), (10, 1818, 128), (6, 1512, 192)):
            entries = ("dvbs2_bch192_continue_y",) if degree == 192 else (
                "dvbs2_bch128_continue", "dvbs2_bch128_continue_y")
            table, generator = (ct.c_uint32 * 8192)(), (ct.c_uint32 * 6)()
            lib.setup(ci, table, generator)
            polynomial = int.from_bytes(b"".join(x.to_bytes(4, "big") for x in generator[:degree // 32]), "big")
            fixtures = [[], [0], [MASK], [0xAAAAAAAA] * 31,
                        [rng.getrandbits(32) for _ in range(nw)]]
            # Each slicing table sees all 256 indices from a zero initial state.
            fixtures += [[byte << shift] for shift in (0, 8, 16, 24) for byte in range(256)]
            for entry in entries:
                for words in fixtures:
                    parity = [0] * 6
                    want = serial(words, parity, polynomial, degree)
                    got, count = execute(code, labels, entry, words, table, parity)
                    assert got == want, (ci, entry, len(words))
                    cases += 1
                    instructions += count
                words = fixtures[4]
                parity = [rng.getrandbits(32) for _ in range(degree // 32)] + [0] * (6 - degree // 32)
                want = serial(words, parity, polynomial, degree)
                # Arbitrary prior remainders and uneven live slice boundaries.
                cursor = 0
                while cursor < len(words):
                    take = rng.choice((1, 3, 96, 128))
                    parity, count = execute(code, labels, entry, words[cursor:cursor + take], table, parity)
                    cursor += take
                    instructions += count
                assert parity == want
                cases += 1
    print(f"BCH128/192 assembly semantics: {cases} cases, {instructions} instructions; "
          "three leaves, all table indices, continuation and AAPCS exact (no silicon timing)")


if __name__ == "__main__":
    main()
