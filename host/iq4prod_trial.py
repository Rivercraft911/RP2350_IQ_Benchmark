"""Opt-in L7/N4 8PSK four-producer/four-writer digital trial.

Build and flash separately. Only an explicit CLI invocation opens a serial
port; importing this module and running its tests do not access hardware.
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
from iq4hist_trial import (firmware_identity, reference_source as cyclic_source,
                          table_crc, validate_clock)
from pulse_profiles import FOUR_LEVELS_Q30, L7_COEFFICIENTS, L7_PROFILE

SEED, INPUT_WORDS, CAP_WORDS = 0x1234abcd, 4096, 4096
PERIOD_SYMBOLS, CLOCK_HZ, POLL_US, MAX_POLL_US = 65536, 128000000, 128, 256
TABLE_BASE, INPUT_BASE = 0x20000000, 0x20020000
SOURCE_EMPTY = 1 << 6


@lru_cache(maxsize=1)
def reference_codes():
    """Unpack the independently derived geometric constellation source."""
    source, _ = cyclic_source(8)
    shifts = 2*np.arange(16, dtype=np.uint32)
    return tuple(((source[axis*INPUT_WORDS:(axis+1)*INPUT_WORDS, None] >> shifts) & 3).ravel()
                 for axis in range(2))


@lru_cache(maxsize=1)
def reference_pointers():
    """Expected producer rows in I-even,Q-even,I-odd,Q-odd stream order.

    Every producer sees every axis symbol; parity selects emitted rows after
    updating the complete seven-symbol history. The first row uses cyclic
    prehistory from the source tail, including the last six codes.
    """
    symbols = np.arange(PERIOD_SYMBOLS)
    axes = []
    for codes in reference_codes():
        keys = sum(codes[(symbols-age) % PERIOD_SYMBOLS].astype(np.uint32) << (2*age)
                   for age in range(7))
        axes.append(TABLE_BASE+4*keys)
    return np.asarray([axis[parity::2] for parity in (0, 1) for axis in axes], dtype='<u4')


def xorshift_step(value):
    value ^= (value << 13) & 0xffffffff
    value ^= value >> 17
    value ^= (value << 5) & 0xffffffff
    return value


def xorshift_jump(value, steps):
    """Advance the linear PRBS in O(log steps), without replaying a long run."""
    if type(steps) is not int or steps < 0:
        raise ValueError('nonnegative integer PRBS step count required')

    def transform(columns, word):
        result = 0
        while word:
            bit = word & -word
            result ^= columns[bit.bit_length()-1]
            word ^= bit
        return result

    columns = [xorshift_step(1 << j) for j in range(32)]
    while steps:
        if steps & 1:
            value = transform(columns, value)
        steps >>= 1
        if steps:
            columns = [transform(columns, column) for column in columns]
    return value


def live_source_words(first_word, words):
    """Direct packed PRBS contract; one complete code word per axis/group.

    Q's code-bit parity is opposite I's, so one coordinate has the large
    magnitude and the other the small magnitude. Both signs remain free.
    """
    if type(first_word) is not int or first_word < 0 or type(words) is not int or words < 0:
        raise ValueError('nonnegative integer source window required')
    state = xorshift_jump(SEED, 2*first_word)
    source = np.empty((2, words), dtype='<u4')
    for index in range(words):
        state = xorshift_step(state)
        i = state
        state = xorshift_step(state)
        q = (state & 0x55555555) | ((~(i ^ (i >> 1) ^ state) & 0x55555555) << 1)
        source[:, index] = i, q
    return source


def live_reference_bus(first_symbol, symbols):
    """Finite scalar FIR reference with six known zero-code startup symbols."""
    if type(first_symbol) is not int or first_symbol < 0 or type(symbols) is not int or symbols < 1:
        raise ValueError('positive finite source symbol window required')
    first_word = max(0, (first_symbol-6)//16)
    stop_word = (first_symbol+symbols+15)//16
    source = live_source_words(first_word, stop_word-first_word)
    shifts = 2*np.arange(16, dtype=np.uint32)
    codes = ((source[:, :, None] >> shifts) & 3).reshape(2, -1)
    indices = np.arange(first_symbol, first_symbol+symbols)[:, None]-np.arange(7)
    relative = indices-first_word*16
    levels = np.asarray(FOUR_LEVELS_Q30, dtype=np.int64)
    taps = np.asarray(L7_COEFFICIENTS, dtype=np.int64).reshape(7, 4)
    bus = np.empty(8*symbols, dtype='<u2')
    for axis in range(2):
        history = codes[axis][np.maximum(relative, 0)]
        history = np.where(indices < 0, 0, history)
        sums = levels[history] @ taps
        samples = ((sums+(3399 << 31))//(3399 << 32)*64).ravel()
        bus[axis::2] = (samples & 0x3fff) | (0x4000 if axis == 0 else 0)
    return bus


def live_ring_crc(published_words):
    """CRC of the latest published full ring in physical SRAM order."""
    if type(published_words) is not int or published_words < INPUT_WORDS:
        raise ValueError('a complete initial live ring must be published')
    source = live_source_words(published_words-INPUT_WORDS, INPUT_WORDS)
    source = np.roll(source, published_words % INPUT_WORDS, axis=1)
    return zlib.crc32(source.tobytes())


def _capture_reference(result, live):
    if live:
        word = result['cap_source_word']
        if type(word) is not int or word < 0:
            raise ValueError('absolute capture source word required')
        first = max(0, word*16-4096)
        reference = live_reference_bus(first, 32768)
        # Keep the complete capture inside this finite window; never accept
        # cyclic wrapping or fit separate offsets to individual rows/axes.
        stop = min((word*16+12288-first)*8, len(reference)-17*(CAP_WORDS-1))
        return reference, 0, stop, False
    _, reference = cyclic_source(8)
    return reference, 0, len(reference), True


def validate_capture(result: dict, lines: list[str], invert: int, *, live=False) -> dict:
    """Check ordered transport, all physical GPIO slots, and GPIO28 parity."""
    try:
        words = parse_capture(lines)
        if result.get('cap_error') or result.get('cap_words') != CAP_WORDS or len(words) != CAP_WORDS:
            raise ValueError('incomplete4096-word GPIO capture')
        if zlib.crc32(words.tobytes()) != result.get('cap_crc'):
            raise ValueError('capture CRC mismatch (serial transfer)')
        reference, start, stop, cyclic = _capture_reference(result, live)
        bus = words & 0xffff
        candidates = np.flatnonzero(reference[start:stop] == bus[0])+start
        for j in range(1, 16):
            indices = candidates+17*j
            if cyclic:
                indices %= len(reference)
            candidates = candidates[reference[indices] == bus[j]]
        clock_bad = int(np.count_nonzero(((words >> 28) & 1) != ((words >> 14) & 1)))
        clock_high = int(np.count_nonzero(words & (1 << 28)))
        common = dict(cap_transport_ok=True, cap_clock_checked=CAP_WORDS,
                      cap_clock_mismatches=clock_bad, cap_clock_high=clock_high,
                      cap_clock_ok=clock_bad == CAP_WORDS*invert and 0 < clock_high < CAP_WORDS)
        if len(candidates) != 1:
            return dict(common, cap_aligned=False, cap_mismatches=None,
                        cap_checked_bus_words=16,
                        cap_error='capture does not have one fixed reference offset')
        offset = int(candidates[0])
        indices = offset+17*np.arange(CAP_WORDS)
        if cyclic:
            indices %= len(reference)
        expected = reference[indices]
        bad = np.flatnonzero(bus != expected)
        checked = dict(common, cap_aligned=True, cap_offset=offset,
                       cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None,
                       cap_checked_bus_words=CAP_WORDS)
        if live:
            checked['cap_global_bus_offset'] = max(0, result['cap_source_word']*16-4096)*8+offset
        return checked
    except (ValueError, TypeError, KeyError, OverflowError, IndexError) as error:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(error))


def validate_run(result: dict, lines: list[str], cpw: int, ms: int, invert: int,
                 expected_git: str | None = None, priority: int = 13,
                 *, clock_hz: int = CLOCK_HZ, live=False, starve_ms=0) -> dict:
    """Fail closed on source ownership, resources, waveform, timing and cleanup."""
    r, faults = result, []
    capture = validate_capture(r, lines, invert, live=live)
    try:
        starving = bool(live and starve_ms)
        expected = dict(cmd='bench4prod8live' if live else 'bench4prod8', clk_hz=clock_hz,
                        source='runtime_8psk_prbs' if live else 'cyclic_8psk',
                        cpw=cpw, divider256=256*cpw, ms=ms, invert=invert,
                        sps=4, L=7, row_profile=L7_PROFILE, row_symbols=1, row_words=1,
                        writers=4, producers=4, chains_per_axis=2, pointer_source='pio',
                        sample_bits=8, low_bits=0, seed=SEED, in_words=INPUT_WORDS,
                        input_words=2*INPUT_WORDS, input_base=INPUT_BASE,
                        ring_base=TABLE_BASE, table_base=TABLE_BASE, ring_bytes=131072,
                        table_bytes=65536, table_crc=table_crc(8), bus_priority=priority,
                        errors=0, pointer_errors=0, output_errors=0, txstalls_i=0, txstalls_q=0,
                        restored=True, selectors_restored=True, resources_released=True,
                        core1_idle=not live, sample_math_busy=0.0, descriptor_gen_busy=0.0,
                        idle_wait='spin', reason='complete', dac_clock_pio=0,
                        producer_pios=[0, 0, 0, 1], poll_interval_us=POLL_US,
                        max_poll_interval_us=MAX_POLL_US, cap_format='pins32',
                        cap_words=CAP_WORDS, capture_count=CAP_WORDS, cap_stride=17,
                        cap_divider256=256*cpw, cap_fifo_only=False)
        if live:
            expected.update(period_symbols=0, source_generator='xorshift32_packed_v1',
                            grant_mode='finite_software_rearm', batch_words=512,
                            cursor_kind='completed_batch_words', source_empty=starving,
                            starve_ms=starve_ms)
        else:
            expected.update(period_symbols=PERIOD_SYMBOLS, source_generator='iq8_fill',
                            source_empty=False, starve_ms=0, grant_mode='endless_immutable',
                            batch_words=512, cursor_kind='issued_words', runtime_gen_words=0,
                            source_gen_cycles=0, source_gen_busy=0., worker_polls=0,
                            worker_max_gap_cycles=0, worker_service_cycles=0)
        if starving:
            for key in ('errors', 'pointer_errors', 'output_errors', 'txstalls_i', 'txstalls_q'):
                del expected[key]
            expected['reason'] = 'SOURCE_EMPTY'
        if expected_git is not None:
            expected['git'] = firmware_identity(expected_git)
        faults += [key for key, value in expected.items() if r.get(key) != value]
        if r.get('error'):
            faults.append(str(r['error']))
        if live:
            if r.get('input_crc') != live_ring_crc(INPUT_WORDS):
                faults.append('initial runtime source CRC')
            if r.get('previous_words') != [0, 0]:
                faults.append('runtime zero-code startup history')
        else:
            source, _ = cyclic_source(8)
            crc = zlib.crc32(source.tobytes())
            if r.get('input_crc') != crc or r.get('input_after_crc') != crc:
                faults.append('immutable cyclic8PSK source CRC')
            if r.get('previous_words') != [int(source[INPUT_WORDS-1]), int(source[-1])]:
                faults.append('cyclic initial history')
        pios, sms = r['producer_pios'], r['producer_sms']
        if (len(pios) != 4 or len(sms) != 4 or any(type(v) is not int or not 0 <= v < 4 for v in sms) or
                len(set(zip(pios, sms))) != 4):
            faults.append('independent producer SM ownership')
        clock_sm = r['dac_clock_sm']
        if type(clock_sm) is not int or not 0 <= clock_sm < 4 or (0, clock_sm) in set(zip(pios, sms)):
            faults.append('DDR clock SM ownership')
        producer_mask = sum(1 << (4*p+s) for p, s in zip(pios, sms))
        tx, rx = r['pointer_txstall_mask'], r['pointer_rxstall_mask']
        if (type(tx) is not int or type(rx) is not int or tx < 0 or rx < 0 or
                (tx | rx) & ~producer_mask or (not starving and tx) or (starving and not tx)):
            faults.append('producer FIFO sticky flags')
        elapsed = int(r['elapsed_us'])
        deadline = (starve_ms*1000, starve_ms*1000+INPUT_WORDS*16*8*cpw*1e6/clock_hz+2000) \
            if starving else (ms*1000, ms*1000+1000)
        if not deadline[0] <= elapsed <= deadline[1]:
            faults.append('run duration')
        rate = float(r['sym_rate'])
        if not math.isfinite(rate) or abs(rate-clock_hz/(8*cpw)) > 0.1:
            faults.append('symbol rate/divider')
        cycles = int(r['supervision_cycles'])
        busy = cycles/(elapsed*clock_hz/1e6)
        reported = float(r['supervision_busy'])
        if not 0 <= busy <= 1 or not math.isfinite(reported) or abs(reported-busy) > 1e-6:
            faults.append('supervision cycle fraction')
        if not POLL_US <= int(r['max_poll_gap_us']) <= MAX_POLL_US:
            faults.append('supervision poll deadline')
        if not elapsed//MAX_POLL_US <= int(r['polls']) <= elapsed//POLL_US+1:
            faults.append('supervision poll count')
        initial, final = [r[k] for k in ('start_input_issue_words', 'input_issue_words')]
        if len(initial) != 4 or len(final) != 4 or any(type(v) is not int for v in initial+final):
            raise ValueError('four integer source producer cursors required')
        expected_reads = elapsed*rate/16e6
        if any(not 0 <= a <= b or abs(b-a-expected_reads) > 1024+expected_reads*.001
               for a, b in zip(initial, final)):
            faults.append('source issue rate/duration')
        if live:
            faults.extend(live_faults(r, elapsed, clock_hz, cpw, ms, starve_ms))
        if not (capture.get('cap_transport_ok') and capture.get('cap_aligned') and
                capture.get('cap_mismatches') == 0 and capture.get('cap_checked_bus_words') == CAP_WORDS):
            faults.append(capture.get('cap_error') or 'sparse waveform mismatch')
        if not capture.get('cap_clock_ok'):
            faults.append('DDR clock phase/control')
    except (KeyError, ValueError, TypeError, ZeroDivisionError, OverflowError) as error:
        faults.append(str(error))
    return dict(capture, faults=list(dict.fromkeys(faults)),
                classification='failed' if faults else 'source_starvation_detected' if live and starve_ms
                else 'clock_inversion_detected' if invert
                else 'digital_live_producers_clean' if live else 'digital_cyclic_producers_clean')


def live_faults(result, elapsed, clock_hz, cpw, ms, starve_ms):
    """Prove publication/retirement arithmetic independently of DMA cursor speed."""
    r, faults = result, []
    names = ('published_words', 'reusable_words', 'runtime_gen_words',
             'source_gen_cycles', 'cap_source_word', 'worker_polls', 'worker_max_gap_cycles',
             'worker_service_cycles')
    if any(type(r.get(key)) is not int or r[key] < 0 for key in names):
        return ['invalid runtime source counters']
    published, reusable = (r[key] for key in names[:2])
    completed, grants = r['input_issue_words'], r.get('granted_words')
    if not isinstance(grants, list) or len(grants) != 4 or any(type(v) is not int or v < 0 for v in grants):
        return ['four integer finite source grant limits required']
    if (published < INPUT_WORDS or published % 512 or reusable != min(completed) or
            r.get('completed_words') != completed or published-reusable > INPUT_WORDS or
            any(not c <= g <= published or not 0 <= g-c <= 512 or c % 512 or g % 512
                for c, g in zip(completed, grants))):
        faults.append('finite source publication/grant/retirement ownership')
    if r['runtime_gen_words'] != published-INPUT_WORDS or r['runtime_gen_words'] <= 0:
        faults.append('runtime source generation count')
    if r.get('input_after_crc') != live_ring_crc(published):
        faults.append('final runtime source ring CRC')
    source_busy = r.get('source_gen_busy')
    if (type(source_busy) not in (float, int) or not math.isfinite(source_busy) or
            not 0 < source_busy <= 1 or r['source_gen_cycles'] <= 0 or
            abs(source_busy-r['source_gen_cycles']/(elapsed*clock_hz/1e6)) > 1e-6):
        faults.append('runtime source generation cycle fraction')
    if any(r[key] <= 0 for key in ('worker_polls', 'worker_max_gap_cycles', 'worker_service_cycles')):
        faults.append('runtime input service accounting')
    cursor = r['cap_source_word']
    capture_ms = starve_ms/2 if starve_ms else ms/2
    if not 0 <= cursor <= reusable or abs(cursor-clock_hz/(8*cpw)*capture_ms/16000) > 1024:
        faults.append('runtime capture source cursor/window')
    if starve_ms:
        # SOURCE_EMPTY is an intentional fenced stop. Only its producer flag,
        # propagated bench flags and consequent writer TX stalls are accepted.
        pointer, output, errors = (r.get(key) for key in ('pointer_errors', 'output_errors', 'errors'))
        if (any(type(v) is not int or v < 0 for v in (pointer, output, errors)) or
                not pointer & SOURCE_EMPTY or pointer & ~(SOURCE_EMPTY | (1 << 3)) or
                output & ~3 or not errors & (1 << 21) or errors & ~((1 << 21) | (1 << 16) | 3)):
            faults.append('starvation stop contains unrelated faults')
        if any(type(r.get(key)) is not int or r[key] < 0 for key in ('txstalls_i', 'txstalls_q')):
            faults.append('invalid starvation writer diagnostics')
    return faults


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--cpws', nargs='+', type=int, choices=(1, 2, 4), default=[2, 1])
    p.add_argument('--priorities', nargs='+', type=int, choices=range(16), default=[0, 13])
    p.add_argument('--inverts', nargs='+', type=int, choices=(0, 1), default=[0, 1])
    p.add_argument('--clock-khz', type=int, choices=(128000, 256000), default=128000)
    p.add_argument('--live', action='store_true')
    p.add_argument('--starve-ms', type=int, default=0)
    p.add_argument('--ms', type=int, default=3000)
    p.add_argument('--reps', type=int, default=1)
    p.add_argument('--port')
    p.add_argument('--expected-git', help='Exact runtime identity or a clean full40-digit commit SHA')
    p.add_argument('--build', type=Path, default=iq.ROOT/'firmware/build-b6')
    p.add_argument('--source', type=Path, default=iq.ROOT/'firmware')
    args = p.parse_args(argv)
    if args.reps < 1 or not 100 <= args.ms <= 60000:
        p.error('--reps must be positive and --ms100..60000')
    if args.starve_ms and (not args.live or not 10 <= args.starve_ms < args.ms):
        p.error('--starve-ms requires --live and must be10..--ms-1')
    if any(args.clock_khz*1000/(2*cpw) > 65000000 for cpw in args.cpws):
        p.error('requested per-axis DAC rate exceeds65MSPS')
    build, source = args.build.resolve(), args.source.resolve()
    if 'IQ4HIST_EXPERIMENT:BOOL=ON' not in (build/'CMakeCache.txt').read_text():
        p.error('--build must contain an IQ4HIST_EXPERIMENT=ON firmware build')
    if 'PICO_STACK_SIZE=0x1000' not in (build/'build.ninja').read_text():
        p.error('B6 needs the4KiB core0 stack; configure with -DCORE0_STACK_SRAM_HI=ON')
    args.out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, args.out/'driver.py')
    hashes = {}
    for name in ('iqbench.uf2', 'iqbench.elf', 'CMakeCache.txt', 'memmap_iq.ld', 'git_rev.h'):
        shutil.copy2(build/name, args.out/name)
        hashes[name] = hashlib.sha256((args.out/name).read_bytes()).hexdigest()
    files = [f for f in (source/'src').rglob('*') if f.suffix in ('.c', '.h', '.S', '.pio')]
    files += [source/'CMakeLists.txt']
    hashes.update({'firmware/'+str(f.relative_to(source)):hashlib.sha256(f.read_bytes()).hexdigest() for f in files})
    for relative in ('host/iq4prod_trial.py', 'host/iq4hist_trial.py', 'host/iqbench.py',
                     'host/dual_trial.py', 'host/pulse_profiles.py', 'reference/iqlut.py'):
        hashes[relative] = hashlib.sha256((iq.ROOT/relative).read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=args.ms, cpws=args.cpws,
                    inverts=args.inverts, reps=args.reps, priorities=args.priorities, live=args.live,
                    starve_ms=args.starve_ms, clock_khz=args.clock_khz, expected_firmware_git=args.expected_git,
                    setup='Digital GPIO output; coordinating engineer owns device and wiring',
                    scope='8PSK L7/N4 PIO pointers and four writers; no FEC/SPI or AFE/RF qualification',
                    head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=iq.ROOT, text=True).strip(),
                    source_firmware_directory=str(source), build_directory=str(build), sha256=hashes)
    (args.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    iq.LOGGING = False
    board = iq.Board(args.port)
    failed = False
    try:
        clock_result, _ = board.cmd('info')
        (args.out/'clock.json').write_text(json.dumps(clock_result, indent=2)+'\n')
        validate_clock(clock_result, args.expected_git, args.clock_khz*1000)
        for cpw in args.cpws:
            for invert in args.inverts:
                for priority in args.priorities:
                    for rep in range(args.reps):
                        command = f'bench4prod8{"live" if args.live else ""} {cpw} {args.ms} {invert} {priority}'
                        if args.live:
                            command += f' {args.starve_ms}'
                        r, lines = board.cmd(command, timeout=args.ms/1000+20)
                        (args.out/f'capture-{cpw}-{invert}-prio{priority}-{rep}.txt').write_text('\n'.join(lines)+'\n')
                        r.update(validate_run(r, lines, cpw, args.ms, invert,
                                              expected_git=args.expected_git, priority=priority,
                                              clock_hz=args.clock_khz*1000, live=args.live, starve_ms=args.starve_ms))
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
