"""Opt-in SYS128 cyclic 8PSK quad-writer SRAM-pointer trial.

Build and flash separately. Only an explicit invocation opens a serial port;
imports and the companion tests do not access hardware.
"""
from __future__ import annotations

import argparse
import datetime as dt
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import zlib

import numpy as np

import iqbench as iq
from dual_trial import parse_capture
from iq4hist_trial import firmware_identity, table_crc, validate_clock
from pulse_profiles import FOUR_LEVELS_Q30, L7_COEFFICIENTS, L7_PROFILE

SEED, PERIOD_SYMBOLS, INPUT_WORDS, CAP_WORDS = 0x1234abcd, 4096, 256, 4096
CLOCK_HZ, POLL_US, MAX_POLL_US = 128000000, 128, 256
TABLE_BASE, POINTER_BASE, POINTER_RING_WORDS = 0x20000000, 0x20020000, 2048
POINTER_WORDS = 4 * POINTER_RING_WORDS


@lru_cache(maxsize=1)
def reference_codes():
    """Derive planar codes from geometric PRBS constellation coordinates."""
    phases = iq.m.xorshift32(SEED, PERIOD_SYMBOLS) & 7
    points = np.exp(1j * (np.pi / 8 + phases * np.pi / 4))
    levels = np.asarray(FOUR_LEVELS_Q30, dtype=np.int64)
    return tuple(np.abs(axis[:, None] - levels[None, :] / 2**30).argmin(axis=1)
                 for axis in (points.real, points.imag))


@lru_cache(maxsize=2)
def reference_source(bits=8):
    """Packed source and scalar FIR bus period, independent of table/pointer C.

    The six preceding symbols come from this 4096-symbol period's tail. Each
    Q38 sum is rounded once, after all seven taps; no generated row is read.
    """
    if bits not in (8, 14):
        raise ValueError('sample bits must be8 or14')
    levels = np.asarray(FOUR_LEVELS_Q30, dtype=np.int64)
    taps = np.asarray(L7_COEFFICIENTS, dtype=np.int64).reshape(7, 4)
    packed, samples = [], []
    shifts = 2 * np.arange(16, dtype=np.uint32)
    for codes in reference_codes():
        packed.append((codes.astype(np.uint32).reshape(-1, 16) << shifts).sum(axis=1, dtype=np.uint32))
        values = levels[codes]
        rows = np.empty((PERIOD_SYMBOLS, 4), dtype=np.int64)
        for symbol in range(PERIOD_SYMBOLS):
            for phase in range(4):
                total = sum(int(values[(symbol-age) % PERIOD_SYMBOLS]) * int(taps[age, phase])
                            for age in range(7))
                rows[symbol, phase] = ((total + (3399 << 31)) // (3399 << 32)) * 64 \
                    if bits == 8 else (total + (1 << 37)) >> 38
        samples.append(rows.ravel())
    source = np.concatenate(packed).astype('<u4')
    bus = np.empty(PERIOD_SYMBOLS * 8, dtype='<u2')
    bus[0::2] = (samples[0] & 0x3fff) | 0x4000
    bus[1::2] = samples[1] & 0x3fff
    return source, bus


@lru_cache(maxsize=2)
def reference_pointers(bits=8):
    """I-even, Q-even, I-odd, Q-odd addresses, including cyclic history."""
    if bits not in (8, 14):
        raise ValueError('sample bits must be8 or14')
    axes = []
    symbols = np.arange(PERIOD_SYMBOLS)
    for codes in reference_codes():
        keys = sum(codes[(symbols-age) % PERIOD_SYMBOLS].astype(np.uint32) << (2*age)
                   for age in range(7))
        pointers = TABLE_BASE + keys * (4 if bits == 8 else 8)
        axes.append(pointers)
    return np.concatenate([axis[parity::2] for parity in (0, 1) for axis in axes]).astype('<u4')


def validate_capture(result: dict, lines: list[str], invert: int, bits: int = 8) -> dict:
    """Validate every stride-17 physical slot and GPIO28 against IQ_FLAG."""
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
            candidates = candidates[reference[(candidates + 17*j) % len(reference)] == bus[j]]
        clock_bad = int(np.count_nonzero(((words >> 28) & 1) != ((words >> 14) & 1)))
        clock_high = int(np.count_nonzero(words & (1 << 28)))
        if len(candidates) != 1:
            return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                        cap_checked_bus_words=16, cap_clock_checked=CAP_WORDS,
                        cap_clock_mismatches=clock_bad, cap_clock_high=clock_high,
                        cap_error='capture does not have one fixed cyclic reference offset')
        offset = int(candidates[0])
        expected = reference[(offset + 17*np.arange(CAP_WORDS)) % len(reference)]
        bad = np.flatnonzero(bus != expected)
        return dict(cap_transport_ok=True, cap_aligned=True, cap_offset=offset,
                    cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None,
                    cap_checked_bus_words=CAP_WORDS, cap_clock_checked=CAP_WORDS,
                    cap_clock_mismatches=clock_bad, cap_clock_high=clock_high,
                    cap_clock_ok=clock_bad == CAP_WORDS*invert and 0 < clock_high < CAP_WORDS)
    except (ValueError, TypeError, KeyError, OverflowError) as error:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(error))


def validate_run(result: dict, lines: list[str], cpw: int, ms: int, invert: int,
                 expected_git: str | None = None, bits: int = 8, priority: int = 13) -> dict:
    """Fail closed on identity, immutable rings, timing, capture and cleanup."""
    r, faults = result, []
    capture = validate_capture(r, lines, invert, bits)
    try:
        expected = dict(cmd='bench4quad8' if bits == 8 else 'bench4quad', clk_hz=CLOCK_HZ,
                        source='cyclic_8psk', cpw=cpw, divider256=256*cpw, ms=ms, invert=invert,
                        sps=4, L=7, row_profile=L7_PROFILE, row_symbols=1,
                        row_words=1 if bits == 8 else 2, sample_bits=bits, low_bits=0,
                        writers=4, chains_per_axis=2, pointer_source='sram',
                        period_symbols=PERIOD_SYMBOLS, seed=SEED, in_words=INPUT_WORDS,
                        input_words=2*INPUT_WORDS, input_base=POINTER_BASE, pointer_base=POINTER_BASE,
                        pointer_words=POINTER_WORDS, pointer_ring_words=POINTER_RING_WORDS,
                        pointer_bases=[POINTER_BASE+4*POINTER_RING_WORDS*j for j in range(4)],
                        ring_base=TABLE_BASE, table_base=TABLE_BASE, ring_bytes=131072,
                        table_bytes=65536 if bits == 8 else 131072, table_crc=table_crc(bits),
                        bus_priority=priority, errors=0, pointer_errors=0, output_errors=0,
                        txstalls_i=0, txstalls_q=0, restored=True, selectors_restored=True,
                        resources_released=True, core1_idle=True, sample_math_busy=0.0,
                        descriptor_gen_busy=0.0, idle_wait='spin', reason='complete',
                        dac_clock_pio=0, poll_interval_us=POLL_US, max_poll_interval_us=MAX_POLL_US,
                        cap_format='pins32', cap_words=CAP_WORDS, capture_count=CAP_WORDS,
                        cap_stride=17, cap_divider256=256*cpw, cap_fifo_only=False)
        if expected_git is not None:
            expected['git'] = firmware_identity(expected_git)
        faults += [key for key, value in expected.items() if r.get(key) != value]
        if r.get('error'):
            faults.append(str(r['error']))
        source, _ = reference_source(bits)
        if r.get('input_crc') != zlib.crc32(source.tobytes()):
            faults.append('cyclic8PSK source CRC')
        if r.get('previous_words') != [int(source[INPUT_WORDS-1]), int(source[-1])]:
            faults.append('cyclic initial history')
        pointer_crc = zlib.crc32(reference_pointers(bits).tobytes())
        if r.get('pointer_crc') != pointer_crc or r.get('pointer_after_crc') != pointer_crc:
            faults.append('immutable SRAM pointer CRC')
        source_base = int(r['source_base'])
        if source_base & 3 or not 0x20040000 <= source_base <= 0x20080000-8*INPUT_WORDS:
            faults.append('ephemeral source address')
        if not 0 <= int(r['dac_clock_sm']) < 4:
            faults.append('DDR clock SM')
        elapsed = int(r['elapsed_us'])
        if not ms*1000 <= elapsed <= ms*1000+1000:
            faults.append('run duration')
        rate = float(r['sym_rate'])
        if not math.isfinite(rate) or abs(rate-CLOCK_HZ/(8*cpw)) > 0.1:
            faults.append('symbol rate/divider')
        cycles = int(r['supervision_cycles'])
        busy = cycles / (elapsed*CLOCK_HZ/1e6)
        reported = float(r['supervision_busy'])
        if not 0 <= busy <= 1 or not math.isfinite(reported) or abs(reported-busy) > 1e-6:
            faults.append('supervision cycle fraction')
        if not POLL_US <= int(r['max_poll_gap_us']) <= MAX_POLL_US:
            faults.append('supervision poll deadline')
        if not elapsed//MAX_POLL_US <= int(r['polls']) <= elapsed//POLL_US+1:
            faults.append('supervision poll count')
        if not (capture.get('cap_transport_ok') and capture.get('cap_aligned') and
                capture.get('cap_mismatches') == 0 and capture.get('cap_checked_bus_words') == CAP_WORDS):
            faults.append(capture.get('cap_error') or 'sparse waveform mismatch')
        if not capture.get('cap_clock_ok'):
            faults.append('DDR clock phase/control')
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
        faults.append(str(error))
    return dict(capture, faults=list(dict.fromkeys(faults)),
                classification='failed' if faults else 'clock_inversion_detected' if invert else 'digital_quad_clean')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--cpws', nargs='+', type=int, choices=(1, 2, 4), default=[2, 1])
    p.add_argument('--bits', type=int, choices=(8, 14), default=8)
    p.add_argument('--priorities', nargs='+', type=int, choices=range(16), default=[13])
    p.add_argument('--inverts', nargs='+', type=int, choices=(0, 1), default=[0, 1])
    p.add_argument('--ms', type=int, default=1000)
    p.add_argument('--reps', type=int, default=1)
    p.add_argument('--port')
    p.add_argument('--expected-git', help='Exact runtime identity or a clean full40-digit commit SHA')
    p.add_argument('--build', type=Path, default=iq.ROOT/'firmware/build-b5')
    p.add_argument('--source', type=Path, default=iq.ROOT/'firmware')
    args = p.parse_args(argv)
    if args.reps < 1 or not 100 <= args.ms <= 60000:
        p.error('--reps must be positive and --ms100..60000')
    build, source = args.build.resolve(), args.source.resolve()
    if 'IQ4HIST_EXPERIMENT:BOOL=ON' not in (build/'CMakeCache.txt').read_text():
        p.error('--build must contain an IQ4HIST_EXPERIMENT=ON firmware build')
    if 'PICO_STACK_SIZE=0x1000' not in (build/'build.ninja').read_text():
        p.error('B5 needs the4KiB core0 stack; configure with -DCORE0_STACK_SRAM_HI=ON')
    args.out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, args.out/'driver.py')
    hashes = {}
    for name in ('iqbench.uf2', 'iqbench.elf', 'CMakeCache.txt', 'memmap_iq.ld', 'git_rev.h'):
        shutil.copy2(build/name, args.out/name)
        hashes[name] = hashlib.sha256((args.out/name).read_bytes()).hexdigest()
    files = [f for f in (source/'src').rglob('*') if f.suffix in ('.c', '.h', '.S', '.pio')]
    files += [source/'CMakeLists.txt']
    hashes.update({'firmware/'+str(f.relative_to(source)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    for relative in ('host/iq4quad_trial.py', 'host/iq4hist_trial.py', 'host/iqbench.py',
                     'host/dual_trial.py', 'host/pulse_profiles.py', 'reference/iqlut.py'):
        hashes[relative] = hashlib.sha256((iq.ROOT/relative).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=args.ms, cpws=args.cpws,
                    inverts=args.inverts, reps=args.reps, bits=args.bits, priorities=args.priorities,
                    clock_khz=CLOCK_HZ//1000, expected_firmware_git=args.expected_git,
                    setup='Digital GPIO output; coordinating engineer owns device and wiring',
                    scope='Cyclic8PSK L7/N4 SRAM pointers and four writers; no live FEC/SPI or AFE/RF qualification',
                    head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=iq.ROOT, text=True).strip(),
                    source_firmware_directory=str(source), build_directory=str(build), sha256=hashes)
    (args.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    iq.LOGGING = False
    board = iq.Board(args.port)
    failed = False
    try:
        clock_result, _ = board.cmd('info')
        (args.out/'clock.json').write_text(json.dumps(clock_result, indent=2)+'\n')
        validate_clock(clock_result, args.expected_git, CLOCK_HZ)
        for cpw in args.cpws:
            for invert in args.inverts:
                for priority in args.priorities:
                    for rep in range(args.reps):
                        command = f'bench4quad{"8" if args.bits == 8 else ""} {cpw} {args.ms} {invert} {priority}'
                        r, lines = board.cmd(command, timeout=args.ms/1000+20)
                        (args.out/f'capture-{cpw}-{invert}-prio{priority}-{rep}.txt').write_text('\n'.join(lines)+'\n')
                        r.update(validate_run(r, lines, cpw, args.ms, invert,
                                              expected_git=args.expected_git, bits=args.bits, priority=priority))
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
