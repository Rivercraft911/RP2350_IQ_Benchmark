"""Check the assembled C7 BCH PIC leaves against bit-serial remainders.

This is a guarded instruction-semantics model of actual GCC/objdump output,
not Arm execution or silicon timing. It also rejects relocations, calls,
external branches and PC-relative data in every copied function.
"""
from __future__ import annotations

import ctypes as ct
import json
from pathlib import Path
import random
import re
import subprocess
import tempfile

from test_bch128_asm import MASK, END, operands, register, registers, serial
from test_c4_mapper_pic import tool
import fwsrc

ROOT = Path(__file__).resolve().parents[1]


def program(folder, existing=False):
    cc, dump = tool("arm-none-eabi-gcc"), tool("arm-none-eabi-objdump")
    assert cc and dump, "ARM GCC and objdump are required for copied-code checks"
    obj = folder / ("existing.o" if existing else "bch.o")
    subprocess.run([cc, "-Wall", "-Wextra", "-Werror", "-mcpu=cortex-m33", "-mthumb",
                    "-march=armv8-m.main+fp+dsp", "-mfloat-abi=softfp", "-mcmse",
                    "-DIQ_ON_DEVICE=1", "-c", str(fwsrc.path(
                        "dvbs2_asm.S" if existing else "dvbs2_bch_fast.S")),
                    "-o", str(obj)], check=True)
    headers = subprocess.check_output([dump, "-h", str(obj)], text=True)
    suffix = r"(?:bch\d+_continue_y|transpose32)" if existing else r"bch\d+_(?:fast_pic|table3_pic)"
    sizes = {name: int(size, 16) for name, size in re.findall(
        rf"\s+\.(?:text|scratch_[xy])\.(dvbs2_{suffix})\s+([0-9a-f]+)\s", headers)}
    assert len(sizes) == (4 if existing else 6) and all(0 < size <= 1024 for size in sizes.values()), sizes
    symbols = subprocess.check_output([dump, "-t", str(obj)], text=True)
    extents = {name: int(size, 16) for size, name in re.findall(
        rf"\n([0-9a-f]+)\s+g\s+\.(?:text|scratch_[xy])\.dvbs2_\w+\s+00000000\s+(dvbs2_{suffix})_end", symbols)}
    assert set(extents) == set(sizes)
    assert all(0 < extents[name] <= sizes[name] <= (extents[name] + 3) for name in sizes)
    sections = [part for name in sizes for part in (
        "-j", (".scratch_x." if name == "dvbs2_transpose32" else
               ".scratch_y." if existing else ".text.") + name)]
    relocations = subprocess.check_output([dump, "-r", *sections, str(obj)], text=True)
    assert "R_ARM_" not in relocations, relocations
    disassembly = subprocess.check_output([dump, "-d", "-Mreg-names-std", str(obj)], text=True)
    code, labels, addresses, branches, loops = [], {}, {}, [], {}
    section = None
    for line in disassembly.splitlines():
        match = re.match(r"Disassembly of section \S+\.(dvbs2_\w+):", line)
        if match:
            section = match[1] if match[1] in sizes else None
            if section:
                labels[section] = len(code)
            continue
        match = re.match(r"\s*([0-9a-f]+):\s+(?:[0-9a-f]{4}\s+)+\s*(\S+)\s*(.*)", line)
        if not match or section is None:
            continue
        address, op, tail = int(match[1], 16), match[2].split(".")[0], match[3].split("@")[0].strip()
        assert section in sizes and address < sizes[section]
        if address >= extents[section]:
            assert op == "nop", line
            continue
        assert op not in ("bl", "blx", "bx", "adr") and not re.search(r"\bpc\b", tail.replace("pc}", "}")), line
        if op == "stmdb":
            assert tail.startswith("sp!, ")
            op, tail = "push", tail[6:]
        elif op == "ldmia" and tail.startswith("sp!, "):
            op, tail = "pop", tail[6:]
        if op in ("b", "beq", "bne"):
            target = int(tail.split()[0], 16)
            assert 0 <= target < extents[section], line
            if target < address:
                if not existing:
                    assert target % 4 == 0, (section, "unaligned hot loop", target)
                loops.setdefault(section, []).append((target, address))
            tail = f"{section}:{target}"
            branches.append((len(code), tail))
        addresses[f"{section}:{address}"] = len(code)
        code.append(f"{op} {tail}")
    assert set(loops) == set(sizes)
    for _, target in branches:
        labels[target] = addresses[target]
    for section, targets in loops.items():
        assert len(targets) == (2 if section == "dvbs2_transpose32" else 1)
        start, end = targets[0]
        labels[section + ":loop_start"] = addresses[f"{section}:{start}"]
        labels[section + ":loop_end"] = addresses[f"{section}:{end}"]
    return code, labels, extents


def execute(code, labels, entry, words, table, parity):
    # Deliberately use a table address that lacks any 8 KiB alignment.
    regions = [(0x100004, list(words), False), (0x200004, list(table), False),
               (0x300000, [0xCA85319D, *parity, 0xCA85319D], True),
               (0x400000, [0] * 64, True)]
    transpose = entry == "dvbs2_transpose32"
    if transpose:
        assert len(words) == 32
        regions[0] = (0x100000, [0xCA85319D, *words, 0xCA85319D], True)

    def access(address, value=None):
        assert address % 4 == 0
        for base, data, writable in regions:
            offset = (address - base) // 4
            if 0 <= offset < len(data):
                if value is not None:
                    assert writable, (entry, hex(address))
                    data[offset] = value & MASK
                return data[offset]
        raise AssertionError((entry, "unmapped access", hex(address)))

    r = [0x12340000 + i * 0x111 for i in range(16)]
    r[:4] = [0x100004, 0x300004, 0x200004, len(words)]
    r[13:16] = [0x400100, END | 1, labels[entry]]
    saved, zero, steps, minimum_sp = r[4:12].copy(), False, 0, r[13]

    def value(arg):
        return int(arg[1:], 0) if arg.startswith("#") else r[register(arg)]

    while r[15] != END:
        assert steps < 250000
        line = code[r[15]]
        r[15] += 1
        steps += 1
        parts = line.split(maxsplit=1)
        op, tail = parts[0], parts[1] if len(parts) == 2 else ""
        args = operands(tail)
        if op == "nop":
            pass
        elif op in ("push", "pop"):
            selected = registers(args[0])
            if op == "push":
                r[13] -= 4 * len(selected)
                minimum_sp = min(minimum_sp, r[13])
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
            selected, address = registers(args[1]), r[base]
            for i, k in enumerate(selected):
                if op == "ldmia":
                    r[k] = access(address + i * 4)
                else:
                    access(address + i * 4, r[k])
            if args[0].endswith("!"):
                assert base not in selected
                r[base] = address + 4 * len(selected)
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
        elif op in ("eor", "and", "add", "adds", "mov", "movs", "uxtb", "ubfx", "pkhtb", "pkhbt"):
            dest = register(args[0])
            x = r[dest] if op == "adds" and len(args) == 2 else value(args[1])
            if op in ("eor", "and", "add", "adds", "pkhtb", "pkhbt"):
                y = value(args[1] if op == "adds" and len(args) == 2 else args[2])
                if len(args) == 4:
                    shift, amount = args[3].split(" #")
                    y = y << int(amount) if shift == "lsl" else y >> int(amount)
                if op == "eor": result = x ^ y
                elif op == "and": result = x & y
                elif op == "pkhtb": result = (x & 0xFFFF0000) | (y & 0xFFFF)
                elif op == "pkhbt": result = (x & 0xFFFF) | (y & 0xFFFF0000)
                else: result = x + y
            elif op == "uxtb":
                result = x & 255
            elif op == "ubfx":
                result = (x >> value(args[2])) & ((1 << value(args[3])) - 1)
            else:
                result = x
            r[dest] = result & MASK
            if op in ("movs", "adds"):
                zero = r[dest] == 0
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
    stack_bytes = 36 if transpose else 48 if "192" in entry else 40
    assert minimum_sp == 0x400100 - stack_bytes
    assert regions[3][1][:-stack_bytes // 4] == [0] * (64 - stack_bytes // 4), "stack extent"
    if transpose:
        output = regions[0][1]
        assert output[0] == output[-1] == 0xCA85319D, "transpose overwrite"
    return output[1:-1], steps


def main():
    bridge = '''
#include "dvbs2.c"
#include "dvbs2_codes.h"
void setup(int ci, uint32_t *table, uint32_t *generator) {
    dvbs2_init(&DVBS2_CODES[ci], false);
    memcpy(table, bch_tab, sizeof bch_tab);
    memcpy(generator, bch_g, sizeof bch_g);
}
'''
    rng, cases, instructions = random.Random(710), 0, 0
    with tempfile.TemporaryDirectory(prefix="bch-fast-asm-") as temp:
        folder = Path(temp)
        code, labels, sizes = program(folder)
        existing_code, existing_labels, existing_sizes = program(folder, existing=True)
        source, binary = folder / "bridge.c", folder / "bridge.so"
        source.write_text(bridge)
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror", "-shared", "-fPIC",
                        *fwsrc.INCLUDES, str(source), "-o", str(binary)], check=True)
        lib = ct.CDLL(str(binary))
        summary = {}
        for ci, nw, degree in ((9, 1796, 128), (10, 1818, 128), (5, 1345, 160),
                               (8, 1682, 160), (6, 1512, 192)):
            original, generator = (ct.c_uint32 * 8192)(), (ct.c_uint32 * 6)()
            lib.setup(ci, original, generator)
            polynomial = int.from_bytes(b"".join(x.to_bytes(4, "big") for x in generator[:degree // 32]), "big")
            for variant in ("fast", "table3"):
                entry = f"dvbs2_bch{degree}_{variant}_pic"
                table = list(original)
                slices = [(shift, 8) for shift in (24, 16, 8, 0)]
                if variant == "table3":
                    slices, table = [(21, 11), (10, 11), (0, 10)], []
                    # Build independently from a serial polynomial recurrence,
                    # rather than using the firmware's table-combination helper.
                    for shift, bits in slices:
                        for index in range(1 << bits):
                            table += serial([index << shift], [0] * 6, polynomial, degree)[:degree // 32]
                fixtures = [[], [0], [MASK], [0xAAAAAAAA] * 31,
                            [rng.getrandbits(32) for _ in range(nw)]]
                fixtures += [[index << shift] for shift, bits in slices for index in range(1 << bits)]
                for words in fixtures:
                    parity = [0] * 6
                    got, count = execute(code, labels, entry, words, table, parity)
                    assert got == serial(words, parity, polynomial, degree), (ci, entry, len(words))
                    cases += 1
                    instructions += count
                words = fixtures[4]
                parity = [rng.getrandbits(32) for _ in range(degree // 32)] + [0] * (6 - degree // 32)
                want, cursor = serial(words, parity, polynomial, degree), 0
                while cursor < len(words):
                    take = rng.choice((1, 2, 3, 63, 64, 65, 128))
                    parity, count = execute(code, labels, entry, words[cursor:cursor + take], table, parity)
                    cursor += take
                    instructions += count
                assert parity == want, (ci, entry, "continuation")
                cases += 1
                # A used lookup bit is an explicit negative control for the
                # independent recurrence comparison.
                damaged = table.copy()
                index = 8 if variant == "fast" else (4096 + 1) * (degree // 32)
                damaged[index] ^= 1
                got, count = execute(code, labels, entry, [1], damaged, [0] * 6)
                assert got != serial([1], [0] * 6, polynomial, degree)
                cases += 1
                instructions += count
                loop = code[labels[entry + ":loop_start"]:labels[entry + ":loop_end"] + 1]
                transfers = sum(len(registers(operands(line.split(maxsplit=1)[1])[1])) - 1
                                for line in loop if line.startswith(("ldmia ", "stmia ")))
                summary[entry] = dict(bytes=sizes[entry], loop_instructions=len(loop),
                                      transfer_expanded=len(loop) + transfers)
        fixtures = [[0] * 32, [MASK] * 32]
        fixtures += [[rng.getrandbits(32) for _ in range(32)] for _ in range(32)]
        fixtures += [[(1 << col) if at == row else 0 for at in range(32)]
                     for row in range(32) for col in range(32)]
        for words in fixtures:
            want = [sum(((word >> (31 - col)) & 1) << (31 - row)
                        for row, word in enumerate(words)) for col in range(32)]
            got, count = execute(existing_code, existing_labels, "dvbs2_transpose32", words, [], [0] * 6)
            assert got == want, "actual transpose leaf differs from scalar bit matrix"
            cases += 1
            instructions += count
        print(json.dumps(dict(cases=cases, interpreted_instructions=instructions,
                              existing_pic_extents=existing_sizes, transpose_cases=len(fixtures),
                              copies=summary, device_timing=False), sort_keys=True))


if __name__ == "__main__":
    main()
