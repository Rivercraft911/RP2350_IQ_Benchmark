"""CPU-only two-core 8PSK FEC cadence trial, with independent final-ring verification.

The default and --self-test access no hardware. A device run requires both --out
and an explicit --port. Build and flash separately; this program never does either.
The source is an in-memory cyclic 256-byte generic stream, byte j = (29*j+7)&255,
adapted into real GCS BBFRAMEs. This does not measure external/PV-SPI ingress or output.

  python3 host/c3_fec_trial.py --self-test
  python3 host/c3_fec_trial.py --reference --codes 5 8 6 --frames 3
  python3 host/c3_fec_trial.py --port /dev/cu.usbmodem... --out results/c3/quick --ms 3000
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import functools
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import zlib

import numpy as np

import iqbench as iq
from pulse_profiles import FOUR_LEVELS_Q30
from test_dvbs2_8psk import ref, scalar_frame

RATES = {5: "2/3", 6: "3/4", 8: "5/6"}
FRAME_SYMBOLS = 22194
SYMBOL_CYCLES = 8
SOURCE_BYTES = 256
SOURCE = bytes((29 * j + 7) & 255 for j in range(SOURCE_BYTES))
STAGES = ("gcs", "bch", "ldpc", "map", "delay")
# Keep these masks in sync with firmware/src/dvbs2/c3_fec.h. The three-table BCH
# diagnostic needs c3pair layout4; the cadence benchmark has no table arena.
C8_ALL = 2047
C8_FEC_ALL = C8_ALL & ~1024
C8_BEST_BASE = 27 | 32 | 64 | 128
C8_BEST = C8_BEST_BASE | 1024


def option_mask(value: str) -> int:
    """The named cadence profile excludes c3pair's large BCH-table diagnostic."""
    if value.lower() == "best":
        return C8_BEST_BASE
    try:
        mask = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an integer FEC mask or 'best'") from exc
    if not 0 <= mask <= C8_ALL:
        raise argparse.ArgumentTypeError(f"FEC mask must be 0..{C8_ALL} or 'best'")
    return mask


def gcs_bbframe(index: int, code_index: int) -> np.ndarray:
    """Random-access GCS BBFRAME; the source cursor never resets at frame boundaries."""
    if type(index) is not int or index < 0:
        raise ValueError("frame index must be a nonnegative integer")
    rate = RATES[code_index]
    count = (ref.code(rate, False).kbch - 80) // 8
    start = index * count % len(SOURCE)
    data = (SOURCE * ((start + count + len(SOURCE) - 1) // len(SOURCE)))[start:start + count]
    return ref.bbframe(np.unpackbits(np.frombuffer(data, np.uint8)), rate,
                       short=False, ro=0.20, sync=0xB9)


@functools.lru_cache(maxsize=24)
def frame_codes(index: int, code_index: int) -> np.ndarray:
    """Independent scalar BCH/LDPC/Gold/PL symbols, quantized by their geometric coordinates."""
    rate = RATES[code_index]
    fec = ref.fecframe(gcs_bbframe(index, code_index), rate, False)
    points = scalar_frame(fec, rate, False, True, 0)
    assert len(points) == FRAME_SYMBOLS
    levels = np.asarray(FOUR_LEVELS_Q30, np.float64) / 2**30
    codes = np.asarray([np.abs(axis[:, None] - levels).argmin(axis=1)
                        for axis in (points.real, points.imag)], np.uint32)
    codes.flags.writeable = False
    return codes


def pack_codes(codes: np.ndarray) -> np.ndarray:
    """Sixteen chronological 2-bit codes per little-endian uint32, separately by axis."""
    codes = np.asarray(codes, np.uint32)
    if codes.ndim != 2 or codes.shape[0] != 2 or codes.shape[1] % 16 or np.any(codes > 3):
        raise ValueError("expected two complete groups of two-bit axis codes")
    shifts = 2 * np.arange(16, dtype=np.uint32)
    return (codes.reshape(2, -1, 16) << shifts).sum(axis=2, dtype=np.uint32).astype("<u4")


def ring_reference(code_index: int, frames: int, ring_groups: int) -> dict:
    """Final physical rings from only the retained stream window; partial groups stay pending.

    Frame length is 2 modulo 16. Concatenate exact symbols before grouping: padding
    individual frames would corrupt every subsequent boundary and the ring CRCs.
    """
    if code_index not in RATES or type(frames) is not int or frames < 0:
        raise ValueError("unsupported code or frame count")
    if (type(ring_groups) is not int or not 1 <= ring_groups <= 65536 or
            ring_groups & (ring_groups - 1)):
        raise ValueError("ring_groups must be a power of two in 1..65536")
    symbols = frames * FRAME_SYMBOLS
    groups, fill = divmod(symbols, 16)
    first_group = max(0, groups - ring_groups)
    first_symbol = first_group * 16
    rings = np.zeros((2, ring_groups), dtype="<u4")
    pending = [0, 0]
    if symbols:
        first_frame = first_symbol // FRAME_SYMBOLS
        points = np.concatenate([frame_codes(k, code_index) for k in range(first_frame, frames)], axis=1)
        points = points[:, first_symbol - first_frame * FRAME_SYMBOLS:]
        complete = (groups - first_group) * 16
        values = pack_codes(points[:, :complete])
        indices = np.arange(first_group, groups, dtype=np.uint64) & (ring_groups - 1)
        rings[:, indices] = values
        for axis in range(2):
            pending[axis] = sum(int(value) << (2 * j) for j, value in enumerate(points[axis, complete:]))
    return dict(published_groups=groups, pending_fill=fill, pending_codes=pending,
                code_crc=[zlib.crc32(axis.tobytes()) for axis in rings])


def _nonnegative_int(value):
    return type(value) is int and value >= 0


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def run_faults(result: dict, code_index: int, ms: int, slow_us: int,
               expected_git: str | None = None, cpw: int = 1,
               options: int = 0, gap_us: int = 128) -> list[str]:
    """Strict protocol/cadence/CRC checks. A slow control succeeds only by exposing lateness."""
    if result.get("error"):
        return [f"firmware error: {result['error']}"]
    r = result
    faults = []
    expected = dict(cmd="c3fec", code_index=code_index, s2_code="normal " + RATES[code_index],
                    ms=ms, slow_us=slow_us, frame_symbols=FRAME_SYMBOLS,
                    frame_cycles=FRAME_SYMBOLS * SYMBOL_CYCLES * cpw, cpw=cpw, pilots=True, gold=0,
                    source="memory_gcs", source_period_bytes=SOURCE_BYTES, ring_groups=4096)
    if expected_git:
        expected["git"] = expected_git
    if options or "options" in r:
        expected["options"] = options
    if gap_us != 128 or "service_gap_limit_us" in r:
        expected["service_gap_limit_us"] = gap_us
    faults += [f"{key}={r.get(key)!r}, expected {value!r}"
               for key, value in expected.items() if r.get(key) != value]
    if not isinstance(r.get("git"), str) or not r["git"]:
        faults.append("firmware git identity missing")
    if not _nonnegative_int(r.get("clk_hz")) or r["clk_hz"] == 0:
        faults.append("invalid system clock")
    counters = ("elapsed_us", "frames_target", "consumed_frames", "published_frames", "published_groups",
                "pending_fill", "late_frames", "max_lateness_us", "max_service_gap_us", "errors")
    invalid = [key for key in counters if not _nonnegative_int(r.get(key))]
    if invalid:
        return faults + ["invalid counters: " + ", ".join(invalid)]
    if not r["elapsed_us"]:
        faults.append("zero elapsed duration")
    if _nonnegative_int(r.get("clk_hz")) and r["clk_hz"]:
        target = max(2, ms * r["clk_hz"] // (FRAME_SYMBOLS * SYMBOL_CYCLES * cpw * 1000))
        if r["frames_target"] != target:
            faults.append("target frame count differs from requested frame cadence")
    if not 0 <= r["consumed_frames"] <= r["published_frames"] <= r["frames_target"]:
        faults.append("consumption and in-order publication differ")
    try:
        reference = ring_reference(code_index, r["published_frames"], r["ring_groups"])
        for key, expected_value in reference.items():
            if key == "pending_codes" and key not in r:
                continue
            if r.get(key) != expected_value:
                faults.append(f"{key} differs from independent continuous GCS/FEC/PL reference")
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        faults.append("invalid ring metadata: " + str(exc))
    if slow_us:
        if not (r["late_frames"] == 1 and r["errors"] and r["max_lateness_us"]):
            faults.append("slow negative control did not expose publication lateness")
    else:
        if r["late_frames"] or r["max_lateness_us"] or r["errors"]:
            faults.append("publication missed the frame cadence or firmware reported errors")
        if r["published_frames"] != r["frames_target"] or r["consumed_frames"] != r["frames_target"]:
            faults.append("benchmark did not encode and consume every target frame")
        if not ms * 1000 <= r["elapsed_us"] <= ms * 1000 + 100000:
            faults.append("elapsed duration differs from request")
        if not 0 < r["max_service_gap_us"] <= gap_us:
            faults.append(f"core 0 service gap exceeded {gap_us} us")
    cores = r.get("core")
    if not isinstance(cores, list) or len(cores) != 2:
        return faults + ["missing two-core accounting"]
    core_frames = 0
    for index, core in enumerate(cores):
        if not isinstance(core, dict):
            faults.append(f"invalid core {index} accounting")
            continue
        numeric = ("frames", "busy_cycles", "max_frame_cycles", "max_slice_cycles")
        if any(not _nonnegative_int(core.get(key)) for key in numeric):
            faults.append(f"invalid core {index} counters")
            continue
        core_frames += core["frames"]
        if not slow_us and core["frames"] != (r["published_frames"] + 1 - index) // 2:
            faults.append(f"core {index} did not own its even/odd frames")
        stages = core.get("stage_cycles")
        if (not isinstance(stages, dict) or set(stages) != set(STAGES) or
                any(not _nonnegative_int(stages[key]) for key in STAGES)):
            faults.append(f"invalid core {index} stage cycle accounting")
        elif sum(stages.values()) > core["busy_cycles"]:
            faults.append(f"core {index} stages exceed total busy cycles")
        if core["frames"] and not 0 < core["max_frame_cycles"] <= core["busy_cycles"]:
            faults.append(f"invalid core {index} maximum frame time")
        if core["frames"] and not 0 < core["max_slice_cycles"] <= core["max_frame_cycles"]:
            faults.append(f"invalid core {index} maximum slice time")
        if (not slow_us and gap_us == 128 and _nonnegative_int(r.get("clk_hz")) and
                core["max_slice_cycles"] > r["clk_hz"] * 128 // 1000000):
            faults.append(f"core {index} slice exceeded 128 us")
        if _finite(r.get("clk_hz")) and r["clk_hz"] > 0 and r["elapsed_us"] > 0:
            utilization = core["busy_cycles"] / (r["elapsed_us"] * r["clk_hz"] / 1e6)
            if not 0 <= utilization <= 1.01:
                faults.append(f"invalid core {index} utilization")
            if "busy" in core and (not _finite(core["busy"]) or
                                   not math.isclose(utilization, core["busy"], abs_tol=0.00015)):
                faults.append(f"core {index} utilization differs from charged cycles")
    if not slow_us and core_frames != r["published_frames"]:
        faults.append("per-core frame counts differ from publication")
    if slow_us and not r["published_frames"] <= core_frames <= r["published_frames"] + 2:
        faults.append("negative control completed frames differ from bounded in-flight slots")
    return list(dict.fromkeys(faults))


def self_test():
    """No hardware: exercise real FEC references and strict positive/negative validation."""
    base = dict(cmd="c3fec", git="self-test", clk_hz=128000000, code_index=5,
                s2_code="normal 2/3", ms=100, slow_us=0, pilots=True, gold=0,
                frame_symbols=FRAME_SYMBOLS, frame_cycles=FRAME_SYMBOLS * SYMBOL_CYCLES, cpw=1,
                elapsed_us=101300, frames_target=72, consumed_frames=72, published_frames=72, ring_groups=4096,
                late_frames=0, max_lateness_us=0, errors=0, max_service_gap_us=87,
                source="memory_gcs", source_period_bytes=SOURCE_BYTES,
                core=[dict(frames=n, busy_cycles=300000*n, max_frame_cycles=300000,
                           max_slice_cycles=6000,
                           stage_cycles=dict(gcs=1000*n, bch=100000*n, ldpc=150000*n, map=40000*n, delay=0))
                      for n in (36, 36)])
    base.update(ring_reference(5, 72, 4096))
    assert not run_faults(base, 5, 100, 0)
    bad = copy.deepcopy(base)
    bad["code_crc"][1] ^= 1
    assert any("code_crc" in fault for fault in run_faults(bad, 5, 100, 0))
    bad = copy.deepcopy(base)
    bad.update(late_frames=1, max_lateness_us=1, errors=1)
    assert any("cadence" in fault for fault in run_faults(bad, 5, 100, 0))
    bad["slow_us"] = 5000
    assert not run_faults(bad, 5, 100, 5000)
    bad.update(late_frames=0, max_lateness_us=0)
    assert any("control" in fault for fault in run_faults(bad, 5, 100, 5000))
    for code in RATES:
        actual = ring_reference(code, 3, 4096)
        assert actual["published_groups"] == 4161 and actual["pending_fill"] == 6
    print("c3_fec_trial self-test: independent GCS/FEC/PL/code rings, frame carry, CRC corruption "
          "and publication-lateness control checked; no hardware accessed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--reference", action="store_true", help="print independent final-ring CRCs; no serial")
    parser.add_argument("--codes", nargs="+", type=int, choices=tuple(RATES), default=[5, 8, 6])
    parser.add_argument("--frames", type=int, default=3, help="offline reference frame count")
    parser.add_argument("--ring-groups", type=int, default=4096)
    parser.add_argument("--ms", type=int, default=3000)
    parser.add_argument("--cpw", type=int, choices=(1, 2, 4), default=1,
                        help="symbol interval in units of eight SYS cycles (1 is 16 Msym/s at SYS128)")
    parser.add_argument("--slow-us", nargs="+", type=int, default=[0])
    parser.add_argument("--options", nargs="+", type=option_mask, default=[0],
                        help="FEC mask: C4 LDPC=1, mapper=2, BCH=4, fused GCS/BB=8, cached deadlines=16, "
                             "C5 LDPC=32, C6 mapper=64, C7 BCH copies=128, BCH multiword loads=256, "
                             "transpose copies=512; best=251. BCH three-table=1024 requires c3pair layout4; "
                             "0 retains C3")
    parser.add_argument("--gap-us", type=int, default=128,
                        help="128 for qualification; larger values are CPU-fit diagnostics only")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--port", help="explicit CDC device; no automatic serial discovery")
    parser.add_argument("--expected-git")
    parser.add_argument("--build", type=Path, default=iq.ROOT / "firmware/build-c3")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    if args.reference:
        for code in args.codes:
            print(json.dumps(dict(code_index=code, s2_code="normal " + RATES[code], frames=args.frames,
                                  ring_groups=args.ring_groups,
                                  **ring_reference(code, args.frames, args.ring_groups))))
        return
    if args.out is None or args.port is None:
        parser.error("device trials require --out and explicit --port; use --self-test or --reference offline")
    if not 100 <= args.ms <= 60000 or any(not 0 <= delay <= 10000 for delay in args.slow_us):
        parser.error("--ms must be 100..60000 and --slow-us must be 0..10000")
    if not 128 <= args.gap_us <= 4096:
        parser.error("--gap-us must be 128..4096")
    if any(mask & 1024 for mask in args.options):
        parser.error("BCH three-table bit 1024 needs c3pair layout4; c3fec 'best' uses mask 251")
    build = args.build.resolve()
    cache = (build / "CMakeCache.txt").read_text()
    if "IQ4HIST_EXPERIMENT:BOOL=ON" not in cache:
        parser.error("--build must contain IQ4HIST_EXPERIMENT=ON firmware")
    args.out.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in ("iqbench.uf2", "iqbench.elf", "CMakeCache.txt", "memmap_iq.ld", "git_rev.h"):
        path = build / name
        hashes["build/" + name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if path.suffix in (".uf2", ".elf"):
            shutil.copy2(path, args.out / name)
    sources = [path for folder in ("firmware/src", "host", "reference")
               for path in (iq.ROOT / folder).rglob("*")
               if path.suffix in (".c", ".h", ".S", ".py") and "build" not in path.parts]
    sources += [iq.ROOT / "firmware/CMakeLists.txt"]
    hashes.update({str(path.relative_to(iq.ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in sorted(sources)})
    git = lambda *a: subprocess.run(["git", *a], cwd=iq.ROOT, capture_output=True, text=True).stdout.strip()
    manifest = dict(started=dt.datetime.now().isoformat(), codes=args.codes, ms=args.ms,
                    slow_us=args.slow_us, cpw=args.cpw, options=args.options, gap_us=args.gap_us,
                    qualification=args.gap_us == 128,
                    expected_firmware_git=args.expected_git, port=args.port,
                    head=git("rev-parse", "HEAD"), branch=git("branch", "--show-current"),
                    status=git("status", "--short"), sha256=hashes,
                    scope="CPU-only timer-paced two-core 8PSK FEC; in-memory GCS source; no output/ingress claim")
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copy2(__file__, args.out / "driver.py")
    board = iq.Board(args.port)
    failed = False
    try:
        for code, delay, options in ((code, delay, options) for code in args.codes
                                     for delay in args.slow_us for options in args.options):
            label = f"c3fec-code{code}-cpw{args.cpw}-slow{delay}-opt{options}-gap{args.gap_us}"
            result, lines = board.cmd(f"c3fec {code} {args.ms} {delay} {args.cpw} {args.gap_us} {options}",
                                      timeout=args.ms / 1000 + 60)
            (args.out / (label + ".txt")).write_text("@" + json.dumps(result) + "\n" + "\n".join(lines) + "\n")
            result["faults"] = run_faults(result, code, args.ms, delay, args.expected_git, args.cpw,
                                          options, args.gap_us)
            result["qualification"] = args.gap_us == 128
            result["label"] = label
            with (args.out / "runs.jsonl").open("a") as file:
                file.write(json.dumps(result) + "\n")
            print(label, json.dumps({key: result.get(key) for key in
                  ("elapsed_us", "published_frames", "late_frames", "max_lateness_us", "core", "faults")}), flush=True)
            failed |= bool(result["faults"])
    finally:
        board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
