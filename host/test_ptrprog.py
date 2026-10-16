"""Execute the actual pin-free PIO instruction words against an independent serial-bit model.

No device access. --record accepts a ptrbench JSON record and checks its four output CRCs
against this host reference, without trusting the firmware's expected_crc fields.
"""
from __future__ import annotations

import argparse
import json
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path
import fwsrc

ROOT = Path(__file__).resolve().parents[1]


def axis_words(seed, count):
    out = []
    for _ in range(count):
        seed ^= (seed << 13) & 0xffffffff
        seed ^= seed >> 17
        seed ^= (seed << 5) & 0xffffffff
        out.append(seed & 0xffff)
    return out


def expected(words, phase, base):
    bits = [(word >> bit) & 1 for word in words for bit in range(16)]
    return [base + 16 * sum(bits[n - age] << age for age in range(13) if n >= age)
            for n in range(1 + 2 * phase, len(bits), 4)]


def execute(code, words, phase, base):
    fifo = [((word & 0x5555) << 1) | ((word & 0xaaaa) >> 1) for word in words]
    regs = dict(x=0, y=base >> 17, isr=0, osr=0)
    source = {1: "x", 2: "y", 3: None, 6: "isr", 7: "osr"}
    pc, shifted, consumed, cycles = (0 if phase == 0 else 10), 32, 0, 0
    outputs = []
    while len(outputs) < len(words) * 4:
        op = code[pc]
        next_pc = (pc + 1) % len(code)
        major, arg, count = op >> 13, (op >> 5) & 7, op & 31
        cycles += 1
        if major == 0:  # JMP !OSRE
            assert arg == 7
            if shifted < 16:
                next_pc = count
        elif major == 4:
            if op & 0x80:  # PULL, replicated DMA16 word
                word = fifo[consumed]; consumed += 1
                regs["osr"] = word | (word << 16); shifted = 0
            else:  # PUSH
                outputs.append(regs["isr"]); regs["isr"] = 0
        elif major == 5:  # MOV
            assert (op >> 3) & 3 == 0
            regs[source[arg]] = regs[source[op & 7]]
        elif major == 2:  # IN, left shift
            value = regs[source[arg]] if source[arg] else 0
            regs["isr"] = ((regs["isr"] << count) | (value & ((1 << count) - 1))) & 0xffffffff
        elif major == 3:  # OUT NULL, right shift
            assert arg == 3
            regs["osr"] >>= count; shifted = min(32, shifted + count)
        else:
            raise AssertionError(f"unmodeled instruction {op:04x}")
        pc = next_pc
        assert cycles < 100 * len(words), "instruction model made no bounded progress"
    return outputs, cycles


def crc(values):
    return zlib.crc32(struct.pack(f"<{len(values)}I", *values))


def check_record(record):
    actual = record.get("crc")
    want = [crc(expected(axis_words(record[key], record["axis_halfwords"]), phase, record["base"]))
            for key in ("seed_i", "seed_q") for phase in (0, 1)]
    assert record.get("cmd") == "ptrbench" and record.get("errors") == 0, record
    assert record.get("completed") == record.get("reps") and record.get("mismatches") == 0, record
    assert actual == want, (actual, want)
    assert record.get("txstall_mask") == 0 and record.get("dma_error_mask") == 0, record
    return want


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="ptrprog-native-") as build:
        exe = Path(build) / "ptrprog_test"
        subprocess.run(["cc", "-O2", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        *fwsrc.INCLUDES, str(ROOT / "host/native/ptrprog_test.c"), "-o", str(exe)], check=True)
        raw = subprocess.check_output([str(exe)])
    code = struct.unpack(f"<{len(raw) // 2}H", raw)
    cases = 0
    for seed in (0, 1, 0x1234abcd, 0x9e3779b9, 0xffffffff):
        for count in (1, 2, 3, 31, 256):
            words = axis_words(seed, count)
            for base in (0x20000000, 0x20020000, 0x20040000, 0x20060000):
                for phase in (0, 1):
                    got, cycles = execute(code, words, phase, base)
                    assert got == expected(words, phase, base), (seed, count, base, phase)
                    assert all(base <= value < base + 131072 and value % 16 == 0 for value in got)
                    if phase:
                        assert cycles == count * 57, cycles
                    cases += 1
    want = [crc(expected(axis_words(seed, 256), phase, 0x20020000))
            for seed in (0x1234abcd, 0x9e3779b9) for phase in (0, 1)]
    print(json.dumps(dict(instruction_model_cases=cases, model_cyc_per_sym=57 / 16,
                          expected_device_crc=want)))
    if args.record:
        text = args.record.read_text().strip().removeprefix("@")
        record = json.loads(text)
        check_record(record)
        print("ptrbench device record: independent host CRCs exact")


if __name__ == "__main__":
    main()
