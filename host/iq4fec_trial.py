"""Live SPI/GCS 8PSK 5/6 FEC through the four-level L7/N4 hardware shaper (`bench4fec`).

Build (IQ4HIST_EXPERIMENT=ON) and flash separately; this runner never does either. The Pico
supplies its own on-chip PV-SPI pattern. Every one of 4096 sparse GPIO0..31 snapshots is
checked against an independent reference: real GCS BBFRAMEs -> scalar BCH/LDPC -> complete
pi/8-rotated 8PSK PLFRAMEs (iq8live_trial.frame_points) -> geometric four-level coordinates
-> the frozen L7 Q8 FIR with one Q38 rounding, from the stream's real start (six symbols of
the repeated pi/8 point as prehistory). GPIO28 must equal IQ_FLAG in every snapshot (all
inverted for the clock control). Digital only: no AFE, spectrum, EVM or receiver claim.

  python3 host/iq4fec_trial.py --self-test
  python3 host/iq4fec_trial.py --out results/b2-8psk-live/quick --ms 3000 --cpws 4 2
  python3 host/iq4fec_trial.py --out results/b3-8psk-rows/quick --writers 2 4 --schedules 0 1 2 3 --prios 13
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

import iqbench as iq
import dual_spi
from dual_trial import parse_capture
import iq8live_trial as live8
from pulse_profiles import FOUR_LEVELS_Q30, L7_COEFFICIENTS, L7_PROFILE

FRAME_SYMBOLS = live8.FRAME_SYMBOLS
BB_BITS = live8.BB_BITS
CAP_WORDS = 4096
STRIDE = 17
BUS_PER_SYMBOL = 8  # four samples/symbol, each I then Q
SPAN = 7
PREFILL_FRAMES = 2
TABLE_CRC = 0x19bf3c1d


def frame_codes(points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Nearest four-level code per axis, from the complex angle, not the firmware's planes."""
    levels = np.asarray(FOUR_LEVELS_Q30, dtype=np.float64) / 2**30
    return tuple(np.abs(axis[:, None] - levels[None, :]).argmin(axis=1)
                 for axis in (points.real, points.imag))


@functools.lru_cache(maxsize=8)
def symbol_codes(first: int, last: int) -> tuple[np.ndarray, np.ndarray]:
    """Codes for symbols first*F - 6 .. last*F, with real or pi/8 prehistory."""
    if first == 0:
        pre = np.full(SPAN - 1, np.exp(1j * np.pi / 8))
    else:
        pre = live8.frame_points(first - 1)[-(SPAN - 1):]
    points = np.concatenate([pre] + [live8.frame_points(k) for k in range(first, last)])
    return frame_codes(points)


def shaped_bus(first: int, last: int) -> np.ndarray:
    """Bus halfwords for frames first..last-1 (index 0 = frame `first`, symbol 0, sample 0)."""
    levels = np.asarray(FOUR_LEVELS_Q30, dtype=np.int64)
    coefficients = np.asarray(L7_COEFFICIENTS, dtype=np.int64).reshape(SPAN, 4)
    samples = []
    for codes in symbol_codes(first, last):
        values = levels[codes]
        histories = np.lib.stride_tricks.sliding_window_view(values, SPAN)[:, ::-1]
        samples.append(((histories @ coefficients + (1 << 37)) >> 38).ravel())
    bus = np.empty(2 * len(samples[0]), dtype='<u2')
    bus[0::2] = (samples[0] & 0x3fff) | 0x4000
    bus[1::2] = samples[1] & 0x3fff
    return bus


def reference_window(symbol_lo: int, symbol_hi: int):
    """Finite reference covering every candidate start in [symbol_lo, symbol_hi] plus a capture."""
    if not 0 <= symbol_lo <= symbol_hi:
        raise ValueError('invalid capture search window')
    end = symbol_hi + math.ceil(STRIDE * CAP_WORDS / BUS_PER_SYMBOL) + 1
    first, last = symbol_lo // FRAME_SYMBOLS, end // FRAME_SYMBOLS + 1
    bus = shaped_bus(first, last)
    base = first * FRAME_SYMBOLS * BUS_PER_SYMBOL
    return base, bus


def validate_capture(r: dict, lines: list[str], invert: int) -> dict:
    try:
        words = parse_capture(lines)
        if r.get('cap_words') != CAP_WORDS or len(words) != CAP_WORDS:
            raise ValueError('incomplete 4096-word GPIO capture')
        if zlib.crc32(words.tobytes()) != r.get('cap_crc'):
            raise ValueError('capture CRC mismatch (serial transfer)')
        if r.get('cap_stride') != STRIDE or r.get('cap_format') != 'pins32':
            raise ValueError('capture must be pins32 at stride 17')
        lo = int(r['cap_symbol_lo']) - int(r['cap_margin_symbols'])
        hi = int(r['cap_symbol_hi']) + int(r['cap_margin_symbols'])
        if hi < max(0, lo):
            raise ValueError('invalid capture search window')
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None, cap_error=str(exc))
    bus = (words & 0xffff).astype('<u2')
    clock_bad = int(np.count_nonzero(((words >> 28) & 1) != ((words >> 14) & 1)))
    clock_high = int(np.count_nonzero(words & (1 << 28)))
    clock = dict(cap_clock_checked=CAP_WORDS, cap_clock_mismatches=clock_bad, cap_clock_high=clock_high,
                 cap_clock_ok=clock_bad == (CAP_WORDS if invert else 0) and 0 < clock_high < CAP_WORDS)
    base, reference = reference_window(max(0, lo), hi)
    steps = STRIDE * np.arange(CAP_WORDS)
    start, stop = max(0, lo) * BUS_PER_SYMBOL - base, hi * BUS_PER_SYMBOL - base
    stop = min(stop, len(reference) - steps[-1] - 1)
    candidates = np.arange(start, stop + 1)
    for j in range(32):
        candidates = candidates[reference[candidates + steps[j]] == bus[j]]
        if not len(candidates):
            break
    if len(candidates) != 1:
        return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                    cap_candidates=int(len(candidates)), cap_search_bus_words=int(stop - start + 1),
                    cap_error='no unique offset in the bounded window matches the first 32 snapshots', **clock)
    offset = int(candidates[0])
    bad = np.flatnonzero(reference[offset + steps] != bus)
    return dict(cap_transport_ok=True, cap_aligned=True, cap_global_bus_offset=base + offset,
                cap_symbol=(base + offset) // BUS_PER_SYMBOL, cap_search_bus_words=int(stop - start + 1),
                cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None,
                cap_checked_bus_words=CAP_WORDS, **clock)


def run_faults(r: dict, cpw: int, ms: int, invert: int, expected_git: str | None,
               *, writers: int = 2, dma_schedule: int = 0,
               clock_hz: int = 128000000) -> list[str]:
    if r.get('error'):
        return [f"firmware error: {r['error']}"]
    faults = []
    if (cpw not in (2, 4) or writers not in (2, 4) or dma_schedule not in range(4) or
            clock_hz not in (128000000, 256000000) or invert not in (0, 1)):
        return ['unsupported requested configuration']
    rs = clock_hz / (8 * cpw)
    expected = dict(cmd='bench4fec', clk_hz=clock_hz, cpw=cpw, ms=ms, invert=invert, source=6,
                    code_index=8, s2_code='normal 5/6', modcod=15, kbch=BB_BITS, pilots=True, L=SPAN,
                    sps=4, row_profile=L7_PROFILE, row_symbols=1, row_words=2, writers=writers,
                    dma_schedule=dma_schedule, pointer_pio=0 if writers == 4 else 2,
                    pointer_routing='alternating_dma' if writers == 4 else 'direct',
                    frame_symbols=FRAME_SYMBOLS, table_crc=TABLE_CRC, prefill_frames=PREFILL_FRAMES,
                    restored=True, selectors_restored=True, errors=0, pointer_errors=0, output_errors=0,
                    txstalls_i=0, txstalls_q=0, null_trigger_mask=0)
    expected.update({k: v for k, v in live8.prefill_reference().items() if k != 'prefill_frames'})
    if expected_git:
        expected['git'] = expected_git
    faults += [f'{k}={r.get(k)!r}, expected {v!r}' for k, v in expected.items() if r.get(k) != v]
    if (not isinstance(r.get('sym_rate'), (int, float)) or
            not math.isclose(r['sym_rate'], rs, rel_tol=1e-9)):
        faults.append('symbol rate differs from the requested divider')
    counters = ('elapsed_us', 'encoder_frames', 'encoder_runtime_frames', 'input_published_groups',
                'granted_words', 'published_words', 'converted_groups', 'max_poll_gap_us',
                'max_poll_interval_us', 'min_credit_runway_words')
    invalid = [name for name in counters if type(r.get(name)) is not int or r[name] < 0]
    issued = r.get('input_issue_words')
    if (not isinstance(issued, list) or len(issued) != 2 or
            any(type(count) is not int or count < 0 for count in issued)):
        invalid.append('input_issue_words')
    if invalid:
        return faults + ['invalid runtime counters: ' + ', '.join(invalid)]
    elapsed = r.get('elapsed_us', 0)
    if not ms * 1000 <= elapsed <= ms * 1000 + 50000:
        faults.append('elapsed duration differs from request')
    frames = r.get('encoder_frames', 0)
    if frames <= PREFILL_FRAMES or r.get('encoder_runtime_frames') != frames - PREFILL_FRAMES:
        faults.append('no coherent runtime encoder frame count')
    if r.get('input_published_groups') != frames * FRAME_SYMBOLS // 16:
        faults.append('encoder group publication differs from complete frames')
    if not (min(issued) * 16 >= rs * elapsed / 1e6 * 0.98 and max(issued) <= r.get('granted_words', -1)
            <= r.get('published_words', -1) <= r.get('converted_groups', -1) <= r.get('input_published_groups', -1)):
        faults.append('pointer issue/grant/publication/conversion ordering or rate inconsistent')
    if not 0 < r.get('max_poll_gap_us', 10**9) <= r.get('max_poll_interval_us', 0):
        faults.append('supervision poll gap exceeded its bound')
    if r.get('min_credit_runway_words', -1) <= 0:
        faults.append('credit runway reached zero')
    spi = r.get('pv_spi', {})
    if not isinstance(spi, dict):
        return faults + ['invalid packet accounting: expected pv_spi object']
    try:
        faults.extend(dual_spi.packet_faults(dict(r, source=6)))
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        faults.append(f'invalid packet accounting: {type(exc).__name__}')
    if spi.get('bbframes') != frames or spi.get('runtime_bbframes') != frames - PREFILL_FRAMES:
        faults.append('GCS/encoder BBFRAME count mismatch')
    if not (r.get('cap_transport_ok') and r.get('cap_aligned') and r.get('cap_mismatches') == 0):
        faults.append(r.get('cap_error') or 'capture differs from the independent reference')
    if not r.get('cap_clock_ok'):
        faults.append('DDR clock parity differs from ' + ('inverted control' if invert else 'I-slot high'))
    return list(dict.fromkeys(faults))


def validate_run(r: dict, lines: list[str], cpw: int, ms: int, invert: int,
                 expected_git: str | None, *, writers: int = 2, dma_schedule: int = 0,
                 clock_hz: int = 128000000) -> dict:
    """Apply the same independent waveform reference to either hardware topology."""
    capture = {} if r.get('error') else validate_capture(r, lines, invert)
    faults = run_faults(dict(r, **capture), cpw, ms, invert, expected_git, writers=writers,
                        dma_schedule=dma_schedule, clock_hz=clock_hz)
    return dict(capture, faults=faults, classification='failed' if faults else
                'inverted_clock_detected' if invert else 'digital_live_clean',
                negative_control_detected=not faults and bool(invert))


def capture_lines(words: np.ndarray) -> tuple[dict, list[str]]:
    words = np.asarray(words, dtype='<u4')
    lines = ['@cap ' + str(k) + ' ' + ' '.join(f'{int(w):08x}' for w in words[k:k + 16])
             for k in range(0, len(words), 16)]
    return dict(cap_words=len(words), cap_crc=zlib.crc32(words.tobytes()), cap_stride=STRIDE,
                cap_format='pins32'), lines


def self_test():
    """No hardware: a synthetic pins32 capture must align uniquely; corruptions must fail."""
    for symbol in (0, 1, FRAME_SYMBOLS - 300, 3 * FRAME_SYMBOLS + 12345):
        lo, hi = max(0, symbol - 40), symbol + 40
        base, ref = reference_window(max(0, lo - 256), hi + 256)
        start = symbol * BUS_PER_SYMBOL + 3 - base
        bus = ref[start + STRIDE * np.arange(CAP_WORDS)].astype(np.uint32)
        flag = (bus >> 14) & 1
        noise = iq.m.xorshift32(symbol + 1, CAP_WORDS).astype(np.uint32) & ~np.uint32(0xffff | 1 << 28)
        for invert, clock in ((0, flag), (1, 1 - flag)):
            words = bus | noise | clock << 28
            meta, lines = capture_lines(words)
            r = dict(meta, cap_symbol_lo=lo, cap_symbol_hi=hi, cap_margin_symbols=256)
            check = validate_capture(r, lines, invert)
            assert check['cap_aligned'] and check['cap_mismatches'] == 0 and check['cap_clock_ok'], check
            assert check['cap_global_bus_offset'] == symbol * BUS_PER_SYMBOL + 3, check
        bad = (bus | clock << 28).copy(); bad[4000] ^= 1
        meta, lines = capture_lines(bad)
        check = validate_capture(dict(meta, cap_symbol_lo=lo, cap_symbol_hi=hi, cap_margin_symbols=256), lines, 1)
        assert check['cap_mismatches'] == 1 or not check['cap_aligned'], check
        meta, lines = capture_lines(bus | flag << 28)
        check = validate_capture(dict(meta, cap_symbol_lo=lo, cap_symbol_hi=hi, cap_margin_symbols=256), lines, 1)
        assert not check['cap_clock_ok'], check
    # The six-symbol prehistory at stream start is the repeated pi/8 point, both axes.
    i, q = symbol_codes(0, 1)
    assert list(i[:6]) == [0] * 6 and list(q[:6]) == [2] * 6
    print('iq4fec_trial self-test: frame-boundary references, unique alignment, clock parity, '
          'inverted control and data corruption checked; no hardware accessed')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out', type=Path)
    p.add_argument('--cpws', nargs='+', type=int, choices=(2, 4), default=[2])
    p.add_argument('--inverts', nargs='+', type=int, choices=(0, 1), default=[0, 1])
    p.add_argument('--ms', type=int, default=3000)
    p.add_argument('--ldpc-pack', type=int, choices=(0, 1), default=1)
    p.add_argument('--prios', nargs='+', type=int, choices=range(16), default=[0],
                   help='dual_dma bus-priority masks to try (register fields PROC0/PROC1/DMA_R/DMA_W)')
    p.add_argument('--writers', nargs='+', type=int, choices=(2, 4), default=[2],
                   help='two direct writers or four writers with alternating-row DMA routing')
    p.add_argument('--schedules', nargs='+', type=int, choices=range(4), default=[0],
                   help='DMA 0=all high, 1=sample low, 2=control/router low, 3=interleaved claim order')
    p.add_argument('--clocks', nargs='+', type=int, choices=(128000, 256000), default=[128000],
                   help='requested system clocks in kHz (firmware must support the clock profile)')
    p.add_argument('--port')
    p.add_argument('--expected-git')
    p.add_argument('--build', type=Path, default=iq.ROOT / 'firmware/build-b1')
    p.add_argument('--self-test', action='store_true')
    args = p.parse_args()
    if args.self_test:
        self_test()
        return
    if args.out is None or not 100 <= args.ms <= 60000:
        p.error('--out is required and --ms must be 100..60000')
    args.out.mkdir(parents=True, exist_ok=False)
    build = args.build.resolve()
    cache = (build / 'CMakeCache.txt').read_text()
    if 'IQ4HIST_EXPERIMENT:BOOL=ON' not in cache:
        p.error('--build must contain an IQ4HIST_EXPERIMENT=ON firmware build')
    hashes = {}
    for name in ('iqbench.uf2', 'iqbench.elf'):
        shutil.copy2(build / name, args.out / name)
    for name in ('iqbench.uf2', 'iqbench.elf', 'CMakeCache.txt', 'memmap_iq.ld', 'git_rev.h'):
        hashes['build/' + name] = hashlib.sha256((build / name).read_bytes()).hexdigest()
    sources = [f for folder in ('firmware/src', 'host', 'reference') for f in (iq.ROOT / folder).rglob('*')
               if f.suffix in ('.c', '.h', '.S', '.py') and 'build' not in f.parts]
    sources.append(iq.ROOT / 'firmware/CMakeLists.txt')
    hashes.update({str(f.relative_to(iq.ROOT)): hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(sources)})
    git = lambda *a: subprocess.run(['git', *a], cwd=iq.ROOT, capture_output=True, text=True).stdout.strip()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=args.ms, cpws=args.cpws, inverts=args.inverts,
                    prios=args.prios, writers=args.writers, dma_schedules=args.schedules,
                    clocks_khz=args.clocks,
                    ldpc_pack=args.ldpc_pack, expected_firmware_git=args.expected_git,
                    head=git('rev-parse', 'HEAD'), branch=git('branch', '--show-current'),
                    status=git('status', '--short'), cmake_flag='IQ4HIST_EXPERIMENT:BOOL=ON', sha256=hashes,
                    setup='USB only, external GPIOs unconnected, on-chip PV-SPI master',
                    scope='Live GCS 8PSK 5/6 FEC through L7/N4 hardware shaping; sparse pins32 captures; '
                          'no AFE/RF/receiver qualification')
    (args.out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    shutil.copy2(__file__, args.out / 'driver.py')
    iq.LOGGING = False
    board = iq.Board(args.port)
    failed = False
    try:
        for line in ('info', 'bchkernel 2', f'ldpcpack {args.ldpc_pack}'):
            r, _ = board.cmd(line, timeout=60)
            with (args.out / 'runs.jsonl').open('a') as f:
                f.write(json.dumps(dict(label=line, **r)) + '\n')
            if 'error' in r:
                raise SystemExit(f"{line}: {r['error']}")
        for clock in args.clocks:
            line = f'clock {clock}'
            r, _ = board.cmd(line, timeout=60)
            with (args.out / 'runs.jsonl').open('a') as f:
                f.write(json.dumps(dict(label=line, **r)) + '\n')
            if 'error' in r:
                raise SystemExit(f"{line}: {r['error']}")
            for writers, schedule, cpw, invert, prio in (
                    (w, s, c, i, m) for w in args.writers for s in args.schedules
                    for m in args.prios for c in args.cpws for i in args.inverts):
                label = (f'bench4fec-clk{clock}-w{writers}-schedule{schedule}'
                         f'-cpw{cpw}-invert{invert}-prio{prio}')
                r, lines = board.cmd(f'bench4fec {cpw} {args.ms} {invert} {prio} {writers} {schedule}',
                                     timeout=args.ms / 1000 + 60)
                (args.out / f'{label}.txt').write_text('\n'.join(lines) + '\n')
                r.update(validate_run(r, lines, cpw, args.ms, invert, args.expected_git,
                                      writers=writers, dma_schedule=schedule, clock_hz=clock * 1000))
                if r.get('bus_priority_mask', 0) != prio:
                    r['faults'].append('reported bus priority differs from request')
                    r.update(classification='failed', negative_control_detected=False)
                r['label'] = label
                with (args.out / 'runs.jsonl').open('a') as f:
                    f.write(json.dumps(r) + '\n')
                print(label, json.dumps({k: r.get(k) for k in (
                    'elapsed_us', 'sym_rate', 'core0_work_busy', 'convert_busy', 'supervision_busy',
                    'encoder_busy', 'min_credit_runway_words', 'cap_mismatches', 'cap_clock_mismatches',
                    'reason', 'classification', 'faults')}), flush=True)
                failed |= bool(r['faults'])
    finally:
        for line in ('ldpcpack 0', 'bchkernel 0'):
            board.cmd(line, timeout=60)
        board.s.close()
    if failed:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
