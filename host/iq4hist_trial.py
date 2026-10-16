"""Opt-in cyclic 8PSK L7/N4 hardware-pointer trial with GPIO28 DDR capture.

Build and flash separately. This runner drives GPIOs only when explicitly invoked;
its import and the companion native tests do not open a serial port.
"""
from __future__ import annotations

import argparse
import datetime as dt
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import zlib

import numpy as np

import iqbench as iq
from dual_trial import parse_capture
from pulse_profiles import FOUR_LEVELS_Q30, L7_COEFFICIENTS, L7_PROFILE, iq4hist_rows, iq4hist8_rows

SEED, INPUT_WORDS, CAP_WORDS = 0x1234abcd, 4096, 4096
CLOCK_HZ, POLL_US, MAX_POLL_US = 128000000, 128, 256


@lru_cache(maxsize=2)
def reference_source(bits=14):
    """Geometric constellation -> planar packed source and cyclic scalar FIR.

    The label mapping is derived from the complex angles rather than the firmware's
    sign lookup. FIR accumulation uses Q8 taps and Q30 coordinates with one final
    Q38 rounding; it does not index or reuse the generated full-history table.
    """
    phases = iq.m.xorshift32(SEED, INPUT_WORDS * 16) & 7
    points = np.exp(1j * (np.pi / 8 + phases * np.pi / 4))
    levels = np.asarray(FOUR_LEVELS_Q30, dtype=np.int64)
    coefficients = np.asarray(L7_COEFFICIENTS, dtype=np.int64).reshape(7, 4)
    packed, samples = [], []
    shifts = 2 * np.arange(16, dtype=np.uint32)
    for coordinate in (points.real, points.imag):
        codes = np.abs(coordinate[:, None] - levels[None, :] / 2**30).argmin(axis=1)
        packed.append((codes.astype(np.uint32).reshape(-1, 16) << shifts).sum(axis=1, dtype=np.uint32))
        values = levels[codes]
        prefix = np.concatenate([values[-6:], values])
        histories = np.lib.stride_tricks.sliding_window_view(prefix, 7)[:, ::-1]
        sums = histories @ coefficients
        if bits == 8:
            samples.append((((sums + (3399 << 31)) // (3399 << 32)) * 64).ravel())
        else:
            samples.append(((sums + (1 << 37)) >> 38).ravel())
    source = np.concatenate(packed).astype('<u4')
    bus = np.empty(len(samples[0]) * 2, dtype='<u2')
    bus[0::2] = (samples[0] & 0x3fff) | 0x4000
    bus[1::2] = samples[1] & 0x3fff
    return source, bus


@lru_cache(maxsize=2)
def table_crc(bits=14):
    rows = iq4hist8_rows() if bits == 8 else iq4hist_rows()
    return zlib.crc32(rows.astype('<u4').tobytes())


def firmware_identity(expected: str):
    # Firmware git_rev.h reports ten hex digits. Keep a dirty suffix significant.
    return expected[:10].lower() if re.fullmatch(r'[0-9a-fA-F]{40}', expected) else expected


def validate_capture(result: dict, lines: list[str], invert: int, bits: int = 14) -> dict:
    """Check ordered transport, every bus sample, and every DDR clock phase."""
    try:
        words = parse_capture(lines)
        if result.get('cap_error') or result.get('cap_words') != CAP_WORDS or len(words) != CAP_WORDS:
            raise ValueError('incomplete4096-word GPIO capture')
        if zlib.crc32(words.tobytes()) != result.get('cap_crc'):
            raise ValueError('capture CRC mismatch (serial transfer)')
        _, reference = reference_source(bits)
        bus = words & 0xffff
        candidates = np.flatnonzero(reference == bus[0])
        for j in range(1, 16):
            candidates = candidates[reference[(candidates + 17 * j) % len(reference)] == bus[j]]
        clock_bad = int(np.count_nonzero(((words >> 28) & 1) != ((words >> 14) & 1)))
        clock_high = int(np.count_nonzero(words & (1 << 28)))
        if len(candidates) != 1:
            return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                        cap_checked_bus_words=16, cap_clock_checked=CAP_WORDS,
                        cap_clock_mismatches=clock_bad, cap_clock_high=clock_high,
                        cap_error='capture does not have one fixed cyclic reference offset')
        offset = int(candidates[0])
        expected = reference[(offset + 17 * np.arange(CAP_WORDS)) % len(reference)]
        bad = np.flatnonzero(bus != expected)
        clock_ok = clock_bad == (CAP_WORDS if invert else 0) and 0 < clock_high < CAP_WORDS
        return dict(cap_transport_ok=True, cap_aligned=True, cap_offset=offset,
                    cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None,
                    cap_checked_bus_words=CAP_WORDS, cap_clock_checked=CAP_WORDS,
                    cap_clock_mismatches=clock_bad, cap_clock_high=clock_high,
                    cap_clock_ok=clock_ok)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(error))


def validate_run(result: dict, lines: list[str], cpw: int, ms: int, invert: int,
                 clock_hz: int = CLOCK_HZ, expected_git: str | None = None,
                 bits: int = 14, priority: int = 13) -> dict:
    """Fail closed on waveform/clock, immutable input, run identity and faults."""
    r, faults = result, []
    capture = validate_capture(r, lines, invert, bits)
    try:
        expected = dict(cmd='bench4live', clk_hz=clock_hz, source='cyclic_8psk',
                        cpw=cpw, divider256=256*cpw, ms=ms, invert=invert,
                        sps=4, L=7, row_profile=L7_PROFILE, row_symbols=1, row_words=2,
                        writers=2, seed=SEED, in_words=INPUT_WORDS, input_words=2*INPUT_WORDS,
                        ring_base=0x20000000, table_base=0x20000000,
                        ring_bytes=131072, table_bytes=131072, table_crc=table_crc(bits), input_base=0x20020000,
                        errors=0, pointer_errors=0, output_errors=0, txstalls_i=0, txstalls_q=0,
                        pointer_txstall_mask=0, restored=True, selectors_restored=True,
                        core1_idle=True, sample_math_busy=0.0, descriptor_gen_busy=0.0,
                        idle_wait='spin', reason='complete',
                        poll_interval_us=POLL_US, max_poll_interval_us=MAX_POLL_US,
                        cap_format='pins32', cap_words=CAP_WORDS, capture_count=CAP_WORDS,
                        cap_stride=17, cap_divider256=256*cpw, cap_fifo_only=False)
        if bits == 8:
            expected.update(cmd='bench4live8', row_words=1, table_bytes=65536,
                            sample_bits=8, low_bits=0, bus_priority=priority)
        if expected_git is not None:
            expected['git'] = firmware_identity(expected_git)
        faults += [key for key, value in expected.items() if r.get(key) != value]
        if r.get('error'):
            faults.append(str(r['error']))
        source, _ = reference_source(bits)
        source_crc = zlib.crc32(source.tobytes())
        if r.get('input_crc') != source_crc or r.get('input_after_crc') != source_crc:
            faults.append('immutable cyclic8PSK source CRC')
        if r.get('previous_words') != [int(source[INPUT_WORDS-1]), int(source[-1])]:
            faults.append('cyclic initial history')
        elapsed = int(r['elapsed_us'])
        if not ms*1000 <= elapsed <= ms*1000+1000:
            faults.append('run duration')
        rate = float(r['sym_rate'])
        if not math.isfinite(rate) or abs(rate-clock_hz/(8*cpw)) > 0.1:
            faults.append('symbol rate/divider')
        cycles = int(r['supervision_cycles'])
        busy = cycles / (elapsed*clock_hz/1e6)
        reported = float(r['supervision_busy'])
        if not 0 <= busy <= 1 or not math.isfinite(reported) or abs(reported-busy) > 1e-6:
            faults.append('supervision cycle fraction')
        if not POLL_US <= int(r['max_poll_gap_us']) <= MAX_POLL_US:
            faults.append('supervision poll deadline')
        if not elapsed//MAX_POLL_US <= int(r['polls']) <= elapsed//POLL_US+1:
            faults.append('supervision poll count')
        initial, final = [list(map(int, r[k])) for k in ('start_input_issue_words', 'input_issue_words')]
        if len(initial) != 2 or len(final) != 2:
            raise ValueError('two source axis cursors required')
        expected_reads = elapsed*rate/16e6
        if any(not 0 <= a <= b or abs(b-a-expected_reads) > 32+expected_reads*0.001
               for a, b in zip(initial, final)):
            faults.append('source issue rate/duration')
        if int(r['pointer_rxstall_mask']) & ~15:
            faults.append('pointer RX flags outside PIO block')
        if not (capture.get('cap_transport_ok') and capture.get('cap_aligned') and
                capture.get('cap_mismatches') == 0 and capture.get('cap_checked_bus_words') == CAP_WORDS):
            faults.append(capture.get('cap_error') or 'sparse waveform mismatch')
        if not capture.get('cap_clock_ok'):
            faults.append('DDR clock phase/control')
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
        faults.append(str(error))
    return dict(capture, faults=list(dict.fromkeys(faults)),
                classification='failed' if faults else 'clock_inversion_detected' if invert else 'digital_live_clean')


def validate_clock(result: dict, expected_git: str | None = None, clock_hz: int = CLOCK_HZ):
    if result.get('error') or result.get('cmd') != 'info' or result.get('clk_hz') != clock_hz:
        raise ValueError(f'clock{clock_hz//1000} failed: {result}')
    if expected_git is not None and result.get('git') != firmware_identity(expected_git):
        raise ValueError('firmware identity differs from --expected-git')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--cpws', nargs='+', type=int, choices=(1, 2, 4), default=[2])
    p.add_argument('--bits', type=int, choices=(8, 14), default=14)
    p.add_argument('--priorities', nargs='+', type=int, choices=range(16))
    p.add_argument('--clock-khz', type=int, choices=(128000, 256000), default=128000)
    p.add_argument('--inverts', nargs='+', type=int, choices=(0, 1), default=[0, 1])
    p.add_argument('--ms', type=int, default=1000)
    p.add_argument('--reps', type=int, default=1)
    p.add_argument('--port')
    p.add_argument('--expected-git', help='Exact runtime identity or a clean full40-digit commit SHA')
    p.add_argument('--build', type=Path, default=iq.ROOT/'firmware/build-b1')
    p.add_argument('--source', type=Path, default=iq.ROOT/'firmware')
    args = p.parse_args()
    if args.reps < 1 or not 100 <= args.ms <= 60000:
        p.error('--reps must be positive and --ms100..60000')
    priorities = args.priorities if args.priorities is not None else ([13] if args.bits == 8 else [0])
    if args.bits == 14 and (1 in args.cpws or priorities != [0] or args.clock_khz != 128000):
        p.error('14-bit B1 uses SYS128, cpw2/4 and its original priority0')
    if any(args.clock_khz*1000/(2*cpw) > 65000000 for cpw in args.cpws):
        p.error('requested per-axis DAC rate exceeds65MSPS')
    args.out.mkdir(parents=True, exist_ok=False)
    build, source = args.build.resolve(), args.source.resolve()
    cache = (build/'CMakeCache.txt').read_text()
    if 'IQ4HIST_EXPERIMENT:BOOL=ON' not in cache:
        p.error('--build must contain an IQ4HIST_EXPERIMENT=ON firmware build')
    if args.bits == 8 and 'PICO_STACK_SIZE=0x1000' not in (build/'build.ninja').read_text():
        p.error('B4 needs the4KiB core0 stack; configure with -DCORE0_STACK_SRAM_HI=ON')
    shutil.copy2(__file__, args.out/'driver.py')
    hashes = {}
    for name in ('iqbench.uf2', 'iqbench.elf'):
        shutil.copy2(build/name, args.out/name)
        hashes[name] = hashlib.sha256((args.out/name).read_bytes()).hexdigest()
    for name in ('CMakeCache.txt', 'memmap_iq.ld', 'git_rev.h'):
        shutil.copy2(build/name, args.out/name)
        hashes[name] = hashlib.sha256((args.out/name).read_bytes()).hexdigest()
    files = [f for f in (source/'src').rglob('*') if f.suffix in ('.c', '.h', '.S')]
    files += [source/'CMakeLists.txt']
    hashes.update({'firmware/'+str(f.relative_to(source)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    for relative in ('host/iq4hist_trial.py', 'host/iqbench.py', 'host/dual_trial.py',
                     'host/pulse_profiles.py', 'reference/iqlut.py'):
        hashes[relative] = hashlib.sha256((iq.ROOT/relative).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=args.ms, cpws=args.cpws,
                    inverts=args.inverts, reps=args.reps, bits=args.bits, priorities=priorities,
                    clock_khz=args.clock_khz, expected_firmware_git=args.expected_git,
                    setup='Digital GPIO output; coordinating engineer owns device and wiring',
                    scope='Cyclic8PSK L7/N4 hardware-pointer output; no live FEC/SPI or AFE/RF qualification',
                    head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=iq.ROOT, text=True).strip(),
                    source_firmware_directory=str(source), build_directory=str(build), sha256=hashes)
    (args.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    iq.LOGGING = False
    board = iq.Board(args.port)
    failed = False
    try:
        clock_result, _ = board.cmd(f'clock {args.clock_khz}')
        (args.out/'clock.json').write_text(json.dumps(clock_result, indent=2)+'\n')
        validate_clock(clock_result, args.expected_git, args.clock_khz*1000)
        for cpw in args.cpws:
            for invert in args.inverts:
                for priority in priorities:
                    for rep in range(args.reps):
                        command = f'bench4live {cpw} {args.ms} {invert}' if args.bits == 14 else \
                            f'bench4live8 {cpw} {args.ms} {invert} {priority}'
                        r, lines = board.cmd(command, timeout=args.ms/1000+20)
                        suffix = '' if args.bits == 14 else f'-prio{priority}'
                        (args.out/f'capture-{cpw}-{invert}{suffix}-{rep}.txt').write_text('\n'.join(lines)+'\n')
                        r.update(validate_run(r, lines, cpw, args.ms, invert,
                                              clock_hz=args.clock_khz*1000, expected_git=args.expected_git,
                                              bits=args.bits, priority=priority))
                        r['rep'] = rep
                        with (args.out/'runs.jsonl').open('a') as stream:
                            stream.write(json.dumps(r)+'\n')
                        print(json.dumps(r), flush=True)
                        failed |= bool(r['faults'])
    finally:
        board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
