"""Check that the M33 mapper leaf remains safe to copy to caller-owned SRAM.

This compiles and inspects ARM instructions only. It never opens hardware and
does not report native wall time or instruction counts as device timings.
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


def tool(name):
    candidate = Path.home() / ".pico-sdk/toolchain/14_2_Rel1/bin" / name
    return str(candidate) if candidate.exists() else shutil.which(name)


def main():
    cc, dump = tool("arm-none-eabi-gcc"), tool("arm-none-eabi-objdump")
    assert cc and dump, "ARM GCC and objdump are required for the copied-code check"
    with tempfile.TemporaryDirectory(prefix="c4-mapper-pic-") as temp:
        obj = Path(temp) / "map.o"
        subprocess.run([cc, "-O3", "-std=c11", "-Wall", "-Wextra", "-Werror",
                        "-mcpu=cortex-m33", "-mthumb", "-march=armv8-m.main+fp+dsp",
                        "-mfloat-abi=softfp", "-mcmse", "-DNDEBUG",
                        "-ffunction-sections", "-fdata-sections", "-DIQ_ON_DEVICE=1",
                        *fwsrc.INCLUDES, "-c",
                        str(ROOT / "firmware/src/dvbs2/dvbs2_8psk.c"), "-o", str(obj)], check=True)
        headers = subprocess.check_output([dump, "-h", str(obj)], text=True)
        section = re.search(r"\s+c4_mapper\s+([0-9a-f]+)\s", headers)
        assert section
        size = int(section[1], 16)
        assert 0 < size <= 1024, f"copied mapper needs {size} bytes; arena reserves 1024"
        relocations = subprocess.check_output([dump, "-r", "-j", "c4_mapper", str(obj)], text=True)
        assert "R_ARM_" not in relocations, relocations
        disassembly = subprocess.check_output([dump, "-d", "-j", "c4_mapper", str(obj)], text=True)
        instructions = []
        branches = []
        for line in disassembly.splitlines():
            match = re.match(r"\s*([0-9a-f]+):\s+(?:[0-9a-f]{4}\s+)+\s*(\S+)\s*(.*)", line)
            if not match: continue
            address, op, args = int(match[1], 16), match[2], match[3]
            instructions.append((address, op, args))
            assert op not in ("bl", "bl.w", "blx", "bx"), line
            assert not re.search(r"\[pc\b", args) and op not in ("adr", "adr.w"), line
            if re.fullmatch(r"b(?:eq|ne|cs|cc|mi|pl|vs|vc|hi|ls|ge|lt|gt|le)?(?:\.[nw])?", op):
                target = int(args.split()[0], 16)
                assert 0 <= target < size, line
                if target < address: branches.append((target, address))
        assert len(branches) == 1, branches
        start, end = branches[0]
        loop = [(op, args) for address, op, args in instructions if start <= address <= end]
    print(json.dumps(dict(result="position-independent", section_bytes=size,
                          loop_instructions=len(loop),
                          loop_loads=sum(op.startswith("ldr") for op, _ in loop),
                          loop_stores=sum(op.startswith("str") for op, _ in loop),
                          device_timing=False)))


if __name__ == "__main__": main()
