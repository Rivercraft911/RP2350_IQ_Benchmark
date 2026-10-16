"""B8 two-core complete 8PSK/GCS transmitter trial and independent pins reference.

The offline default and --self-test never access hardware. Device runs require
--port and --out; build and flash separately. The reference reconstructs the
actual PV-SPI pattern, GCS BBHEADER, scalar BCH/LDPC and scalar complex PLFRAME,
then applies the frozen L7/N4 FIR directly to geometric axis coordinates. It
does not consume firmware mapping tables, code rings or native encoder output.

  python3 host/b8_lean8_trial.py --self-test
  python3 host/b8_lean8_trial.py --port PORT --out results/b8/soak \
      --ms 60000 --cpws 2 --codes 8 --options 27 --prios 13 0 --inverts 0
"""
from __future__ import annotations

import argparse
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

import dual_spi
from dual_trial import parse_capture
import iqbench as iq
from pulse_profiles import FOUR_LEVELS_Q30, L7_COEFFICIENTS, L7_PROFILE
from test_dvbs2_8psk import MODCOD, scalar_frame

RATES = {5: "2/3", 6: "3/4", 8: "5/6"}
CLOCK_HZ = 128000000
FRAME_SYMBOLS = 22194
BUS_PER_SYMBOL = 8
CAP_WORDS = 4096
STRIDE = 17
SPAN = 7
TABLE8_CRC = 0x5E13245C
PREFILL_FRAMES = 2
STAGES = ("gcs", "bch", "ldpc", "map", "delay")
SOURCE_EMPTY = 1 << 6


@functools.lru_cache(maxsize=24)
def frame_points(index: int, code: int) -> np.ndarray:
    """Random access to a real GCS frame; the packet cursor continues across frames."""
    if type(index) is not int or index < 0 or code not in RATES:
        raise ValueError("unsupported frame index or code")
    rate = RATES[code]
    bits = dual_spi.bbframe(index, gcs=True, rate=rate)
    fec = dual_spi.ref.fecframe(bits, rate, False)
    points = scalar_frame(fec, rate, False, True, 0)
    assert len(points) == FRAME_SYMBOLS
    points.flags.writeable = False
    return points


def geometric_codes(points: np.ndarray) -> np.ndarray:
    levels = np.asarray(FOUR_LEVELS_Q30, np.float64) / 2**30
    return np.asarray([np.abs(axis[:, None] - levels).argmin(axis=1)
                       for axis in (points.real, points.imag)], np.uint32)


@functools.lru_cache(maxsize=12)
def ring_crcs(frames: int, code: int) -> list[int]:
    """Physical 4096-word rings from the retained suffix of the real continuous stream.

    Only complete 16-symbol groups are stored. Frames are 22194 == 2 (mod 16)
    symbols, so each boundary carries its two remaining symbols into the next
    frame. The last partial group is pending and never written to either ring.
    """
    if type(frames) is not int or frames < 0 or code not in RATES:
        raise ValueError("invalid ring frame count or code")
    groups = frames * FRAME_SYMBOLS // 16
    first_group = max(0, groups - 4096)
    first_frame = first_group * 16 // FRAME_SYMBOLS
    rings = np.zeros((2, 4096), dtype="<u4")
    if groups:
        points = np.concatenate([frame_points(index, code) for index in range(first_frame, frames)])
        begin = first_group * 16 - first_frame * FRAME_SYMBOLS
        codes = geometric_codes(points)[..., begin:begin + (groups - first_group) * 16]
        packed = (codes.reshape(2, -1, 16) << (2 * np.arange(16, dtype=np.uint32))).sum(axis=2, dtype=np.uint32)
        indices = np.arange(first_group, groups, dtype=np.uint64) & 4095
        rings[:, indices] = packed
    return [zlib.crc32(axis.tobytes()) for axis in rings]


@functools.lru_cache(maxsize=8)
def shaped_bus(first: int, last: int, code: int) -> np.ndarray:
    """Finite 8-bit output with six genuine preceding symbols, including startup."""
    if type(first) is not int or type(last) is not int or not 0 <= first < last:
        raise ValueError("invalid frame window")
    prehistory = (np.full(SPAN - 1, np.exp(1j * np.pi / 8)) if first == 0 else
                  frame_points(first - 1, code)[-(SPAN - 1):])
    points = np.concatenate([prehistory] + [frame_points(k, code) for k in range(first, last)])
    levels = np.asarray(FOUR_LEVELS_Q30, np.int64)
    coefficients = np.asarray(L7_COEFFICIENTS, np.int64).reshape(SPAN, 4)
    samples = []
    for axis in geometric_codes(points):
        histories = np.lib.stride_tricks.sliding_window_view(levels[axis], SPAN)[:, ::-1]
        sums = histories @ coefficients
        samples.append((((sums + (3399 << 31)) // (3399 << 32)) * 64).ravel())
    bus = np.empty(2 * len(samples[0]), dtype="<u2")
    bus[0::2] = (samples[0] & 0x3fff) | 0x4000
    bus[1::2] = samples[1] & 0x3fff
    bus.flags.writeable = False
    return bus


def reference_window(lo: int, hi: int, code: int) -> tuple[int, np.ndarray]:
    """Cover a bounded start interval and all 4096 stride-17 observations."""
    if type(lo) is not int or type(hi) is not int or not 0 <= lo <= hi or hi - lo > 65536:
        raise ValueError("invalid bounded capture window")
    end = hi + math.ceil(STRIDE * CAP_WORDS / BUS_PER_SYMBOL) + 1
    first, last = lo // FRAME_SYMBOLS, end // FRAME_SYMBOLS + 1
    return first * FRAME_SYMBOLS * BUS_PER_SYMBOL, shaped_bus(first, last, code)


def validate_capture(result: dict, lines: list[str], code: int, invert: int) -> dict:
    """Require one finite stream offset and check every snapshot and clock edge."""
    try:
        if result.get("cap_error"):
            raise ValueError(str(result["cap_error"]))
        words = parse_capture(lines)
        if result.get("cap_words") != CAP_WORDS or len(words) != CAP_WORDS:
            raise ValueError("incomplete 4096-word GPIO capture")
        if zlib.crc32(words.tobytes()) != result.get("cap_crc"):
            raise ValueError("capture CRC mismatch (serial transfer)")
        if result.get("cap_stride") != STRIDE or result.get("cap_format") != "pins32":
            raise ValueError("capture must be pins32 at stride 17")
        keys = ("cap_symbol_lo", "cap_symbol_hi", "cap_margin_symbols")
        if any(type(result.get(key)) is not int or result[key] < 0 for key in keys):
            raise ValueError("invalid capture cursor metadata")
        if result["cap_symbol_lo"] > result["cap_symbol_hi"] or result["cap_margin_symbols"] > 4096:
            raise ValueError("invalid capture cursor bounds")
        lo = max(0, result["cap_symbol_lo"] - result["cap_margin_symbols"])
        hi = result["cap_symbol_hi"] + result["cap_margin_symbols"]
        base, reference = reference_window(lo, hi, code)
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(exc))
    bus = (words & 0xffff).astype("<u2")
    clock_bad = int(np.count_nonzero(((words >> 28) & 1) != ((words >> 14) & 1)))
    clock_high = int(np.count_nonzero(words & (1 << 28)))
    clock = dict(cap_clock_checked=CAP_WORDS, cap_clock_mismatches=clock_bad,
                 cap_clock_high=clock_high,
                 cap_clock_ok=clock_bad == (CAP_WORDS if invert else 0) and 0 < clock_high < CAP_WORDS)
    steps = STRIDE * np.arange(CAP_WORDS)
    start = lo * BUS_PER_SYMBOL - base
    stop = min(hi * BUS_PER_SYMBOL - base, len(reference) - int(steps[-1]) - 1)
    candidates = np.arange(start, stop + 1)
    for index in range(32):
        candidates = candidates[reference[candidates + steps[index]] == bus[index]]
        if not len(candidates):
            break
    if len(candidates) != 1:
        return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_candidates=int(len(candidates)),
                    cap_search_bus_words=max(0, stop - start + 1),
                    cap_error="no unique finite offset matches the first 32 snapshots", **clock)
    offset = int(candidates[0])
    bad = np.flatnonzero(reference[offset + steps] != bus)
    return dict(cap_transport_ok=True, cap_aligned=True, cap_global_bus_offset=base + offset,
                cap_symbol=(base + offset) // BUS_PER_SYMBOL, cap_search_bus_words=stop - start + 1,
                cap_checked_bus_words=CAP_WORDS, cap_mismatches=int(len(bad)),
                cap_first_bad=int(bad[0]) if len(bad) else None, **clock)


def capture_lines(words: np.ndarray) -> tuple[dict, list[str]]:
    words = np.asarray(words, dtype="<u4")
    lines = ["@cap " + str(k) + " " + " ".join(f"{int(w):08x}" for w in words[k:k + 16])
             for k in range(0, len(words), 16)]
    return dict(cap_words=len(words), cap_crc=zlib.crc32(words.tobytes()), cap_stride=STRIDE,
                cap_format="pins32"), lines


def _uint(value):
    return type(value) is int and value >= 0


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def run_faults(result: dict, code: int, ms: int, cpw: int, invert: int, priority: int,
               options: int, starve: int, expected_git: str | None = None,
               max_busy: float = .70) -> list[str]:
    """Fail closed on metadata, ordered ownership, bounded service and pin samples."""
    if result.get("error"):
        return [f"firmware error: {result['error']}"]
    r, faults = result, []
    expected = dict(cmd="b8lean8", clk_hz=CLOCK_HZ, code_index=code,
                    s2_code="normal " + RATES[code], kbch=dual_spi.ref.code(RATES[code], False).kbch,
                    cpw=cpw, ms=ms, invert=invert, bus_priority_mask=priority, options=options,
                    starve=starve, pilots=True, gold=0, source=6, L=SPAN, sps=4,
                    row_profile=L7_PROFILE, row_symbols=1, row_words=1, sample_bits=8,
                    writers=2, frame_symbols=FRAME_SYMBOLS, table_crc=TABLE8_CRC,
                    prefill_frames=PREFILL_FRAMES, service_gap_limit_us=128,
                    restored=True, selectors_restored=True)
    if expected_git:
        expected["git"] = expected_git
    faults += [f"{key}={r.get(key)!r}, expected {value!r}" for key, value in expected.items()
               if r.get(key) != value]
    if not isinstance(r.get("git"), str) or not r["git"]:
        faults.append("firmware git identity missing")
    counters = ("elapsed_us", "published_frames", "published_groups", "published_words",
                "granted_words", "reusable_words", "max_service_gap_us", "errors",
                "pointer_errors", "output_errors", "txstalls_i", "txstalls_q",
                "pointer_txstall_mask", "null_trigger_mask")
    invalid = [key for key in counters if not _uint(r.get(key))]
    issued = r.get("input_issue_words")
    if not isinstance(issued, list) or len(issued) != 2 or any(not _uint(v) for v in issued):
        invalid.append("input_issue_words")
    if invalid:
        return list(dict.fromkeys(faults + ["invalid runtime counters: " + ", ".join(invalid)]))
    elapsed, frames = r["elapsed_us"], r["published_frames"]
    if frames < PREFILL_FRAMES or r["published_groups"] != frames * FRAME_SYMBOLS // 16:
        faults.append("ordered frame publication differs from complete continuous groups")
    if r.get("code_crc") != ring_crcs(frames, code):
        faults.append("axis code ring CRC differs from independent continuous GCS/FEC/PL reference")
    # Admitted FEC jobs drain after pin DMA stops. Those last ordered frames are
    # present in the code rings but were never offered to the stopped DMA graph.
    if not 0 <= r["published_groups"] - r["published_words"] <= 4096:
        faults.append("axis code words differ from bounded ordered frame publication")
    if not (0 <= r["reusable_words"] <= min(issued) <= max(issued)
            <= r["granted_words"] <= r["published_words"]):
        faults.append("axis issue/grant/publication/reuse ordering inconsistent")
    if not 0 < r["max_service_gap_us"] <= 128:
        faults.append("core 0 supervision service gap exceeded 128 us")
    for key in ("own_errors", "publish_order_errors"):
        if key in r and r[key] != 0:
            faults.append(key)
    if not starve:
        for key in ("errors", "pointer_errors", "output_errors", "txstalls_i", "txstalls_q",
                    "pointer_txstall_mask", "null_trigger_mask"):
            if r[key]:
                faults.append(key)
        if not ms * 1000 <= elapsed <= ms * 1000 + 50000:
            faults.append("elapsed duration differs from request")
        if frames <= PREFILL_FRAMES:
            faults.append("no runtime frames were published")
        if not _finite(r.get("min_credit_runway_words")) or r["min_credit_runway_words"] <= 0:
            faults.append("credit runway reached zero")
        expected_words = CLOCK_HZ / (8 * cpw) * elapsed / 16e6
        if not expected_words * .98 <= min(issued) <= max(issued) <= expected_words * 1.02 + 32:
            faults.append("issued code words differ from the requested continuous output rate")
    else:
        if (frames != PREFILL_FRAMES or not r["errors"] & SOURCE_EMPTY or
                not r["pointer_errors"] & SOURCE_EMPTY):
            faults.append("starvation control did not stop at startup frame publication")
        if r["pointer_errors"] & ~(SOURCE_EMPTY | 8) or r["output_errors"] & ~3 or r["errors"] & ~(SOURCE_EMPTY | 8 | 3):
            faults.append("starvation control reported an unrelated fault")
        if r.get("reason") != "SOURCE_EMPTY":
            faults.append("starvation control did not identify SOURCE_EMPTY")
        runway_us = PREFILL_FRAMES * FRAME_SYMBOLS * 8 * cpw / CLOCK_HZ * 1e6
        if not 0 < elapsed < min(ms * 1000, runway_us + 50000):
            faults.append("starvation control did not stop within the finite startup runway")
        if not r["null_trigger_mask"]:
            faults.append("starvation control did not exhaust a finite DMA grant")
    cores = r.get("core")
    if not isinstance(cores, list) or len(cores) != 2:
        return list(dict.fromkeys(faults + ["missing two-core accounting"]))
    runtime = max(0, frames - PREFILL_FRAMES)
    total_frames = 0
    for index, core in enumerate(cores):
        if not isinstance(core, dict) or any(not _uint(core.get(key)) for key in
                                            ("frames", "busy_cycles", "max_slice_cycles")):
            faults.append(f"invalid core {index} accounting")
            continue
        total_frames += core["frames"]
        if core["frames"] != (runtime + 1 - index) // 2:
            faults.append(f"core {index} did not own its even/odd runtime frames")
        stage = core.get("stage_cycles")
        if (not isinstance(stage, dict) or set(stage) != set(STAGES) or
                any(not _uint(stage[key]) for key in STAGES)):
            faults.append(f"invalid core {index} stage cycle accounting")
        elif sum(stage.values()) > core["busy_cycles"]:
            faults.append(f"core {index} stage totals exceed charged busy cycles")
        elif stage["delay"]:
            faults.append(f"core {index} reported an unrequested delay stage")
        if core["max_slice_cycles"] > CLOCK_HZ * 128 // 1000000:
            faults.append(f"core {index} work slice exceeded 128 us")
        if core["frames"] and not core["max_slice_cycles"]:
            faults.append(f"core {index} maximum work slice missing")
        busy = core.get("busy")
        charged = core["busy_cycles"] / (elapsed * CLOCK_HZ / 1e6) if elapsed else math.inf
        if (not _finite(busy) or not 0 <= busy <= 1.01 or
                not math.isclose(charged, busy, abs_tol=.00015)):
            faults.append(f"core {index} utilization differs from charged cycles")
        elif not starve and busy > max_busy:
            faults.append(f"core {index} utilization exceeds {max_busy:.1%} target")
    if total_frames != runtime:
        faults.append("runtime core frame ownership differs from ordered publication")
    spi = r.get("pv_spi")
    if not isinstance(spi, dict):
        faults.append("missing PV-SPI accounting")
    else:
        required = ("bbframes", "runtime_bbframes", "runtime_messages", "gcs_packets", "gcs_bytes", "ok")
        if any(not _uint(spi.get(key)) for key in required):
            faults.append("invalid PV-SPI runtime counters")
        if spi.get("bbframes") != frames or spi.get("runtime_bbframes") != runtime:
            faults.append("PV-SPI BBFRAME count differs from ordered encoded frames")
        if spi.get("sck_hz") != 32000000:
            faults.append("on-chip PV-SPI clock differs from SYS128 profile")
        try:
            if starve:
                faults.extend(starvation_packet_faults(r))
            else:
                faults.extend(dual_spi.packet_faults(r))
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            faults.append("invalid packet accounting: " + type(exc).__name__)
    capture_required = not starve or r.get("cap_words") != 0
    if capture_required and not (r.get("cap_transport_ok") and r.get("cap_aligned") and
                                  r.get("cap_mismatches") == 0 and r.get("cap_checked_bus_words") == CAP_WORDS):
        faults.append(r.get("cap_error") or "capture differs from independent GCS/FEC/PL/FIR reference")
    if capture_required and not r.get("cap_clock_ok"):
        faults.append("DDR clock parity differs from " + ("inverted control" if invert else "I-slot high"))
    return list(dict.fromkeys(faults))


def starvation_packet_faults(result: dict) -> list[str]:
    """The startup-only control has exactly two BBFRAMEs and no runtime ingress."""
    stats, faults = result["pv_spi"], []
    for name in ("lost", "bad_hdr", "bad_crc", "bad_sync", "null_packets", "short_msgs",
                 "long_msgs", "overflows", "gcs_underflows", "drain_timeouts", "late_closes"):
        if stats.get(name) != 0:
            faults.append("pv_spi." + name)
    count = PREFILL_FRAMES * (result["kbch"] - 80) // 8
    if (stats.get("bb_mode") != "gcs" or stats.get("gcs_bytes") != count or
            stats.get("gcs_packets") != (count + 187) // 188 or stats.get("ts_packets") != 0):
        faults.append("startup GCS accounting differs from exactly two frames")
    expected = dual_spi.first_crcs(gcs=True, rate=RATES[result["code_index"]])
    if stats.get("bb_crc") != expected[:PREFILL_FRAMES] + [0, 0]:
        faults.append("startup GCS BBFRAME CRC differs from independent reference")
    accepted = stats.get("ok")
    if not _uint(accepted) or not accepted:
        faults.append("startup PV-SPI accepted-message count invalid")
    else:
        crc_fields = b"".join(dual_spi.pv.message(k, dual_spi.pv.pattern_payload(k))[-4:] for k in range(16))
        chain = crc_fields * (accepted // 16) + crc_fields[:4 * (accepted % 16)]
        if stats.get("crc_chain") != zlib.crc32(chain):
            faults.append("startup accepted SPI CRC chain differs from pattern source")
    return faults


def validate_run(result: dict, lines: list[str], code: int, ms: int, cpw: int,
                 invert: int, priority: int, options: int, starve: int,
                 expected_git: str | None = None, max_busy: float = .70) -> dict:
    checked = dict(result)
    if not checked.get("error"):
        if starve and checked.get("cap_words") == 0:
            checked.update(cap_checked_bus_words=0, cap_clock_checked=0,
                           capture_skipped="starvation_before_capture")
        else:
            checked.update(validate_capture(checked, lines, code, invert))
    checked["faults"] = run_faults(checked, code, ms, cpw, invert, priority, options,
                                   starve, expected_git, max_busy)
    checked["classification"] = ("failed" if checked["faults"] else
                                 "source_starvation_detected" if starve else
                                 "clock_inversion_detected" if invert else
                                 "digital_complete_8psk_clean")
    return checked


def self_test():
    """Offline real-FEC capture checks; no Board instance or native encoder is used."""
    for code in RATES:
        symbol = FRAME_SYMBOLS - 300
        lo, hi = symbol - 40, symbol + 40
        base, reference = reference_window(lo - 256, hi + 256, code)
        offset = symbol * BUS_PER_SYMBOL + 3 - base
        bus = reference[offset + STRIDE * np.arange(CAP_WORDS)].astype("<u4")
        for invert in (0, 1):
            values = bus | ((((bus >> 14) & 1) ^ invert) << 28)
            meta, lines = capture_lines(values)
            r = dict(meta, cap_symbol_lo=lo, cap_symbol_hi=hi, cap_margin_symbols=256)
            checked = validate_capture(r, lines, code, invert)
            assert checked["cap_aligned"] and checked["cap_mismatches"] == 0 and checked["cap_clock_ok"], checked
            assert checked["cap_global_bus_offset"] == symbol * BUS_PER_SYMBOL + 3
            bad = values.copy(); bad[-1] ^= 64
            meta, bad_lines = capture_lines(bad)
            checked = validate_capture(dict(r, **meta), bad_lines, code, invert)
            assert checked["cap_mismatches"] == 1, checked
            assert not validate_capture(r, lines, code, 1 - invert)["cap_clock_ok"]
    print("b8_lean8_trial self-test: all three rates, independent GCS/scalar FEC/PL/8-bit FIR, "
          "frame seam, clock inversion and late capture corruption checked; no hardware accessed")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--port", help="explicit serial port; required only for a device run")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--build", type=Path, default=iq.ROOT / "firmware/build-exp")
    parser.add_argument("--expected-git")
    parser.add_argument("--codes", type=int, nargs="+", choices=tuple(RATES), default=[8])
    parser.add_argument("--cpws", type=int, nargs="+", choices=(1, 2, 4), default=[2])
    parser.add_argument("--ms", type=int, default=3000)
    parser.add_argument("--inverts", type=int, nargs="+", choices=(0, 1), default=[0])
    parser.add_argument("--prios", type=int, nargs="+", choices=range(16), default=[13, 0])
    parser.add_argument("--options", type=int, nargs="+", choices=(0, 27), default=[27])
    parser.add_argument("--starves", type=int, nargs="+", choices=(0, 1), default=[0])
    parser.add_argument("--max-busy", type=float, default=.70,
                        help="qualification target per core; use 1 for cadence-only A/B or 16-Msym/s probes")
    args = parser.parse_args()
    if args.self_test or (args.port is None and args.out is None):
        self_test()
        return
    if not args.port or args.out is None or not 100 <= args.ms <= 60000:
        parser.error("device runs require --port, a fresh --out and --ms 100..60000")
    if not math.isfinite(args.max_busy) or not 0 < args.max_busy <= 1:
        parser.error("--max-busy must be finite and in (0, 1]")
    build = args.build.resolve()
    if "IQ4HIST_EXPERIMENT:BOOL=ON" not in (build / "CMakeCache.txt").read_text():
        parser.error("--build must be IQ4HIST_EXPERIMENT=ON")
    args.out.mkdir(parents=True, exist_ok=False)
    hashes = {}
    for name in ("iqbench.uf2", "iqbench.elf"):
        shutil.copy2(build / name, args.out / name)
    for name in ("iqbench.uf2", "iqbench.elf", "CMakeCache.txt", "memmap_iq.ld", "git_rev.h"):
        hashes["build/" + name] = hashlib.sha256((build / name).read_bytes()).hexdigest()
    sources = [f for directory in ("firmware/src", "host", "reference")
               for f in (iq.ROOT / directory).rglob("*") if f.suffix in (".c", ".h", ".S", ".py")]
    sources.append(iq.ROOT / "firmware/CMakeLists.txt")
    hashes.update({str(f.relative_to(iq.ROOT)): hashlib.sha256(f.read_bytes()).hexdigest()
                   for f in sorted(sources)})
    def git(*arguments):
        return subprocess.run(["git", *arguments], cwd=iq.ROOT, capture_output=True, text=True).stdout.strip()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=args.ms, codes=args.codes,
                    cpws=args.cpws, inverts=args.inverts, prios=args.prios, options=args.options,
                    starves=args.starves, max_busy=args.max_busy, expected_firmware_git=args.expected_git,
                    head=git("rev-parse", "HEAD"), branch=git("branch", "--show-current"),
                    status=git("status", "--short"), sha256=hashes,
                    setup="USB only, external GPIOs unconnected, stock voltage, on-chip PV-SPI master",
                    scope="complete GCS/two-core FEC/8PSK/L7-N4 transmitter, sparse pins32 capture; digital only")
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    shutil.copy2(__file__, args.out / "driver.py")
    iq.LOGGING = False
    board, failed = iq.Board(args.port), False
    try:
        for command in ("info", "clock 128000"):
            result, _ = board.cmd(command, timeout=60)
            with (args.out / "runs.jsonl").open("a") as stream:
                stream.write(json.dumps(dict(label=command, **result)) + "\n")
            if result.get("error"):
                raise SystemExit(f"{command}: {result['error']}")
        for code in args.codes:
            for cpw in args.cpws:
                for priority in args.prios:
                    for options in args.options:
                        for invert in args.inverts:
                            for starve in args.starves:
                                command = f"b8lean8 {code} {args.ms} {cpw} {invert} {priority} {options} {starve}"
                                label = f"b8-code{code}-cpw{cpw}-prio{priority}-opt{options}-invert{invert}-starve{starve}"
                                result, lines = board.cmd(command, timeout=args.ms / 1000 + 60)
                                (args.out / (label + ".txt")).write_text("\n".join(lines) + "\n")
                                checked = validate_run(result, lines, code, args.ms, cpw, invert,
                                                       priority, options, starve, args.expected_git,
                                                       args.max_busy)
                                checked["label"] = label
                                with (args.out / "runs.jsonl").open("a") as stream:
                                    stream.write(json.dumps(checked) + "\n")
                                print(label, json.dumps({key: checked.get(key) for key in
                                      ("elapsed_us", "core", "max_service_gap_us", "min_credit_runway_words",
                                       "cap_mismatches", "cap_clock_mismatches", "reason", "classification", "faults")}),
                                      flush=True)
                                failed |= bool(checked["faults"])
    finally:
        board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

