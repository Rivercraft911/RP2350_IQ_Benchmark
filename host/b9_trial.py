"""Complete B9 memory-GCS -> 8PSK 2/3 -> L7/OUT8 digital trial.

Import and --self-test are entirely offline. A device run requires an explicit
--port and --out; the runner never builds, flashes, or discovers serial ports.
The scalar reference constructs real GCS BBFRAMEs, BCH/LDPC and complete PLFRAMEs
before applying the seven-symbol FIR directly, without the firmware row table.
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

import c3_fec_trial as fec
import iqbench as iq
from dual_trial import parse_capture
from iq4hist_trial import firmware_identity, table_crc, validate_clock
from pulse_profiles import FOUR_LEVELS_Q30, L7_COEFFICIENTS, L7_PROFILE

CLOCK_HZ, CODE_INDEX = 128000000, 5
FRAME_SYMBOLS, RING_GROUPS, CAP_WORDS, STRIDE = fec.FRAME_SYMBOLS, 4096, 4096, 17
BUS_PER_SYMBOL, SPAN, OPTIONS = 8, 7, 251


@lru_cache(maxsize=8)
def reference_bus(first_symbol: int, symbols: int) -> np.ndarray:
    """Finite chronological stream, including real inter-frame and startup history.

    The producer startup registers are zero on both axes, so the six symbols before
    frame zero use code zero. Frames are never padded to a sixteen-symbol boundary.
    """
    if type(first_symbol) is not int or first_symbol < 0 or type(symbols) is not int or symbols < 1:
        raise ValueError('positive finite source symbol window required')
    first_frame = max(0, (first_symbol - (SPAN - 1)) // FRAME_SYMBOLS)
    last_frame = (first_symbol + symbols - 1) // FRAME_SYMBOLS + 1
    codes = np.concatenate([fec.frame_codes(index, CODE_INDEX)
                            for index in range(first_frame, last_frame)], axis=1)
    absolute = np.arange(first_symbol, first_symbol + symbols)[:, None] - np.arange(SPAN)
    relative = absolute - first_frame * FRAME_SYMBOLS
    levels = np.asarray(FOUR_LEVELS_Q30, dtype=np.int64)
    taps = np.asarray(L7_COEFFICIENTS, dtype=np.int64).reshape(SPAN, 4)
    bus = np.empty(BUS_PER_SYMBOL * symbols, dtype='<u2')
    for axis in range(2):
        history = codes[axis][np.maximum(relative, 0)]
        history = np.where(absolute < 0, 0, history)
        sums = levels[history] @ taps
        samples = ((sums + (3399 << 31)) // (3399 << 32) * 64).ravel()
        bus[axis::2] = (samples & 0x3fff) | (0x4000 if axis == 0 else 0)
    bus.flags.writeable = False
    return bus


def capture_window(result: dict) -> tuple[int, np.ndarray, int, int]:
    """Bounded source cursor window; no cyclic replay or per-sample alignment."""
    word = result['cap_source_word']
    if type(word) is not int or word < 0:
        raise ValueError('absolute complete-group capture cursor required')
    first = max(0, word * 16 - 4096)
    bus = reference_bus(first, 32768)
    stop = min((word * 16 + 12288 - first) * BUS_PER_SYMBOL,
               len(bus) - STRIDE * (CAP_WORDS - 1))
    return first * BUS_PER_SYMBOL, bus, 0, stop


def validate_capture(result: dict, lines: list[str], invert: int) -> dict:
    """Check all 4096 captured bus slots and GPIO28 against one finite offset."""
    try:
        words = parse_capture(lines)
        if (result.get('cap_error') or result.get('cap_words') != CAP_WORDS or
                len(words) != CAP_WORDS or result.get('cap_stride') != STRIDE or
                result.get('cap_format') != 'pins32'):
            raise ValueError('complete pins32 capture at stride 17 required')
        if zlib.crc32(words.tobytes()) != result.get('cap_crc'):
            raise ValueError('capture CRC mismatch (serial transfer)')
        base, reference, start, stop = capture_window(result)
        bus = (words & 0xffff).astype('<u2')
        candidates = np.flatnonzero(reference[start:stop] == bus[0]) + start
        for index in range(1, 32):
            candidates = candidates[reference[candidates + STRIDE * index] == bus[index]]
            if not len(candidates):
                break
        clock_bad = int(np.count_nonzero(((words >> 28) & 1) != ((words >> 14) & 1)))
        clock_high = int(np.count_nonzero(words & (1 << 28)))
        common = dict(cap_transport_ok=True, cap_clock_checked=CAP_WORDS,
                      cap_clock_mismatches=clock_bad, cap_clock_high=clock_high,
                      cap_clock_ok=clock_bad == CAP_WORDS * invert and 0 < clock_high < CAP_WORDS)
        if len(candidates) != 1:
            return dict(common, cap_aligned=False, cap_mismatches=None,
                        cap_checked_bus_words=32, cap_candidates=int(len(candidates)),
                        cap_error='capture does not have one fixed finite reference offset')
        offset = int(candidates[0])
        expected = reference[offset + STRIDE * np.arange(CAP_WORDS)]
        bad = np.flatnonzero(expected != bus)
        first_bad = int(bad[0]) if len(bad) else None
        return dict(common, cap_aligned=True, cap_global_bus_offset=base + offset,
                    cap_mismatches=int(len(bad)), cap_first_bad=first_bad,
                    cap_got_first_bad=int(bus[first_bad]) if first_bad is not None else None,
                    cap_want_first_bad=int(expected[first_bad]) if first_bad is not None else None,
                    cap_checked_bus_words=CAP_WORDS)
    except (ValueError, TypeError, KeyError, IndexError, OverflowError) as error:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(error))


def capture_lines(words: np.ndarray) -> tuple[dict, list[str]]:
    """Offline fixture transport, in the exact firmware capture format."""
    words = np.asarray(words, dtype='<u4')
    lines = ['@cap ' + str(index) + ' ' + ' '.join(f'{int(word):08x}' for word in words[index:index + 16])
             for index in range(0, len(words), 16)]
    return dict(cap_words=len(words), cap_crc=zlib.crc32(words.tobytes()), cap_stride=STRIDE,
                cap_format='pins32'), lines


def _integer(value):
    return type(value) is int and value >= 0


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def run_faults(result: dict, cpw: int, ms: int, invert: int, priority: int = 13,
               slow_us: int = 0, options: int = OPTIONS,
               expected_git: str | None = None) -> list[str]:
    """Fail closed on profile, finite publication ownership, FEC CRC and output faults."""
    r, faults = result, []
    if r.get('error'):
        return [f"firmware error: {r['error']}"]
    expected = dict(cmd='b9', clk_hz=CLOCK_HZ, cpw=cpw, ms=ms, invert=invert,
                    bus_priority=priority, slow_us=slow_us, options=options,
                    source='memory_gcs', source_period_bytes=fec.SOURCE_BYTES,
                    code_index=CODE_INDEX, s2_code='normal 2/3', pilots=True, gold=0,
                    frame_symbols=FRAME_SYMBOLS, frame_cycles=FRAME_SYMBOLS * 8 * cpw,
                    ring_groups=RING_GROUPS, sample_bits=8, sps=4, L=SPAN,
                    row_profile=L7_PROFILE, row_symbols=1, row_words=1, writers=4, producers=4,
                    chains_per_axis=2, pointer_source='pio', table_bytes=65536,
                    table_crc=table_crc(8), previous_words=[0, 0],
                    grant_mode='finite_irq_rearm', batch_words=512,
                    cursor_kind='completed_batch_words', source_empty=bool(slow_us),
                    table_base=0x20000000, context_base=0x20010000, input_base=0x20020000,
                    restored=True, selectors_restored=True, resources_released=True)
    if expected_git:
        expected['git'] = firmware_identity(expected_git)
    faults.extend(f'{key}={r.get(key)!r}, expected {value!r}'
                  for key, value in expected.items() if r.get(key) != value)
    if not isinstance(r.get('git'), str) or not r['git']:
        faults.append('missing firmware identity')
    counters = ('elapsed_us', 'accounting_elapsed_us', 'published_frames', 'published_groups', 'pending_fill',
                'stream_frames', 'stream_groups', 'published_words', 'reusable_words',
                'max_service_gap_us', 'errors', 'pointer_errors', 'output_errors',
                'txstalls_i', 'txstalls_q', 'cap_source_word', 'pointer_txstall_mask',
                'grant_irq_calls', 'grant_irq_cycles', 'grant_irq_max_cycles', 'context_bytes',
                'supervise_cycles')
    invalid = [key for key in counters if not _integer(r.get(key))]
    if invalid:
        return faults + ['invalid runtime counters: ' + ', '.join(invalid)]
    elapsed = r['elapsed_us']
    if not elapsed:
        return faults + ['zero elapsed duration']
    if r['accounting_elapsed_us'] < elapsed:
        return faults + ['accounting interval ends before the output loop interval']
    if not 0 < r['context_bytes'] <= 65536:
        faults.append('FEC context exceeds the OUT8 table reservation tail')
    if not (r['grant_irq_calls'] > 0 and 0 < r['grant_irq_max_cycles'] <= r['grant_irq_cycles']):
        faults.append('missing finite-grant IRQ cycle accounting')
    if r.get('grant_irq_index') not in (2, 3):
        faults.append('finite-grant DMA IRQ ownership differs')
    # Output cursor/rate checks use the physical run loop. Core1 may finish its
    # current bounded slice while the engines stop and join; utilization uses
    # the explicitly longer interval ending at that join, before final drain.
    budget = r['accounting_elapsed_us'] * CLOCK_HZ / 1e6
    if (not _finite(r.get('supervise_busy')) or
            not math.isclose(r['supervise_cycles'] / budget, r['supervise_busy'], abs_tol=.000002)):
        faults.append('supervision busy fraction differs from charged cycles')
    rate = CLOCK_HZ / (8 * cpw)
    if not _finite(r.get('sym_rate')) or not math.isclose(r['sym_rate'], rate, abs_tol=0.1):
        faults.append('symbol rate differs from requested divider')
    final, stream = r['published_frames'], r['stream_frames']
    if not 2 <= stream <= final <= stream + 2:
        faults.append('stream/final drained frame publication differs')
    if (r['stream_groups'] != stream * FRAME_SYMBOLS // 16 or
            r['published_groups'] != final * FRAME_SYMBOLS // 16 or
            r['pending_fill'] != final * FRAME_SYMBOLS % 16 or
            r['published_words'] != r['stream_groups']):
        faults.append('frame-to-complete-group publication differs')
    try:
        reference = fec.ring_reference(CODE_INDEX, final, RING_GROUPS)
        for key in ('code_crc', 'pending_fill'):
            if r.get(key) != reference[key]:
                faults.append(f'{key} differs from independent continuous GCS/FEC/PL reference')
        if 'pending_codes' in r and r['pending_codes'] != reference['pending_codes']:
            faults.append('pending carry differs from independent continuous GCS/FEC/PL reference')
    except (ValueError, KeyError, TypeError, OverflowError) as error:
        faults.append('invalid final code-ring metadata: ' + str(error))
    arrays = ('input_issue_words', 'completed_words', 'granted_words')
    if any(not isinstance(r.get(key), list) or len(r[key]) != 4 or
           any(not _integer(value) for value in r[key]) for key in arrays):
        faults.append('four finite producer cursors required')
    else:
        completed, grants = r['completed_words'], r['granted_words']
        if (r['input_issue_words'] != completed or r['reusable_words'] != min(completed) or
                any(not done <= grant <= r['stream_groups'] or grant - done > 512
                    for done, grant in zip(completed, grants))):
            faults.append('producer completed/granted/published ownership differs')
        if not 0 <= r['stream_groups'] - min(completed) <= RING_GROUPS:
            faults.append('live code-ring ownership exceeded capacity')
        target_words = elapsed * rate / 16e6
        if any(abs(done - target_words) > 1024 + target_words * .001 for done in completed):
            faults.append('producer cursor rate differs from requested output duration')
        if r['cap_source_word'] > min(completed):
            faults.append('capture cursor exceeds completed live output')
    if slow_us:
        # A negative control passes only when the finite grant fence stops output.
        # Consequent writer/producer FIFO stalls are expected, unrelated errors fail.
        if not (r['errors'] & (1 << 21) and r['pointer_errors'] & (1 << 6) and
                r['pointer_txstall_mask'] and r.get('reason') == 'SOURCE_EMPTY'):
            faults.append('slow worker did not expose a fenced input underrun')
        if (r['errors'] & ~((1 << 20) | (1 << 21) | 3) or
                r['pointer_errors'] & ~((1 << 6) | (1 << 3)) or r['output_errors'] & ~3):
            faults.append('slow worker stop contains unrelated faults')
        if not 0 < elapsed < ms * 1000:
            faults.append('slow worker failed to stop before the requested duration')
    else:
        for key in ('errors', 'pointer_errors', 'output_errors', 'txstalls_i', 'txstalls_q'):
            if r[key]:
                faults.append(key)
        if r.get('reason') != 'complete':
            faults.append('unexpected stop reason')
        if not ms * 1000 <= elapsed <= ms * 1000 + 50000:
            faults.append('elapsed duration differs from request')
        if not 0 < r['max_service_gap_us'] <= 128:
            faults.append('core 0 supervision exceeded 128 us')
        capture_target = rate * ms / 32000  # midpoint, in groups of sixteen symbols
        if abs(r['cap_source_word'] - capture_target) > 1024:
            faults.append('capture cursor differs from requested midpoint')
    cores = r.get('core')
    if not isinstance(cores, list) or len(cores) != 2:
        faults.append('missing two-core FEC accounting')
    else:
        for index, core in enumerate(cores):
            if not isinstance(core, dict) or any(not _integer(core.get(key))
                                               for key in ('frames', 'busy_cycles', 'max_frame_cycles',
                                                           'max_slice_cycles', 'timed_busy_cycles')):
                faults.append(f'invalid core {index} counters')
                continue
            if core['frames'] != (final + 1 - index) // 2:
                faults.append(f'core {index} frame ownership differs')
            stages = core.get('stage_cycles')
            if (not isinstance(stages, dict) or set(stages) != set(fec.STAGES) or
                    any(not _integer(value) for value in stages.values()) or
                    sum(stages.values()) != core['busy_cycles'] or
                    (core['frames'] and any(not stages[key] for key in fec.STAGES[:-1]))):
                faults.append(f'invalid core {index} stage accounting')
            if core['frames'] and not 0 < core['max_slice_cycles'] <= core['max_frame_cycles'] <= core['busy_cycles']:
                faults.append(f'invalid core {index} cycle maxima')
            charged = core['timed_busy_cycles'] + (r['supervise_cycles'] if index == 0 else 0)
            if (not _finite(core.get('busy')) or not math.isclose(charged / budget, core['busy'], abs_tol=.000002) or
                    core['timed_busy_cycles'] > core['busy_cycles'] or
                    (not slow_us and not 0 <= core['busy'] <= 1.02)):
                faults.append(f'invalid core {index} measured busy fraction')
    if not (r.get('cap_transport_ok') and r.get('cap_aligned') and r.get('cap_mismatches') == 0 and
            r.get('cap_checked_bus_words') == CAP_WORDS):
        faults.append(r.get('cap_error') or 'capture differs from independent GCS/FEC/PL/FIR reference')
    if not r.get('cap_clock_ok'):
        faults.append('DDR clock phase differs from requested control')
    return list(dict.fromkeys(faults))


def validate_run(result: dict, lines: list[str], cpw: int, ms: int, invert: int,
                 priority: int = 13, slow_us: int = 0, options: int = OPTIONS,
                 expected_git: str | None = None) -> dict:
    capture = validate_capture(result, lines, invert)
    faults = run_faults(dict(result, **capture), cpw, ms, invert, priority, slow_us,
                        options, expected_git)
    return dict(capture, faults=faults,
                classification='failed' if faults else 'source_underrun_detected' if slow_us
                else 'clock_inversion_detected' if invert else 'complete_8psk_2_3_clean')


def self_test():
    """Run the complete offline B9 reference and metadata checks without serial access."""
    import unittest
    from test_b9_trial import B9Protocol, B9Reference
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case)
                               for case in (B9Reference, B9Protocol))
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print('B9 scalar GCS/BCH/LDPC/8PSK/PL/FIR capture checks passed; no hardware accessed')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--port')
    parser.add_argument('--cpws', nargs='+', type=int, choices=(1, 2), default=[1, 2])
    parser.add_argument('--ms', type=int, default=3000)
    parser.add_argument('--inverts', nargs='+', type=int, choices=(0, 1), default=[0])
    parser.add_argument('--priorities', nargs='+', type=int, choices=range(16), default=[0, 13])
    parser.add_argument('--slow-us', type=int, default=0)
    parser.add_argument('--options', nargs='+', type=fec.option_mask, choices=(0, OPTIONS), default=[OPTIONS])
    parser.add_argument('--expected-git')
    parser.add_argument('--build', type=Path, default=iq.ROOT / 'firmware/build-exp')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        return
    if args.out is None or not args.port:
        parser.error('device runs require explicit --out and --port; use --self-test for offline validation')
    if not 100 <= args.ms <= 60000 or not 0 <= args.slow_us <= 10000:
        parser.error('--ms must be 100..60000 and --slow-us 0..10000')
    build = args.build.resolve()
    if 'IQ4HIST_EXPERIMENT:BOOL=ON' not in (build / 'CMakeCache.txt').read_text():
        parser.error('--build must contain an IQ4HIST_EXPERIMENT=ON image')
    if 'PICO_STACK_SIZE=0x1000' not in (build / 'build.ninja').read_text():
        parser.error('B9 needs the 4 KiB core 0 stack; configure -DCORE0_STACK_SRAM_HI=ON')
    args.out.mkdir(parents=True, exist_ok=False)
    shutil.copy2(__file__, args.out / 'driver.py')
    hashes = {}
    for name in ('iqbench.uf2', 'iqbench.elf', 'CMakeCache.txt', 'memmap_iq.ld', 'git_rev.h'):
        shutil.copy2(build / name, args.out / name)
        hashes['build/' + name] = hashlib.sha256((build / name).read_bytes()).hexdigest()
    for folder in ('firmware/src', 'host', 'reference'):
        for path in sorted((iq.ROOT / folder).rglob('*')):
            if path.suffix in ('.c', '.h', '.S', '.pio', '.py') and 'build' not in path.parts:
                hashes[str(path.relative_to(iq.ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
    hashes['firmware/CMakeLists.txt'] = hashlib.sha256((iq.ROOT / 'firmware/CMakeLists.txt').read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=args.ms, cpws=args.cpws,
                    inverts=args.inverts, priorities=args.priorities, slow_us=args.slow_us,
                    options=args.options, expected_firmware_git=args.expected_git,
                    head=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=iq.ROOT, text=True).strip(),
                    build_directory=str(build), sha256=hashes,
                    setup='Coordinator-owned digital GPIO experiment; explicit serial port only',
                    scope='On-chip memory-GCS/BB/BCH/LDPC/8PSK/PL/OUT8 shaping; independent sparse '
                          'DAC bus snapshots. No external ingress, AFE or RF qualification.')
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    iq.LOGGING = False
    board = iq.Board(args.port)
    failed = False
    try:
        info, _ = board.cmd('info')
        validate_clock(info, args.expected_git, CLOCK_HZ)
        (args.out / 'info.json').write_text(json.dumps(info, indent=2) + '\n')
        for cpw in args.cpws:
            for invert in args.inverts:
                for priority in args.priorities:
                    for options in args.options:
                        label = f'b9-cpw{cpw}-invert{invert}-prio{priority}-slow{args.slow_us}-opt{options}'
                        command = f'b9 {cpw} {args.ms} {invert} {priority} {args.slow_us} {options}'
                        result, lines = board.cmd(command, timeout=args.ms / 1000 + 60)
                        (args.out / f'{label}.txt').write_text('\n'.join(lines) + '\n')
                        result.update(validate_run(result, lines, cpw, args.ms, invert, priority,
                                                   args.slow_us, options, args.expected_git))
                        result['label'] = label
                        with (args.out / 'runs.jsonl').open('a') as stream:
                            stream.write(json.dumps(result) + '\n')
                        print(json.dumps(result), flush=True)
                        failed |= bool(result['faults'])
    finally:
        board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
