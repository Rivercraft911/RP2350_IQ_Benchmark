#!/usr/bin/env python3
"""Rebuild B2/round-2 archives and compare linker placement; never accesses USB/Git writes."""
import argparse
import json
import os
from pathlib import Path
import struct
import subprocess


REVISIONS = {
    "b2": "2081cb740bd8588c9a0efa159422b552e4fc5f26",
    "merge": "0b89b341ce1bf4cecfec1c16db5632d3b23572bf",
}
OBJECTS = ("dual_dma", "iq4ptr", "bench4fec", "iq4hist", "pvspi", "dvbs2", "dvbs2_8psk")
SYMBOLS = {
    "ring_buf", "in_buf", "link_ring", "tab_q", "tab_i", "bb_buf", "cap_buf", "bch_tab",
    "fe", "credits", "live", "state", "cfg", "pvspi", "ldpc_matrix", "dvbs2_bch",
    "dvbs2_ldpc", "dvbs2_frame_begin_into", "dvbs2_frame_ldpc_into", "dvbs2_8psk_frame",
    "dvbs2_bch160_stream", "pv_gcs_bbframe", "cs_isr", "pvspi_next_packet",
    "iq4hist_pack_group", "encode_frame", "encoder_task", "bench4fec", "halt_outputs",
    "sample_faults", "dual_dma_faults", "__scratch_x_end__", "__scratch_y_end__",
    "__StackBottom", "__StackTop", "__StackOneBottom", "__StackOneTop", "__end__",
}


def run(argv, cwd=None, env=None):
    return subprocess.check_output(argv, cwd=cwd, env=env, text=True)


def elf_sections(path):
    """Read ELF32 section bytes without additional Python packages."""
    raw = path.read_bytes()
    if raw[:6] != b"\x7fELF\x01\x01":
        raise ValueError(f"expected little-endian ELF32: {path}")
    offset = struct.unpack_from("<I", raw, 32)[0]
    size, count, strings_index = struct.unpack_from("<HHH", raw, 46)
    entries = [struct.unpack_from("<10I", raw, offset + i * size) for i in range(count)]
    strings = entries[strings_index]
    names = raw[strings[4]:strings[4] + strings[5]]
    result = {}
    for entry in entries:
        name = names[entry[0]:].split(b"\0", 1)[0].decode()
        result[name] = {"address": f"0x{entry[3]:08x}", "bytes": entry[5],
                        "executable": bool(entry[2] & 4),
                        "data": raw[entry[4]:entry[4] + entry[5]]}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path("/tmp/b3-regression"),
                        help="fresh export directories are PREFIX-b2 and PREFIX-merge")
    parser.add_argument("--skip-build", action="store_true", help="compare existing exports")
    parser.add_argument("--json", type=Path, required=True, help="small comparison output")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sdk = Path(os.environ.get("PICO_SDK_PATH", str(Path.home() / ".pico-sdk/sdk/2.2.0")))
    toolchain = Path(os.environ.get("PICO_TOOLCHAIN_PATH",
                                    str(Path.home() / ".pico-sdk/toolchain/14_2_Rel1")))
    env = dict(os.environ, PICO_SDK_PATH=str(sdk), PICO_TOOLCHAIN_PATH=str(toolchain))
    report = {
        "kind": "offline_source_object_and_linker_comparison_no_device_measurement",
        "sdk": "2.2.0",
        "compiler": run([str(toolchain / "bin/arm-none-eabi-gcc"), "--version"]).splitlines()[0],
        "build": "Release; IQ4HIST_EXPERIMENT=ON; make build BUILD=firmware/build-b3-regression",
        "version_metadata": "archive git_rev.cmake writes each original ten-digit revision",
        "images": {},
    }
    objects = {}
    for name, revision in REVISIONS.items():
        source = Path(str(args.prefix) + "-" + name)
        flags = ["-DCMAKE_BUILD_TYPE=Release", "-DIQ4HIST_EXPERIMENT=ON"]
        if name == "merge":
            flags += ["-DCORE0_STACK_SRAM_HI=ON"]
        if not args.skip_build:
            source.mkdir(parents=True, exist_ok=False)
            archive = subprocess.Popen(["git", "archive", revision], cwd=root, stdout=subprocess.PIPE)
            subprocess.run(["tar", "-x", "-C", str(source)], stdin=archive.stdout, check=True)
            archive.stdout.close()
            if archive.wait():
                raise RuntimeError("git archive failed")
            (source / "firmware/git_rev.cmake").write_text(
                'file(WRITE "${OUT}" [=[#define GIT_REV "' + revision[:10] + '"\n]=])\n')
            with Path(str(args.prefix) + f"-{name}-config.log").open("w") as log:
                subprocess.run(["cmake", "-S", "firmware", "-B", "firmware/build-b3-regression",
                                "-G", "Ninja", *flags], cwd=source, env=env,
                               stdout=log, stderr=subprocess.STDOUT, check=True)
            with Path(str(args.prefix) + f"-{name}-build.log").open("w") as log:
                subprocess.run(["make", "build", "BUILD=firmware/build-b3-regression"],
                               cwd=source, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
        build = source / "firmware/build-b3-regression"
        nm = run([str(toolchain / "bin/arm-none-eabi-nm"), "-n", "-S", str(build / "iqbench.elf")])
        symbols = {}
        for line in nm.splitlines():
            parts = line.split()
            if len(parts) < 3 or parts[-1] not in SYMBOLS:
                continue
            item = {"address": f"0x{int(parts[0], 16):08x}"}
            if len(parts) == 4:
                item["bytes"] = int(parts[1], 16)
            symbols.setdefault(parts[-1], []).append(item)
        sections = {key: {k: value[k] for k in ("address", "bytes")}
                    for key, value in elf_sections(build / "iqbench.elf").items()
                    if key in {".text", ".rodata", ".waveform_ring", ".input_ring", ".data", ".bss",
                               ".sram_hi", ".heap", ".scratch_x", ".scratch_y", ".stack_dummy",
                               ".stack1_dummy"}}
        report["images"][name] = {"revision": revision, "cmake_flags": flags,
                                   "sections": sections, "symbols": symbols}
        objects[name] = {obj: elf_sections(build / f"CMakeFiles/iqbench.dir/src/{obj}.c.o")
                         for obj in OBJECTS}
    comparisons = {}
    for obj in OBJECTS:
        a = {k: v["data"] for k, v in objects["b2"][obj].items() if v["executable"] and v["bytes"]}
        b = {k: v["data"] for k, v in objects["merge"][obj].items() if v["executable"] and v["bytes"]}
        same = sorted(key for key in a.keys() & b.keys() if a[key] == b[key])
        changed = sorted(key for key in a.keys() & b.keys() if a[key] != b[key])
        comparisons[obj] = {"identical_sections": len(same), "identical_bytes": sum(len(a[k]) for k in same),
                            "changed": [{"section": k, "b2_bytes": len(a[k]), "merge_bytes": len(b[k])}
                                        for k in changed], "added": sorted(b.keys() - a.keys()),
                            "removed": sorted(a.keys() - b.keys())}
    report["executable_object_sections_before_relocation"] = comparisons
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Wrote {args.json}")


if __name__ == "__main__":
    main()
