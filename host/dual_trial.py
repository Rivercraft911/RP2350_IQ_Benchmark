"""Dual-PIO descriptor experiment runner and independent strided-capture validation.

USB-only Pico, all external GPIO wiring disconnected. Build/flash separately: this script
does neither. Every capture is checked at one fixed cyclic offset; no error samples are
discarded. Failed speed probes and deliberate negative controls remain in immutable logs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import functools
import hashlib
import json
import subprocess
import zlib
from pathlib import Path

import numpy as np

import iqbench as iq

NORMALIZED_BASE_I = 0x20040000
NORMALIZED_BASE_Q = 0x20048000
DESCRIPTOR_CRC_I = 491185096
DESCRIPTOR_CRC_Q = 845440890
CAPTURE_STRIDE = 17
SEED = 0x1234ABCD


def pulse_parameters(row_symbols: int) -> tuple[int, float]:
    if row_symbols not in (1, 2, 4):
        raise ValueError(f"unknown row format {row_symbols}")
    return (9, 0.8) if row_symbols == 4 else (12, iq.gen_coeffs.KAISER_BETA)


@functools.lru_cache(maxsize=16)
def reference_bus(source: int, seed: int = SEED, row_symbols: int = 1, bb_stride: int = 0,
                  code_index: int = 5) -> np.ndarray:
    """One exact steady-state period, in physical 16-bit I,Q bus order."""
    span, beta = pulse_parameters(row_symbols)
    lut = iq.m.Lut(iq.gen_coeffs.ALPHA, 4, span, beta, iq.gen_coeffs.HEADROOM_DB)
    if source in (0, 1):
        count = 512 * row_symbols if source == 0 else 8192
        inputs = iq.m.xorshift32(seed, count)
        words = iq.m.generate(np.tile(inputs, 2), lut)[count * 16 * 4:]
    elif source == 4:
        import sys
        sys.path.insert(0, str(iq.ROOT / "reference/dvbs2"))
        import dvbs2 as ref
        rate = {5: "2/3", 6: "3/4", 8: "5/6"}[code_index]
        kbch = ref.code(rate, False).kbch
        w = iq.m.xorshift32(0x9E3779B9 ^ code_index, (kbch + 31) // 32)
        bits = ((w[:, None] >> np.arange(31, -1, -1, dtype=np.uint32)) & 1).astype(np.uint8).ravel()[:kbch]
        phases = []
        for flip in (0, 1):
            b = bits.copy()
            b[::32 * bb_stride if bb_stride else len(bits)] ^= flip
            phases.append(ref.plframe(ref.fecframe(b, rate, False), rate, False, pilots=True))
        bi = np.concatenate([phase[0] for phase in phases])
        bq = np.concatenate([phase[1] for phase in phases])
        packed = iq.pack_symbols(np.tile(bi, 3), np.tile(bq, 3))
        samples = iq.m.generate(packed, lut)
        period = len(bi) * 4
        words = samples[period:2 * period]
    elif source in (2, 3):
        rate = {5: "2/3", 6: "3/4", 8: "5/6"}[code_index]
        bi, bq = iq.dvbs2_ref_symbols(f"normal {rate}", 1, 0x9E3779B9 ^ code_index)
        packed = iq.pack_symbols(np.tile(bi, 3), np.tile(bq, 3))
        all_samples = iq.m.generate(packed, lut)
        # The middle frame has correct preceding history and no frame-packing tail pad.
        period = len(bi) * 4
        words = all_samples[period:2 * period]
    else:
        raise ValueError(f"unknown source {source}")
    return np.asarray(words, dtype="<u4").view("<u2")


def parse_capture(lines: list[str]) -> np.ndarray:
    """Enforce ordered, complete @cap indices before interpreting or CRC-checking values."""
    values = []
    for line in lines:
        fields = line.split()
        if len(fields) < 3 or fields[0] != "@cap" or int(fields[1]) != len(values):
            raise ValueError("capture line missing, duplicated or out of order")
        for word in fields[2:]:
            value = int(word, 16)
            if not 0 <= value <= 0xffffffff:
                raise ValueError("capture word outside uint32 range")
            values.append(value)
    return np.asarray(values, dtype="<u4")


def validate_capture(r: dict, lines: list[str], source: int,
                     requested_words: int = 4096) -> dict:
    """Check serial integrity, then all samples against a fixed-stride reference period."""
    try:
        got = parse_capture(lines)
        if r.get("cap_error"):
            raise ValueError(str(r["cap_error"]))
        if len(got) != requested_words or r.get("cap_words") != requested_words:
            raise ValueError(f"received {len(got)} words, firmware reports {r.get('cap_words')}; "
                             f"expected {requested_words}")
        if zlib.crc32(got.tobytes()) != r.get("cap_crc"):
            raise ValueError("capture CRC mismatch (serial transfer)")
        stride = r.get("cap_stride", r.get("cap_stride_words"))
        if stride != CAPTURE_STRIDE:
            raise ValueError(f"capture stride {stride}, expected {CAPTURE_STRIDE}")
        if not len(got):
            raise ValueError("empty capture")
    except (ValueError, TypeError) as e:
        return dict(cap_aligned=False, cap_error=str(e), cap_mismatches=None,
                    cap_checked_bus_words=0, cap_transport_ok=False, cap_reference_failed=False)

    bus = got.view("<u2")
    if source in (5, 6):
        from dual_spi import reference_window
        span, beta = pulse_parameters(int(r.get("row_symbols", 2)))
        first_frame, ref = reference_window(int(r["cap_symbol_cursor"]), span=span, beta=beta,
                                            gcs=source == 6,
                                            rate=r.get("s2_code", "normal 2/3").split()[-1])
    else:
        first_frame = None
        ref = reference_bus(source, int(r.get("seed", SEED)), int(r.get("row_symbols", 1)),
                            int(r.get("split_bb_stride_words", 0)), int(r.get("code_index", 5)))
    # Uniform clocks use17 logical bus slots per IN. With output divider1.5,
    # capture runs at SYSCLK to avoid the feedback path aliasing one I/Q axis. Every
    # candidate uses one fixed physical-time phase for the ENTIRE snapshot.
    divider = int(r.get("divider256", 256))
    cap_divider = int(r.get("cap_divider256", divider))
    if divider == 384 and cap_divider != 256:
        return dict(cap_aligned=False, cap_transport_ok=True, cap_reference_failed=True,
                    cap_mismatches=None, cap_checked_bus_words=0,
                    cap_error="fractional capture requires independent SYSCLK sampler")
    phases = range(3) if divider == 384 else range(1)
    best_offset, best_bad, best_steps, best_phase = None, None, None, None
    for phase in phases:
        steps = ((np.arange(len(bus), dtype=np.int64) * 34 + phase) // 3
                 if divider == 384 else np.arange(len(bus), dtype=np.int64) * CAPTURE_STRIDE)
        candidates = np.flatnonzero(ref == bus[0])
        if source in (5, 6):
            candidates = candidates[(candidates >= 12 * 8) & (candidates + steps[-1] < len(ref))]
        for j in range(1, min(16, len(bus))):
            candidates = candidates[ref[(candidates + steps[j]) % len(ref)] == bus[j]]
            if not len(candidates):
                break
        for offset in candidates:
            bad = np.flatnonzero(bus != ref[(int(offset) + steps) % len(ref)])
            if best_bad is None or len(bad) < len(best_bad):
                best_offset, best_bad = int(offset), bad
                best_steps, best_phase = steps, phase
            if not len(bad):
                break
        if best_bad is not None and not len(best_bad):
            break
    if best_bad is None:
        return dict(cap_aligned=False, cap_mismatches=None, cap_checked_bus_words=min(16, len(bus)),
                    cap_transport_ok=True, cap_reference_failed=True,
                    cap_error="no fixed offset and physical-time phase matches the first16 bus observations")
    first = int(best_bad[0]) if len(best_bad) else None
    axes = np.bincount((bus >> 14) & 1, minlength=2)
    both_axes = bool(axes.min() >= len(bus) // 4)
    return dict(cap_aligned=both_axes, cap_offset=best_offset, cap_phase=best_phase,
                cap_reference_first_frame=first_frame,
                cap_axis_counts=axes.tolist(), cap_mismatches=int(len(best_bad)),
                cap_transport_ok=True, cap_reference_failed=bool(len(best_bad)) or not both_axes,
                cap_first_bad=first, cap_checked_bus_words=len(bus),
                cap_got_first_bad=int(bus[first]) if first is not None else None,
                cap_want_first_bad=int(ref[(best_offset + best_steps[first]) % len(ref)])
                if first is not None else None)


def stream_faults(r: dict, capture: bool, source: int) -> list[str]:
    faults = []
    if r.get("error"):
        faults.append(f"firmware error: {r['error']}")
    firmware_faults = r.get("faults", [])
    if firmware_faults:
        faults.extend(map(str, firmware_faults if isinstance(firmware_faults, list)
                          else [firmware_faults]))
    for name in ("errors", "underruns", "own_errors", "txstalls_i", "txstalls_q",
                 "txstall_i", "txstall_q", "txstalls", "input_overruns"):
        if r.get(name):
            faults.append(name)
    if not r.get("tables_ok", 1):
        faults.append("tables")
    if r.get("row_symbols") == 4 and (r.get("L") != 9 or r.get("filter_beta") != 0.8 or
                                       r.get("row_profile") != "l9_quad"):
        faults.append("four-symbol rows require the L9 beta0.8 pulse profile")
    # axis_gap_max is a queue-lead diagnostic; a nonzero bounded lead is legitimate.
    if capture and not (r.get("cap_aligned") and r.get("cap_mismatches") == 0):
        faults.append(r.get("cap_error") or "strided capture mismatches")
    if source in (2, 3, 4, 5, 6) and not r.get("s2_runtime_frames"):
        faults.append("no complete DVB-S2 encoder frame during timed run")
    if source in (4, 5, 6) and not r.get("split_bch_frames"):
        faults.append("no runtime frontend BCH frame")
    if source in (5, 6):
        from dual_spi import packet_faults
        faults.extend(packet_faults(r))
    return list(dict.fromkeys(faults))


def validate_bench(r: dict) -> dict:
    faults = []
    if r.get("error"):
        faults.append(f"firmware error: {r['error']}")
    if r.get("in_words") != 8192 or r.get("seed") != SEED:
        faults.append("descriptor benchmark input differs from independent reference")
    if (r.get("normalized_base_i") != NORMALIZED_BASE_I or
            r.get("normalized_base_q") != NORMALIZED_BASE_Q):
        faults.append("descriptor CRC normalization bases missing or different")
    kernels = r.get("kernels", [])
    if len(kernels) != 3 or {str(k.get("name", "")).lower() for k in kernels} != {"c", "asm", "interp"}:
        faults.append("expected C, assembly and interpolator benchmark records")
    for kernel in kernels:
        good = kernel.get("crc_i") == DESCRIPTOR_CRC_I and kernel.get("crc_q") == DESCRIPTOR_CRC_Q
        kernel["host_crc_ok"] = good
        if not good or ("crc_ok" in kernel and not kernel["crc_ok"]):
            faults.append(f"{kernel.get('name', '?')} descriptor CRC differs from Python")
    return dict(host_crc_ok=not faults, faults=faults)


def negative_control_detected(fault: int, r: dict) -> bool:
    """Require the injected cause; configuration/parser/serial failures do not qualify."""
    if r.get("error") or not r.get("gen_syms", 0):
        return False
    if fault == 1:
        # A valid, owned pointer to the wrong Q row must reach output without hardware
        # errors, while a complete and serially intact capture fails the waveform model.
        return (r.get("errors") == 0 and r.get("cap_words") == 4096 and
                bool(r.get("cap_transport_ok")) and bool(r.get("cap_reference_failed")))
    if fault == 2:
        ownership_bits = (1 << 16) | (1 << 17)  # firmware ERR_UNDERRUN / ERR_OWNERSHIP
        return bool(r.get("underruns") or r.get("own_errors") or
                    (int(r.get("errors", 0)) & ownership_bits))
    return False


def self_test():
    """No serial access: exercise genuine reference periods and negative capture controls."""
    for source, bb_stride, rows in ((0, 0, 1), (1, 0, 1), (2, 0, 1), (4, 0, 1),
                                    (4, 64, 1), (0, 0, 4), (1, 0, 4), (4, 64, 4)):
        ref = reference_bus(source, row_symbols=rows, bb_stride=bb_stride)
        samples = np.asarray(ref[(len(ref) - 97 + np.arange(8192) * CAPTURE_STRIDE) % len(ref)],
                             dtype="<u2")
        def make_capture(bus):
            words = np.asarray(bus, dtype="<u2").view("<u4")
            lines = [f"@cap {i} " + " ".join(f"{int(w):08x}" for w in words[i:i + 16])
                     for i in range(0, len(words), 16)]
            return dict(cap_words=len(words), cap_crc=zlib.crc32(words.tobytes()), row_symbols=rows,
                        cap_stride=CAPTURE_STRIDE, seed=SEED, split_bb_stride_words=bb_stride), lines
        r, lines = make_capture(samples)
        check = validate_capture(r, lines, source)
        assert check["cap_aligned"] and check["cap_mismatches"] == 0, check
        for phase in range(3):
            steps = (np.arange(8192) * 34 + phase) // 3
            fractional = ref[(len(ref) - 97 + steps) % len(ref)]
            fr, fl = make_capture(fractional)
            fr.update(divider256=384, cap_divider256=256)
            fc = validate_capture(fr, fl, source)
            assert fc["cap_aligned"] and fc["cap_mismatches"] == 0, fc
        bad = samples.copy(); bad[1000] ^= 1
        br, bl = make_capture(bad)
        check = validate_capture(br, bl, source)
        assert not check["cap_aligned"] or check["cap_mismatches"], check
        assert negative_control_detected(1, dict(br, **check, errors=0, gen_syms=1024))
        r["cap_crc"] ^= 1
        assert "serial transfer" in validate_capture(r, lines, source)["cap_error"]
        assert not validate_capture(br, bl[1:], source)["cap_aligned"]
    assert stream_faults(dict(axis_gap_max=3), False, 1) == []
    assert stream_faults(dict(errors=1), False, 1)
    assert stream_faults({}, False, 2)
    quad_profile = dict(row_symbols=4, L=9, filter_beta=0.8, row_profile="l9_quad")
    assert stream_faults(quad_profile, False, 1) == []
    for field, value in (("L", 12), ("filter_beta", 1.0), ("row_profile", "l12_pair")):
        assert stream_faults(dict(quad_profile, **{field: value}), False, 1)
    for injected in (1, 2):
        assert not negative_control_detected(injected, dict(error="configuration rejected", errors=1))
        assert not negative_control_detected(injected, dict(gen_syms=1024, errors=1,
                                                            cap_error="capture missing"))
    assert not negative_control_detected(1, dict(errors=0, gen_syms=1024, cap_words=4096,
                                                cap_transport_ok=False, cap_reference_failed=True))
    assert negative_control_detected(2, dict(errors=1 << 16, gen_syms=1024))
    assert negative_control_detected(2, dict(errors=0, own_errors=1, gen_syms=1024))
    assert not negative_control_detected(2, dict(errors=1 << 17, gen_syms=0))
    print("dual_trial self-test: L12/L9 waveform periods, integer/fractional capture, corruption, serial CRC, "
          "capture ordering and fault classification checked; no hardware accessed")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("profile", nargs="?", choices=("quick", "soak", "priority", "onecore", "split", "spi", "qualify", "quad", "gcs"), default="quick")
    p.add_argument("--out", type=Path)
    p.add_argument("--build", type=Path, default=Path("firmware/build-limits"))
    p.add_argument("--port")
    p.add_argument("--ms", type=int, help="duration per dual stream; quick=3000, soak=60000")
    p.add_argument("--clocks", nargs="+", type=int, choices=(128000, 150000))
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--writers", type=int, choices=(2, 4), default=2)
    p.add_argument("--priority-mask", type=int, choices=range(16), default=0)
    p.add_argument("--row-symbols", type=int, choices=(1, 2, 4), default=1)
    p.add_argument("--code-index", type=int, choices=(5, 6, 8), default=5)
    p.add_argument("--map-kernel", type=int, choices=(0, 1), default=0)
    a = p.parse_args()
    if a.clocks is None:
        a.clocks = [128000] if a.profile in ("spi", "quad", "gcs") else [128000, 150000]
    if a.profile in ("quad", "gcs"):
        a.row_symbols = 4
    if a.profile == "spi":
        a.writers, a.row_symbols = 4, 2
        if a.clocks != [128000]:
            p.error("the SPI profile is qualified at --clocks 128000 only")
    if a.writers == 4 and a.row_symbols not in (2, 4):
        p.error("four writers require --row-symbols 2 or 4")
    if a.self_test:
        self_test()
        return
    if a.out is None:
        p.error("--out is required for a device run")
    duration = a.ms if a.ms is not None else 60000 if a.profile in ("soak", "qualify") else 3000
    if not 1000 <= duration <= 60000:
        p.error("--ms must be 1000..60000")
    a.out.mkdir(parents=True, exist_ok=True)
    if (a.out / "runs.jsonl").exists() or (a.out / "manifest.json").exists():
        p.error("choose a new --out directory so previous results stay immutable")
    iq.LOGGING = False

    def git(*args):
        return subprocess.run(["git", *args], cwd=iq.ROOT, capture_output=True,
                              text=True, check=True).stdout.strip()
    sources = [f for folder in ("firmware/src", "host", "reference")
               for f in (iq.ROOT / folder).rglob("*")
               if f.suffix in (".c", ".h", ".S", ".py") and "build" not in f.parts]
    sources.append(iq.ROOT / "firmware/CMakeLists.txt")
    hashes = {str(f.relative_to(iq.ROOT)): hashlib.sha256(f.read_bytes()).hexdigest()
              for f in sorted(sources)}
    for name in ("iqbench.uf2", "iqbench.elf"):
        hashes[str(a.build / name)] = hashlib.sha256((a.build / name).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), profile=a.profile, duration_ms=duration,
                    code_index=a.code_index, map_kernel=a.map_kernel,
                    clocks_khz=a.clocks, row_symbols=a.row_symbols, writers=a.writers, priority_mask=a.priority_mask, head=git("rev-parse", "HEAD"),
                    branch=git("branch", "--show-current"), status=git("status", "--short"),
                    sha256=hashes,
                    setup="USB only, all external GPIO connections removed; stock voltage; digital only",
                    provenance="Local source/reference/build snapshot, not proof of source-to-flash identity; "
                               "flash this UF2 explicitly before running.",
                    capture="One fixed cyclic offset and physical-time phase, all captured bus observations checked; "
                            "finite sparse snapshots, not every outgoing sample")
    (a.out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    board = iq.Board(a.port)
    failures = []

    def record(label, fn, expectation="clean"):
        try:
            r = fn()
        except Exception as e:
            r = dict(exception=type(e).__name__, detail=str(e), faults=["host exception"])
            with (a.out / "runs.jsonl").open("a") as f:
                f.write(json.dumps(dict(label=label, expectation=expectation,
                                        recorded_at=dt.datetime.now().isoformat(), **r)) + "\n")
            raise  # a broken serial/parser session is not a successful injected-fault test
        faults = list(r.get("faults", []))
        if "error" in r:
            faults.append("firmware error")
        if r.get("crc_ok") is False:
            faults.append("CRC mismatch")
        if expectation == "negative":
            if not r.get("negative_control_detected"):
                failures.append((label, ["requested injected cause was not detected"]))
        elif faults and expectation != "ceiling":
            failures.append((label, faults))
        r["classification"] = "detected" if expectation == "negative" and r.get("negative_control_detected") else \
            "failed" if faults or expectation == "negative" else "clean"
        with (a.out / "runs.jsonl").open("a") as f:
            f.write(json.dumps(dict(label=label, expectation=expectation,
                                    recorded_at=dt.datetime.now().isoformat(), **r)) + "\n")
        print(label, json.dumps({k: r[k] for k in (
            "sym_rate", "gen_cyc_per_sym", "gen_busy", "control_busy", "core0_work_busy",
            "underruns", "own_errors", "cap_aligned", "cap_mismatches", "faults",
            "s2_runtime_frames", "host_crc_ok", "negative_control_detected") if k in r}), flush=True)
        return r

    def raw(command):
        def run():
            r, _ = board.cmd(command, timeout=120)
            if "error" in r:
                raise RuntimeError(r["error"])
            return r
        return record(command, run)

    def descriptor_bench(clock):
        def run():
            r, _ = board.cmd("dualbench 4", timeout=120)
            r.update(validate_bench(r))
            return r
        return record(f"descriptor-bench-{clock}", run)

    def dual(clock, cpw, kernel, source, fault=0, schedule=0, divider256=0, trial_ms=None, cap_words=4096):
        exploratory = (cpw == 1 or divider256 != 0) and a.profile not in ("qualify", "spi")
        suffix = "-negative-control" if fault else "-ceiling-probe" if exploratory else ""
        variant = f"-sched{schedule}-div{divider256}" if schedule or divider256 else ""
        variant += f"-cap{cap_words}" if cap_words != 4096 else ""
        variant += f"-prio{a.priority_mask}" if a.priority_mask else ""
        variant += f"-writers{a.writers}" if a.writers != 2 else ""
        variant += f"-code{a.code_index}" if a.code_index != 5 else ""
        label = f"dual-row{a.row_symbols}-{clock}-src{source}-k{kernel}-cpw{cpw}-fault{fault}{variant}{suffix}"
        def run():
            ms = 1000 if fault else trial_ms if trial_ms is not None else duration
            r, lines = board.cmd(f"dualstream {cpw} {ms} {kernel} {source} {cap_words} {fault} "
                                 f"{schedule} {divider256} {a.row_symbols} {a.code_index}",
                                 timeout=ms / 1000 + 60)
            if "error" not in r and cap_words:
                r.update(validate_capture(r, lines, source, cap_words))
            r["faults"] = stream_faults(r, bool(cap_words), source)
            if not fault and r.get("elapsed_us", 0) < ms * 1000:
                r["faults"].append("run ended before the requested duration")
            expected_config = dict(row_symbols=a.row_symbols, schedule=schedule,
                                   L=pulse_parameters(a.row_symbols)[0],
                                   divider256=divider256 or cpw * 256,
                                   clk_hz=clock * 1000, source=source,
                                   bus_priority_mask=a.priority_mask, writers=a.writers)
            for key, expected in expected_config.items():
                if r.get(key) != expected:
                    r["faults"].append(f"reported {key}={r.get(key)}; requested {expected}")
            if r.get("code_index", 5) != a.code_index:
                r["faults"].append("reported code differs from requested code")
            if r.get("map_kernel", 0) != a.map_kernel:
                r["faults"].append("reported mapping kernel differs from requested kernel")
            if fault:
                r["negative_control_fault"] = fault
                r["negative_control_detected"] = negative_control_detected(fault, r)
            effective_divider = divider256 / 256 if divider256 else cpw
            r["requested_schedule"] = schedule
            r["requested_divider256"] = divider256
            r["afe_sample_rate_within_65msps"] = clock * 1000 / (2 * effective_divider) <= 65e6
            if lines:
                capture_dir = a.out / "captures"
                capture_dir.mkdir(exist_ok=True)
                capture_path = capture_dir / f"{label}.txt"
                capture_path.write_text("\n".join(lines) + "\n")
                r["capture_file"] = str(capture_path.relative_to(a.out))
            return r
        return record(label, run, "negative" if fault else "ceiling" if exploratory else "clean")

    def baseline(clock, after=False):
        tag = f"baseline-{'after-' if after else ''}{clock}"
        record(f"{tag}-kernel", lambda: iq.run_bench(board, "lut_asm_p", 4, 12, 4, tag))
        if not after:
            record(f"{tag}-stream", lambda: iq.run_stream(board, "lut_asm_p", 4, 12,
                                                          "0", 2, 3000, 4096, tag))
        record(f"{tag}-s2", lambda: iq.run_txs2(board, 5, 1, "lut_asm_p", 4, 12,
                                               2, 3000, 4096, tag))

    try:
        raw("info")
        raw(f"dualprio {a.priority_mask}")
        raw(f"dualwriters {a.writers}")
        raw(f"mapkernel {a.map_kernel}")
        for clock in a.clocks:
            raw(f"clock {clock}")
            raw("bchkernel 2")
            baseline(clock)
            descriptor_bench(clock)
            if a.profile == "quick":
                dual(clock, 1, 1, 0)
                # Isolate DMA arbitration/placement schedules from CPU generation first.
                for schedule in (1, 2, 3):
                    dual(clock, 1, 1, 0, schedule=schedule, trial_ms=1000)
                for schedule in (0, 1, 2, 3):
                    dual(clock, 1, 1, 0, schedule=schedule, divider256=384, trial_ms=1000)
                for kernel in (0, 1, 2):
                    for cpw in (2, 1):
                        dual(clock, cpw, kernel, 1)
                for kernel in (1, 2):
                    for cpw in (2, 1):
                        dual(clock, cpw, kernel, 2)
                # Intermediate divider is exploratory until the complete pipeline is measured.
                for source in (1, 2):
                    for kernel in (1, 2):
                        dual(clock, 1, kernel, source, divider256=384)
            elif a.profile == "qualify":
                a.writers = 2
                raw("dualwriters 2")
                a.row_symbols = 1
                dual(clock, 2, 2, 2)
                a.row_symbols = 2
                dual(clock, 2, 2, 2)
                dual(clock, 1, 2, 2, divider256=384)
                record(f"legacy-onecore-{clock}", lambda: iq.run_txs2(
                    board, 5, 1, "lut_asm_p", 4, 12, 3, 3000, 4096, "dual-onecore", True))
                dual(clock, 2, 2, 3)
                if clock == 128000:
                    a.writers = 4
                    raw("dualwriters 4")
                    dual(clock, 1, 2, 1)
                    dual(clock, 1, 2, 4)
                    a.writers = 2
                    raw("dualwriters 2")
            elif a.profile == "split":
                for cpw, divider in ((2, 0), (1, 384), (1, 0)):
                    dual(clock, cpw, 2, 4, divider256=divider)
            elif a.profile == "spi":
                a.writers, a.row_symbols = 4, 2
                raw("dualwriters 4")
                dual(clock, 2, 2, 5)
                dual(clock, 1, 2, 5)
            elif a.profile == "quad":
                dual(clock, 2, 2, 1)
                dual(clock, 1, 2, 1)
                dual(clock, 2, 2, 3)
                dual(clock, 1, 2, 3, divider256=384)
                dual(clock, 1, 2, 4)
                if clock == 128000:
                    dual(clock, 1, 2, 5)
            elif a.profile == "gcs":
                if clock != 128000:
                    p.error("the on-chip SPI source uses --clocks 128000")
                for source in (5, 6):
                    dual(clock, 2, 2, source)
                    dual(clock, 1, 2, source)
            elif a.profile == "onecore":
                record(f"legacy-onecore-{clock}", lambda: iq.run_txs2(
                    board, 5, 1, "lut_asm_p", 4, 12, 3, 3000, 4096, "dual-onecore", True))
                for cpw in (3, 2):
                    dual(clock, cpw, 2, 3)
                dual(clock, 1, 2, 3, divider256=384)
            elif a.profile == "priority":
                for mask in (0, 1, 8, 9, 13, 15):
                    a.priority_mask = mask
                    raw(f"dualprio {mask}")
                    for source in (1, 2):
                        for cap_words in (0, 8, 4096):
                            dual(clock, 1, 2, source, schedule=1, cap_words=cap_words)
                a.priority_mask = 0
                raw("dualprio 0")
            else:
                # Soak the best measured descriptor kernel, not all C variants or known
                # immediate-stall full-rate probes. Fractional clocks remain digital-only.
                dual(clock, 2, 2, 2)
                if a.row_symbols == 2:
                    dual(clock, 1, 2, 2, divider256=384)
                    dual(clock, 2, 2, 3)
                    if clock == 128000:
                        dual(clock, 1, 2, 1, schedule=1, cap_words=8)
            dual(clock, 2, 1, 1, fault=1)
            dual(clock, 2, 1, 1, fault=2)
            baseline(clock, after=True)
    finally:
        try:
            raw("dualprio 0")
            raw("dualwriters 2")
            raw("clock 128000")
        finally:
            try:
                raw("bchkernel 0")
                raw("mapkernel 0")
            finally:
                board.s.close()
    if failures:
        raise RuntimeError(f"unexpected validation outcomes: {failures}")


if __name__ == "__main__":
    main()
