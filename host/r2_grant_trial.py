"""Finite DMA-only grant experiment; never builds, flashes, or changes clocks.

Offline: python3 host/r2_grant_trial.py --self-test
Device: explicit --port and fresh --out are required. Four exact echo CRCs are
checked against independent Python words; this is not a waveform/FEC trial.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import zlib

import iqbench as iq


def source(axis, word):
    return ((0xA5C30000 if axis else 0x5A3C0000) ^ ((word * 0x9E3779B9) & 0xFFFFFFFF))


def reference_crc(axis, words):
    return zlib.crc32(b"".join(source(axis, k).to_bytes(4, "little") for k in range(words)))


def run_faults(r, batches, cpw, gap_us, fault, priority, expected_git, expected_hz=None):
    failures = []
    expected = dict(cmd="r2grant", batches=batches, cpw=cpw, gap_us=gap_us, fault=fault,
                    priority=priority, git=expected_git, published_words=batches * 512,
                    errors=36 if fault else 0, core1_tasks=0, dma_channels=12, pio_sms=4,
                    pio_words=5, mismatches=0, guards_ok=True, released=True)
    if expected_hz is not None:
        expected["sys_hz"] = expected_hz
    expected_words = [batches * 512] * 4
    if fault:
        expected_words[3] -= 512
    expected["issued"] = expected_words
    expected["captured"] = expected_words
    expected["crc"] = [reference_crc(j & 1, words) for j, words in enumerate(expected_words)]
    expected["expected_crc"] = expected["crc"]
    for key, value in expected.items():
        if r.get(key) != value:
            failures.append(f"{key}: got {r.get(key)!r}, expected {value!r}")
    mask = r.get("input_mask")
    if type(mask) is not int or not 0 < mask < 65536 or mask.bit_count() != 4:
        failures.append("invalid four-reader input mask")
    if r.get("null_mask") != mask:
        failures.append("all four independent null-trigger stops required")
    txstall = r.get("txstall_mask")
    if type(txstall) is not int or (fault and (txstall == 0 or txstall.bit_count() != 1)) or (not fault and txstall):
        failures.append("negative control needs one stopped-source SM; clean run needs zero TXSTALL")
    hz, elapsed = r.get("sys_hz"), r.get("elapsed_us")
    if type(hz) is not int or hz <= 0 or type(elapsed) is not int or elapsed < gap_us:
        failures.append("invalid SYS/elapsed or foreground gap was shortened")
    elif not fault and elapsed + 2 < batches * 512 * 128 * cpw * 1e6 / hz:
        failures.append("echo completed before the programmed PIO rate permits")
    if "error" in r:
        failures.append("firmware rejected command: " + str(r["error"]))
    return failures


def self_test():
    checked = 0
    for batches in (1, 2, 8):
        for cpw in (1, 2, 4):
            for hz in (128000000, 256000000):
                for fault in ((0, 1) if batches >= 2 else (0,)):
                    words = [batches * 512] * 4
                    if fault:
                        words[3] -= 512
                    crcs = [reference_crc(j & 1, k) for j, k in enumerate(words)]
                    r = dict(cmd="r2grant", git="fixture", sys_hz=hz, batches=batches,
                             cpw=cpw, gap_us=6000, fault=fault, priority=13,
                             published_words=batches * 512, errors=36 if fault else 0,
                             elapsed_us=max(6000, math.ceil(batches * 512 * 128 * cpw * 1e6 / hz)),
                             txstall_mask=8 if fault else 0, null_mask=0x249, input_mask=0x249,
                             mismatches=0, guards_ok=True, released=True, core1_tasks=0,
                             dma_channels=12, pio_sms=4, pio_words=5, issued=words, captured=words,
                             crc=crcs, expected_crc=crcs)
                    args = batches, cpw, 6000, fault, 13, "fixture", hz
                    assert run_faults(r, *args) == []
                    for key, value in (("git", "other"), ("guards_ok", False), ("released", False),
                                       ("errors", 0 if fault else 2), ("null_mask", 0),
                                       ("issued", [0] * 4), ("captured", [0] * 4),
                                       ("crc", [0] * 4), ("core1_tasks", 1),
                                       ("txstall_mask", 0 if fault else 1), ("elapsed_us", 0)):
                        damaged = copy.deepcopy(r)
                        damaged[key] = value
                        assert run_faults(damaged, *args), key
                        checked += 1
    print(f"Passed R2 grant runner: clean/short-prefix control, SYS128/256, three dividers; "
          f"{checked} corrupted metadata/CRC/ownership/stop cases rejected; no USB opened.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--port", help="explicit CDC device; no automatic discovery")
    parser.add_argument("--out", type=Path, help="fresh evidence directory")
    parser.add_argument("--build", type=Path, default=iq.ROOT / "firmware/build-r2")
    parser.add_argument("--expected-git")
    parser.add_argument("--batches", type=int, choices=range(1, 9), default=8)
    parser.add_argument("--cpws", nargs="+", type=int, choices=(1, 2, 4), default=[2, 1])
    parser.add_argument("--gap-us", type=int, default=6000)
    parser.add_argument("--fault", type=int, choices=(0, 1), default=0)
    parser.add_argument("--priorities", nargs="+", type=int, default=[13])
    parser.add_argument("--repeats", type=int, default=6)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.port is None or args.out is None:
        parser.error("device trials require --port and a fresh --out; use --self-test offline")
    if not 0 <= args.gap_us <= 20000 or args.repeats < 1 or any(not 0 <= p <= 15 for p in args.priorities):
        parser.error("need gap0..20000,repeats>=1,priorities0..15")
    if args.fault and args.batches < 2:
        parser.error("short-prefix negative control needs at least two batches")
    build = args.build.resolve()
    cache = (build / "CMakeCache.txt").read_text()
    if "IQ4HIST_EXPERIMENT:BOOL=ON" not in cache:
        parser.error("build must have IQ4HIST_EXPERIMENT=ON")
    built_git = re.search(r'^#define GIT_REV "([^"]+)"', (build / "git_rev.h").read_text(), re.M)
    if not built_git:
        parser.error("cannot read built git_rev.h identity")
    expected_git = args.expected_git or built_git.group(1)
    args.out.mkdir(parents=True, exist_ok=False)
    paths = [iq.ROOT / path for path in ("firmware/src/output/r2grant.c", "firmware/src/output/r2grant.h",
             "firmware/src/bench/bench_r2grant.c", "firmware/src/app/main.c", "firmware/src/cpu_shaping/iqgen.c",
             "firmware/CMakeLists.txt", "Makefile", "host/r2_grant_trial.py", "host/test_r2grant.py",
             "host/test_bench_r2grant.py", "host/iqbench.py")]
    hashes = {str(p.relative_to(iq.ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    for name in ("iqbench.uf2", "iqbench.elf", "CMakeCache.txt", "memmap_iq.ld", "git_rev.h"):
        path = build / name
        hashes["build/" + name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix in (".elf", ".uf2"):
            shutil.copy2(path, args.out / name)
    manifest = dict(started=dt.datetime.now().isoformat(), expected_git=expected_git,
                    batches=args.batches, cpws=args.cpws, gap_us=args.gap_us, fault=args.fault,
                    priorities=args.priorities, repeats=args.repeats, sha256=hashes,
                    scope="finite prepublished input grants, pin-free four-reader exact echo; no shaping or rolling publication")
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copy2(__file__, args.out / "driver.py")
    board = iq.Board(args.port)
    failed = False
    try:
        info, _ = board.cmd("info")
        (args.out / "info.json").write_text(json.dumps(info, indent=2) + "\n")
        if info.get("git") != expected_git or type(info.get("clk_hz")) is not int:
            raise SystemExit("firmware identity or clock missing/mismatched; see info.json")
        for cpw in args.cpws:
            for priority in args.priorities:
                for repeat in range(args.repeats):
                    command = f"r2grant {args.batches} {cpw} {args.gap_us} {args.fault} {priority}"
                    result, _ = board.cmd(command, timeout=5)
                    result["faults"] = run_faults(result, args.batches, cpw, args.gap_us,
                                                 args.fault, priority, expected_git, info["clk_hz"])
                    result["command"], result["repeat"] = command, repeat
                    with (args.out / "runs.jsonl").open("a") as file:
                        file.write(json.dumps(result) + "\n")
                    print(command, repeat, json.dumps({k: result.get(k) for k in
                          ("elapsed_us", "errors", "txstall_mask", "issued", "captured", "faults")}), flush=True)
                    failed |= bool(result["faults"])
    finally:
        board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
