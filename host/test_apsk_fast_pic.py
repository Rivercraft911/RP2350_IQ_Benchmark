"""Check the A3 APSK leaves as the M33 compiler emits them (no hardware, no timing).

The table-driven PL mapper leaves are copied whole into caller SRAM, so the
`apsk_fast` section must fit its code reservation and contain no relocations,
calls, PC-relative data or branches leaving the section. The multiply-form
32APSK conversion runs in place from SCRATCH_Y and must not call out. Static
instruction counts of each hot loop are reported for the cycle estimate in
docs/rd/a3-apsk.md; they are compiler output, not device measurements.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import fwsrc

ROOT = Path(__file__).resolve().parents[1]
FLAGS = ["-O3", "-std=c11", "-Wall", "-Wextra", "-Werror", "-mcpu=cortex-m33", "-mthumb",
         "-march=armv8-m.main+fp+dsp", "-mfloat-abi=softfp", "-mcmse", "-DNDEBUG",
         "-ffunction-sections", "-fdata-sections", "-DIQ_ON_DEVICE=1"]
BRANCH = re.compile(r"b(?:eq|ne|cs|cc|hs|lo|mi|pl|vs|vc|hi|ls|ge|lt|gt|le)?(?:\.[nw])?|cbz|cbnz")


def tool(name):
    candidate = Path.home() / ".pico-sdk/toolchain/14_2_Rel1/bin" / name
    return str(candidate) if candidate.exists() else shutil.which(name)


def code_words():
    text = (ROOT / "firmware/src/dvbs2/dvbs2_apsk.h").read_text()
    return int(re.search(r"DVBS2_APSK_FAST_CODE_WORDS = (\d+)", text)[1])


def disassemble(dump, obj, section):
    headers = subprocess.check_output([dump, "-h", str(obj)], text=True)
    match = re.search(r"\s+" + re.escape(section) + r"\s+([0-9a-f]+)\s", headers)
    assert match, f"section {section} missing"
    relocations = subprocess.check_output([dump, "-r", "-j", section, str(obj)], text=True)
    listing = subprocess.check_output([dump, "-d", "-j", section, str(obj)], text=True)
    functions, current = {}, None
    for line in listing.splitlines():
        head = re.match(r"([0-9a-f]+) <(\w+)>:", line)
        if head:
            current = head[2]
            functions[current] = []
            continue
        item = re.match(r"\s*([0-9a-f]+):\s+(?:[0-9a-f]{4}\s+)+\s*(\S+)\s*(.*)", line)
        if item and current:
            functions[current].append((int(item[1], 16), item[2], item[3]))
    return int(match[1], 16), relocations, functions


def loop_counts(instructions):
    """Instructions between each backward branch target and the branch."""
    loops = []
    for address, op, args in instructions:
        if BRANCH.fullmatch(op):
            target = int(args.split()[-2] if op.startswith("cb") else args.split()[0], 16)
            if target < address:
                body = [entry for entry in instructions if target <= entry[0] <= address]
                loops.append(dict(instructions=len(body),
                                  loads=sum(e[1].startswith("ldr") or e[1].startswith("ldm") for e in body),
                                  stores=sum(e[1].startswith("str") or e[1].startswith("stm") for e in body),
                                  multiplies=sum(e[1].startswith(("mul", "mla")) for e in body)))
    return loops


def main():
    cc, dump = tool("arm-none-eabi-gcc"), tool("arm-none-eabi-objdump")
    assert cc and dump, "ARM GCC and objdump are required"
    with tempfile.TemporaryDirectory(prefix="a3-apsk-pic-") as temp:
        mapper, prep = Path(temp) / "mapper.o", Path(temp) / "prep.o"
        for source, obj in (("dvbs2_apsk.c", mapper), ("iq32offsets_mla.c", prep)):
            subprocess.run([cc, *FLAGS, *fwsrc.INCLUDES, "-c",
                            str(fwsrc.path(source)), "-o", str(obj)], check=True)
        size, relocations, functions = disassemble(dump, mapper, "apsk_fast")
        assert 0 < size <= 4 * code_words(), f"leaves need {size} bytes; arena reserves {4 * code_words()}"
        assert "R_ARM_" not in relocations, relocations
        assert set(functions) == {"fast16_run0", "fast16_run2", "fast32_run"}, set(functions)
        report = dict(result="position-independent", section_bytes=size, code_reservation_bytes=4 * code_words(),
                      device_timing=False, leaves={})
        for name, instructions in functions.items():
            for address, op, args in instructions:
                assert op not in ("bl", "bl.w", "blx", "bx"), (name, op, args)
                assert not re.search(r"\[pc\b", args) and op not in ("adr", "adr.w"), (name, op, args)
                if BRANCH.fullmatch(op):
                    target = int(args.split()[-2] if op.startswith("cb") else args.split()[0], 16)
                    assert 0 <= target < size, (name, op, args)
            loops = loop_counts(instructions)
            assert len(loops) == 1, (name, loops)
            report["leaves"][name] = dict(loop_per_32_symbols=loops[0], total_instructions=len(instructions))
        size, relocations, functions = disassemble(dump, prep, ".scratch_y.iq32prep")
        instructions = functions["iq32offsets_from_native_mla"]
        assert not any(op in ("bl", "bl.w", "blx") for _, op, _ in instructions), "SCRATCH_Y leaf calls out"
        loops = [loop for loop in loop_counts(instructions) if loop["multiplies"]]
        assert loops and all(loop["multiplies"] >= 6 for loop in loops), loops
        report["iq32offsets_from_native_mla"] = dict(section_bytes=size, unrolled_loop_per_4_symbols=loops[0],
                                                     placement="scratch_y, linked in place")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
