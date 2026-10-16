"""Decode B10 copied code, verify relocation closure, and execute GCS semantics.

No hardware access or timing claim. The C5/C9 scheduler copies the existing C
state machine; independent native FEC tests cover its output. This check proves
that its assembled direct branches, jump table and literal loads remain within
the copied bundle, with only resolved absolute data/leaf-source literals.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import random
import re
import subprocess
import tempfile

from test_c4_mapper_pic import ROOT, tool


def instructions(text):
    result = []
    for line in text.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+(?:[0-9a-f]{4}\s+)+\s*(\S+)\s*(.*)", line)
        if m:
            result.append((int(m[1], 16), m[2].split(".")[0], m[3].split("@")[0].strip()))
    return result


def inspect(cc, dump, temp):
    flags = ["-O3", "-std=c11", "-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter",
             "-mcpu=cortex-m33", "-mthumb", "-march=armv8-m.main+fp+dsp",
             "-mfloat-abi=softfp", "-mcmse", "-DNDEBUG", "-ffunction-sections",
             "-fdata-sections", "-DIQ_ON_DEVICE=1", "-DIQ4HIST_EXPERIMENT=1",
             f"-I{ROOT / 'firmware/src'}"]
    objects = []
    for source in ("dvbs2.c", "b10_hot.S"):
        obj = temp / (source + ".o")
        subprocess.run([cc, *flags, "-c", str(ROOT / "firmware/src" / source), "-o", str(obj)], check=True)
        objects.append(obj)
    scheduler, gcs = ".text.b10_ldpc_schedule_pic", ".text.dvbs2_gcs_words_pic"
    sizes, decoded = [], []
    for obj, section in zip(objects, (scheduler, gcs)):
        headers = subprocess.check_output([dump, "-h", str(obj)], text=True)
        size = int(re.search(rf"\s+{re.escape(section)}\s+([0-9a-f]+)\s", headers)[1], 16)
        sizes.append(size)
        assert 0 < size <= (1536 if section == scheduler else 64), (section, size)
        relocs = subprocess.check_output([dump, "-r", "-j", section, str(obj)], text=True)
        types = re.findall(r"R_ARM_\w+", relocs)
        assert all(kind == "R_ARM_ABS32" for kind in types), relocs
        if section == gcs:
            assert not types, relocs
        dis = subprocess.check_output([dump, "-d", "-Mreg-names-std", "-j", section, str(obj)], text=True)
        code = instructions(dis)
        decoded.append(code)
        raw_file = temp / (obj.name + ".bin")
        subprocess.run([tool("arm-none-eabi-objcopy"), "-O", "binary", "--only-section", section,
                        str(obj), str(raw_file)], check=True)
        raw = raw_file.read_bytes()
        instruction_addresses = {address for address, _, _ in code}
        literal_count, internal_calls, tables = 0, 0, 0
        for address, op, args in code:
            if op in ("b", "beq", "bne", "bcs", "bcc", "bhi", "bls", "bmi", "bpl", "bl", "cbz", "cbnz"):
                target = int(args.split(",")[-1].split()[0], 16)
                assert 0 <= target < size, (section, address, op, args)
                if section == gcs and target < address:
                    assert target % 4 == 0, ("unaligned GCS hot loop", target)
                internal_calls += op == "bl"
            if "[pc" in args:
                if op == "tbb":
                    assert args == "[pc, r3]", args
                    # This bounded switch has four entries (stages 0..3).
                    for byte in raw[address + 4:address + 8]:
                        target = address + 4 + 2 * byte
                        assert target in instruction_addresses, (section, address, target)
                    tables += 1
                else:
                    assert op == "ldr", (op, args)
                    displacement = int(re.search(r"\[pc, #(\d+)\]", args)[1])
                    target = ((address + 4) & ~3) + displacement
                    assert 0 <= target <= size - 4, (section, address, target, size)
                    literal_count += 1
            assert op != "adr", (section, op, args)
            if op == "blx":
                assert re.fullmatch(r"r\d+", args), args
            if op == "bx":
                assert args == "lr", args
        if section == scheduler:
            assert internal_calls == 1 and tables == 1 and literal_count >= 10
        else:
            assert internal_calls == tables == literal_count == 0
    assert (sizes[0] + 3) // 4 * 4 + sizes[1] <= 2048, sizes
    return sizes, decoded[1]


def execute(code, source, scramble, count):
    # Byte-addressed memory makes every possible unaligned pattern offset legal.
    memory = bytearray(4096)
    src, scr, dst, top = 256, 1024, 1536, 3072
    memory[src:src + len(source)] = source
    memory[scr:scr + 4 * count] = b"".join(x.to_bytes(4, "little") for x in scramble)
    memory[dst - 4:dst + 4 * count + 4] = b"\xa5" * (4 * count + 8)
    regs = [0x12340000 + 17 * i for i in range(16)]
    regs[:4], regs[13], regs[14] = [dst, src, scr, count], top, 0xeeeeeee1
    saved, zero, pc = regs[4:12].copy(), False, code[0][0]
    by_address = {address: i for i, (address, _, _) in enumerate(code)}

    def load(at):
        return int.from_bytes(memory[at:at + 4], "little")

    def store(at, value):
        memory[at:at + 4] = value.to_bytes(4, "little")

    while True:
        i = by_address[pc]
        _, op, args = code[i]
        following = code[i + 1][0] if i + 1 < len(code) else None
        if op in ("push", "pop"):
            assert args == "{r4, r5}", args
            if op == "push":
                regs[13] -= 8
                store(regs[13], regs[4]); store(regs[13] + 4, regs[5])
            else:
                regs[4], regs[5] = load(regs[13]), load(regs[13] + 4)
                regs[13] += 8
        elif op == "nop":
            pass
        elif op == "cmp":
            assert args == "r3, #0", args
            zero = regs[3] == 0
        elif op in ("beq", "bne"):
            if zero == (op == "beq"):
                following = int(args.split()[0], 16)
        elif op in ("ldr", "str"):
            m = re.fullmatch(r"r(\d+), \[r(\d+)\], #4", args)
            assert m, (op, args)
            reg, base = map(int, m.groups())
            if op == "ldr": regs[reg] = load(regs[base])
            else: store(regs[base], regs[reg])
            regs[base] += 4
        elif op == "rev":
            assert args == "r4, r4", args
            regs[4] = int.from_bytes(regs[4].to_bytes(4, "little"), "big")
        elif op == "eors":
            assert args in ("r4, r5", "r4, r4, r5"), args
            regs[4] ^= regs[5]
        elif op == "subs":
            assert args in ("r3, #1", "r3, r3, #1"), args
            regs[3] -= 1
            zero = regs[3] == 0
        elif op == "bx":
            assert args == "lr" and regs[14] == 0xeeeeeee1
            break
        else:
            raise AssertionError((op, args))
        pc = following
    assert regs[4:12] == saved and regs[13] == top
    assert memory[dst - 4:dst] == memory[dst + 4 * count:dst + 4 * count + 4] == b"\xa5" * 4
    return [load(dst + 4 * n) for n in range(count)]


def check_elf(dump, elf):
    symbols = subprocess.check_output([tool("arm-none-eabi-nm"), "-n", str(elf)], text=True)
    addresses = {name: int(address, 16) for address, name in re.findall(
        r"^([0-9a-f]+)\s+\w\s+(\S+)$", symbols, re.M)}
    start, end = (addresses[f"__b10_ldpc_schedule_{part}__"] for part in ("start", "end"))
    assert start % 4 == 0 and 0 < end - start <= 1536
    assert start <= addresses["ldpc_prefix_into"] < end
    assert start <= addresses["ldpc_c5_step"] < end
    dis = subprocess.check_output([dump, "-d", "-Mreg-names-std", f"--start-address={start}",
                                   f"--stop-address={end}", str(elf)], text=True)
    calls = 0
    for address, op, args in instructions(dis):
        if op in ("b", "beq", "bne", "bcs", "bcc", "bhi", "bls", "bmi", "bpl", "bl", "cbz", "cbnz"):
            target = int(args.split(",")[-1].split()[0], 16)
            assert start <= target < end, (hex(address), op, args)
            calls += op == "bl"
        if "[pc" in args and op != "tbb":
            assert op == "ldr", (op, args)
            displacement = int(re.search(r"\[pc, #(\d+)\]", args)[1])
            target = ((address + 4) & ~3) + displacement
            assert start <= target <= end - 4
        assert op != "adr", (op, args)
    assert calls == 1
    print(f"B10 linked scheduler closure: {start:#x}..{end:#x}, entry {addresses['ldpc_c5_step']:#x}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--elf", type=Path, help="also inspect the linked experiment image")
    args = parser.parse_args()
    cc, dump = tool("arm-none-eabi-gcc"), tool("arm-none-eabi-objdump")
    assert cc and dump
    with tempfile.TemporaryDirectory(prefix="b10-hot-") as temp:
        sizes, code = inspect(cc, dump, Path(temp))
    rng, cases = random.Random(235010), 0
    period = bytes(rng.randrange(256) for _ in range(256))
    pattern = period * 2
    for offset in range(256):
        for count in range(65):
            source = pattern[offset:offset + 4 * count]
            scramble = [rng.getrandbits(32) for _ in range(count)]
            expected = [int.from_bytes(source[4 * n:4 * n + 4], "big") ^ scramble[n] for n in range(count)]
            assert execute(code, source, scramble, count) == expected
            cases += 1
    print(f"B10 hot copies: scheduler {sizes[0]} B relocation closure; GCS {sizes[1]} B, "
          f"{cases} decoded-instruction cases, unaligned/wrap/guards/AAPCS; no device timing")
    if args.elf:
        check_elf(dump, args.elf)


if __name__ == "__main__":
    main()
