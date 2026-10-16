"""Isolated C3 mapper comparison; no hardware unless --port and --out are given.

The RISC-V image compares the portable C mapper with explicit Zbkb helpers on
the same Hazard3 core. It is not a mixed-architecture or complete FEC benchmark.
The Arm emulation image only checks the harness and requires --allow-emulated.
"""
from __future__ import annotations
import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil
import numpy as np
import iqbench as iq
from test_dvbs2_8psk import scalar_frame
from test_dvbs2_8psk_codes import packed_codes

ROOT = Path(__file__).resolve().parents[1]
RATES = {5: "2/3", 6: "3/4", 8: "5/6"}
GROUPS = 2048
SYMBOLS = 22194


def fnv(words):
    result = 2166136261
    for word in words: result = ((result ^ int(word)) * 16777619) & 0xffffffff
    return result


def frame_bits():
    words = []
    value = 0xd1b54a35
    for _ in range(2025):
        value ^= value << 13 & 0xffffffff
        value ^= value >> 17
        value ^= value << 5 & 0xffffffff
        words.append(value)
    return np.unpackbits(np.asarray(words, dtype=">u4").view(np.uint8))


def reference(code, frames, fault=0):
    if code not in RATES or type(frames) is not int or not 1 <= frames <= 10000 or fault not in (0, 1):
        raise ValueError("unsupported code, frame count or fault")
    groups, fill = divmod(frames * SYMBOLS, 16)
    first = max(0, groups - GROUPS)
    first_frame = first * 16 // SYMBOLS
    frame = scalar_frame(frame_bits(), RATES[code], False, True, 0)
    points = np.tile(frame, frames - first_frame)[first * 16 - first_frame * SYMBOLS:]
    packed = packed_codes(points[:(groups-first)*16]).T
    rings = np.zeros((2, GROUPS), dtype=np.uint32)
    rings[:, np.arange(first, groups) & (GROUPS-1)] = packed
    # Derive the small pending group with exactly the same scalar constellation
    # conversion, padding only here to make its low two-bit fields accessible.
    pending = packed_codes(points[(groups-first)*16:], flush=True)
    mask = (1 << (2*fill)) - 1
    carry = [int(x) & mask for x in pending[0]] if fill else [0, 0]
    hashes = [[fnv(axis), fnv(axis)] for axis in rings]
    if fault:
        rings[0, (groups-1) & (GROUPS-1)] ^= 1
        hashes[0][1] = fnv(rings[0])
    return dict(published_groups=groups, pending_fill=fill, pending_codes=carry,
                hash_i=hashes[0], hash_q=hashes[1])


def run_faults(result, code, frames, fault, allow_emulated=False):
    errors = []
    expected = dict(bench="r2map", code=code, frames=frames, fault=fault,
                    symbols_per_frame=SYMBOLS, slice_words=8, ring_groups=GROUPS, equal=not bool(fault))
    if allow_emulated:
        expected.update(arch="arm", candidate="emulated-not-hazard3")
    else:
        expected.update(arch="hazard3", candidate="zbkb")
    expected.update(reference(code, frames, fault))
    for key, value in expected.items():
        if type(result.get(key)) is not type(value) or result[key] != value:
            errors.append(f"{key}={result.get(key)!r}, expected {value!r}")
    if type(result.get("sys_hz")) is not int or result["sys_hz"] <= 0:
        errors.append("invalid reported system clock")
    for key in ("cycles", "max_frame_cycles", "max_slice_cycles"):
        values = result.get(key)
        if not isinstance(values, list) or len(values) != 2 or any(type(x) is not int or x <= 0 for x in values):
            errors.append("invalid " + key)
    if not errors:
        for k in range(2):
            if not result["max_slice_cycles"][k] <= result["max_frame_cycles"][k] <= result["cycles"][k]:
                errors.append("inconsistent cycle maxima")
    return errors


def self_test():
    for code in RATES:
        for frames in (1, 2, 3, 8, 9, 10000):
            for fault in (0, 1):
                sample = dict(bench="r2map", arch="hazard3", candidate="zbkb", sys_hz=128000000,
                              code=code, frames=frames, fault=fault, equal=not bool(fault),
                              symbols_per_frame=SYMBOLS, slice_words=8, ring_groups=GROUPS,
                              cycles=[120000*frames, 70000*frames], max_frame_cycles=[120000, 70000],
                              max_slice_cycles=[2000, 1200], **reference(code, frames, fault))
                assert not run_faults(sample, code, frames, fault)
                wrong = copy.deepcopy(sample); wrong["hash_q"][1] ^= 1
                assert run_faults(wrong, code, frames, fault)
                wrong = copy.deepcopy(sample); wrong["equal"] = fault
                assert run_faults(wrong, code, frames, fault)
                wrong = copy.deepcopy(sample); wrong.update(arch="arm", candidate="emulated-not-hazard3")
                assert run_faults(wrong, code, frames, fault)
                assert not run_faults(wrong, code, frames, fault, True)
    print("r2_hazard3_trial self-test: independent scalar PL/Gold reference, ring wrap, carry, "
          "corruption control and emulation rejection pass; no hardware accessed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--reference", action="store_true")
    parser.add_argument("--codes", nargs="+", type=int, choices=tuple(RATES), default=[5, 8, 6])
    parser.add_argument("--frames", type=int, default=100)
    parser.add_argument("--faults", nargs="+", type=int, choices=(0, 1), default=[0])
    parser.add_argument("--port")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--build", type=Path, default=ROOT / "firmware/build-r2-hazard3")
    parser.add_argument("--allow-emulated", action="store_true", help="Arm harness correctness only")
    args = parser.parse_args()
    if args.self_test:
        self_test(); return
    if not 1 <= args.frames <= 10000: parser.error("--frames must be 1..10000")
    if args.reference:
        for code in args.codes:
            for fault in args.faults:
                print(json.dumps(dict(code=code, frames=args.frames, fault=fault,
                                      **reference(code, args.frames, fault))))
        return
    if not args.port or not args.out:
        parser.error("device runs require explicit --port and --out; --self-test and --reference stay offline")
    build = args.build.resolve()
    cache = (build / "CMakeCache.txt").read_text()
    required = "R2_HAZARD3_EMULATE:BOOL=" + ("ON" if args.allow_emulated else "OFF")
    if required not in cache: parser.error("build emulation flag differs from --allow-emulated")
    args.out.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in ("r2map.uf2", "r2map.elf", "CMakeCache.txt"):
        path = build / name
        hashes["build/" + name] = hashlib.sha256(path.read_bytes()).hexdigest()
        shutil.copy2(path, args.out / name)
    for name in ("firmware/src/dvbs2/dvbs2_8psk.c", "firmware/src/dvbs2/r2_hazard3_bits.h",
                 "firmware/r2_hazard3/CMakeLists.txt", "firmware/r2_hazard3/main.c", "host/r2_hazard3_trial.py"):
        hashes[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), code=args.codes, frames=args.frames,
                    fault=args.faults, port=args.port, sha256=hashes,
                    scope="isolated same-core mapper; RISC-V portable versus Zbkb; no FEC, shaping or mixed launch",
                    emulated=args.allow_emulated)
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    board = iq.Board(args.port)
    failed = False
    try:
        for code in args.codes:
            for fault in args.faults:
                result, _ = board.cmd(f"r2map {code} {args.frames} {fault}", timeout=120)
                result["faults"] = run_faults(result, code, args.frames, fault, args.allow_emulated)
                result["timing_scope"] = "emulation-only" if args.allow_emulated else "same-Hazard3-core"
                with (args.out / "runs.jsonl").open("a") as output:
                    output.write(json.dumps(result) + "\n")
                print(json.dumps(result), flush=True)
                failed |= bool(result["faults"])
    finally:
        board.s.close()
    if failed: raise SystemExit(1)


if __name__ == "__main__": main()
