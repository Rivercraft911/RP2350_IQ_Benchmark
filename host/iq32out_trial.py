"""Continuous 32APSK component output; exact native333 FIR at one bounded offset.

Build and flash separately. This runner never builds or flashes. The Pico shapes
cyclic geometric constellation points with IRQs enabled while real DMA feeds PIO,
then captures4096 words of its GPIO bus. Rolling-key conversion happens before
runtime timing. No FEC, live input, AFE or RF qualification.
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
from dual_trial import parse_capture
from test_iq32native8 import TAPS as COEFFICIENTS, levels, expected
COEFFICIENT_SHA = '16936cb50f5de62e68b10367624b868afea4ad49f8dfc4481526de4712fda5ae'

SEED, IN_WORDS, RAW_WORDS, RATE = 0x1234ABCD, 8192, 4096, 2
CAP_WORDS, BLOCK_SYMBOLS, OUT_BLOCKS = 4096, 1024, 4
SEARCH_SYMBOLS = OUT_BLOCKS * BLOCK_SYMBOLS + 32
TABLE_CRCS = [3255586384, 916828675, 1817262751]
PROFILE = 'apsk32_l8_n2_native333'
PULSE = 'psk8_l8_n2_hold_m010_far2'
BUS_PER_SYMBOL = 4
KERNEL_NAMES = ('keys_c', 'keys_burst')


@functools.lru_cache(maxsize=1)
def source_words() -> np.ndarray:
    """Independent ring/angle geometric fixture, deliberately not DVB bit labels."""
    g1, g2 = 2.64, 4.64
    points = np.concatenate([
        np.exp(1j*(np.pi/4+np.arange(4)*np.pi/2))/g2,
        g1/g2*np.exp(1j*(np.pi/12+np.arange(12)*np.pi/6)),
        np.exp(1j*np.arange(16)*np.pi/8),
    ])
    labels = iq.m.xorshift32(SEED, RAW_WORDS*2) & 31
    values = levels(RATE)[0]
    selected = points[labels]
    codes = [np.argmin(abs(axis[:,None]-values),axis=1).astype(np.uint32)
             for axis in (selected.real,selected.imag)]
    assert all(np.max(abs(axis-values[code])) < 1e-12
               for axis,code in zip((selected.real,selected.imag),codes))
    words = (codes[0][::2] | codes[0][1::2]<<5 |
             codes[1][::2]<<16 | codes[1][1::2]<<21)
    assert zlib.crc32(words.astype('<u4').tobytes()) == 2979756864
    words.flags.writeable = False
    return words


@functools.lru_cache(maxsize=1)
def source_keys() -> np.ndarray:
    words=source_words()
    axes=[]
    for shift in (0,16):
        codes=((words[:,None]>>(shift+5*np.arange(2)))&31).ravel()
        windows=np.lib.stride_tricks.sliding_window_view(np.concatenate([codes[-2:],codes]),3)
        axes.append(windows[:,0]+17*windows[:,1]+289*windows[:,2])
    result=(axes[0]|axes[1]<<16).astype('<u4')
    assert len(result)==IN_WORDS and zlib.crc32(result.tobytes())==2091885109
    result.flags.writeable=False
    return result


@functools.lru_cache(maxsize=1)
def reference_period() -> np.ndarray:
    """Independent native333 partial FIR with the cyclic source's actual tail."""
    words=source_words()
    floating,q30=levels(RATE)
    scaled=(q30[:,None]*COEFFICIENTS+(1<<29))>>30
    raw,_,_=expected(words,words[-4:],scaled,floating)
    assert zlib.crc32(raw.astype('<u4').tobytes())==415574042
    values=raw.view('<i2').reshape(-1,4).astype(np.int64)
    i,q=(values[:,:2].ravel()&0x3fff,values[:,2:].ravel()&0x3fff)
    bus=np.stack([i|0x4000,q],axis=1).ravel().astype('<u2')
    assert len(bus)==RAW_WORDS*8 and zlib.crc32(bus.tobytes())==2015101740
    bus.flags.writeable=False
    return bus


def reference_window(cursor: int, search: int = SEARCH_SYMBOLS):
    if type(cursor) is not int or cursor <= 0 or search != SEARCH_SYMBOLS:
        raise ValueError('invalid bounded capture cursor/search')
    lower = max(0, cursor - search) * BUS_PER_SYMBOL
    upper = cursor * BUS_PER_SYMBOL
    period = reference_period()
    # The declared source is cyclic; the candidate interval remains finite and
    # tied to the actual producer cursor, rather than searching an arbitrary phase.
    indices = np.arange(lower, upper + 2 * CAP_WORDS, dtype=np.int64)
    return lower, upper, period[indices % len(period)]


def validate_capture(result: dict, lines: list[str]) -> dict:
    try:
        if result.get('cap_error'):
            raise ValueError(str(result['cap_error']))
        got = parse_capture(lines)
        if len(got) != CAP_WORDS or result.get('cap_words') != CAP_WORDS:
            raise ValueError('capture is incomplete')
        if zlib.crc32(got.tobytes()) != result.get('cap_crc') or result.get('cap_stride') != 1:
            raise ValueError('capture transport CRC or stride mismatch')
        lower, upper, reference = reference_window(result['cap_symbol_cursor'], result['cap_search_symbols'])
    except (KeyError, ValueError, TypeError) as exc:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(exc))
    bus = got.view('<u2')
    candidates = np.flatnonzero(reference[:upper-lower] == bus[0])
    for j in range(1, 32):
        candidates = candidates[reference[candidates + j] == bus[j]]
        if not len(candidates):
            break
    if not len(candidates):
        return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error='no bounded offset matches the first32 observations')
    best = min(((int(o), np.flatnonzero(reference[o:o+len(bus)] != bus)) for o in candidates),
               key=lambda item: len(item[1]))
    offset, bad = best
    axis = ((bus >> 14) & 1).astype(np.uint8)
    physical = np.array_equal(axis, int(axis[0]) ^ (np.arange(len(bus)) & 1)) and not np.any(bus & 0x8000)
    return dict(cap_transport_ok=True, cap_aligned=bool(physical), cap_physical_iq_ok=bool(physical),
                cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None,
                cap_checked_bus_words=len(bus), cap_global_bus_offset=lower+offset,
                cap_period_bus_offset=(lower+offset) % len(reference_period()),
                cap_start_search_bus_words=upper-lower)


def stream_faults(r: dict, clock: int, ms: int, cpw: int, kernel: int, expected_git=None) -> list[str]:
    if r.get('error'):
        return [f"firmware error: {r['error']}"]
    expected = dict(cmd='bench32out', clk_hz=clock*1000, ms=ms, cpw=cpw, kernel=kernel,
                    kernel_name=KERNEL_NAMES[kernel], code_placement='flash_cached',
                    L=8, sps=2, rate_index=RATE, gamma1_100=264, gamma2_100=464, outer_radius=1, row_profile=PROFILE,
                    pulse_profile=PULSE, coefficient_sha256=COEFFICIENT_SHA, quantizer='native_3_3_3',
                    table_bytes=117912, reverse_bytes=0, seed=SEED, in_words=IN_WORDS,
                    input_crc=2091885109, input_after_crc=2091885109, coordinate_words=RAW_WORDS,
                    coordinate_crc=2979756864, input_bytes_per_symbol=4, input_format='rolling_radix17_triples',
                    runtime_encode_included=False, history='cyclic_source_tail',
                    core1_idle=True, idle_wait='spin', interrupts_enabled=True, prefill_blocks=OUT_BLOCKS,
                    block_symbols=BLOCK_SYMBOLS, out_blocks=OUT_BLOCKS, out_words=2048,
                    cap_words=CAP_WORDS, cap_stride=1, cap_search_symbols=SEARCH_SYMBOLS,
                    **dict(zip(('old_crc', 'middle_crc', 'future_crc'), TABLE_CRCS)))
    if expected_git is not None:
        expected['git'] = expected_git
    faults = [f'{key}={r.get(key)!r}, expected {want!r}' for key, want in expected.items() if r.get(key) != want]
    faults += [key for key in ('errors', 'underruns', 'own_errors', 'idle_sent', 'txstalls')
               if type(r.get(key)) is not int or r[key] != 0]
    try:
        hz = clock*1000
        rs = hz/(4*cpw)
        elapsed, blocks = r['elapsed_us'], r['blocks']
        if not (ms*1000 <= elapsed <= ms*1000+1000 and blocks > 0):
            faults.append('duration or runtime block count invalid')
        if not math.isclose(r['sym_rate'], rs, abs_tol=.11):
            faults.append('symbol rate mismatch')
        if r['generated_blocks'] != blocks+OUT_BLOCKS:
            faults.append('producer block accounting mismatch')
        if r['shape_calls'] != blocks:
            faults.append('shaper call accounting mismatch')
        if not (r['setup_encode_cycles'] > 0 and math.isclose(
                r['setup_encode_cycles']/IN_WORDS,r['setup_encode_cyc_per_sym'],abs_tol=.00011)):
            faults.append('setup encoder accounting mismatch')
        if not 0 <= r['generated_blocks']-r['done_blocks'] <= OUT_BLOCKS:
            faults.append('output ring lead out of bounds')
        if abs(r['done_blocks']*BLOCK_SYMBOLS-rs*elapsed/1e6) > 3*BLOCK_SYMBOLS:
            faults.append('completed DMA blocks differ from elapsed output rate')
        for cycles_key, busy_key in (('shape_cycles', 'gen_busy'), ('producer_work_cycles', 'producer_work_busy')):
            busy = r[cycles_key]/(elapsed*hz/1e6)
            if not math.isfinite(r[busy_key]) or not 0 < r[busy_key] <= 1.001 or not math.isclose(r[busy_key], busy, abs_tol=2e-6):
                faults.append(f'{busy_key} accounting mismatch')
        if not math.isclose(r['shape_cycles']/(blocks*BLOCK_SYMBOLS), r['gen_cyc_per_sym'], abs_tol=.00011):
            faults.append('cycles/symbol accounting mismatch')
        if r['producer_work_cycles'] < r['shape_cycles']:
            faults.append('producer work smaller than measured shaper work')
        cursor = r['cap_symbol_cursor']
        if cursor % BLOCK_SYMBOLS or not 0 <= cursor//BLOCK_SYMBOLS-r['cap_done_blocks'] <= OUT_BLOCKS:
            faults.append('capture cursor/ring lead mismatch')
        if abs(cursor-rs*ms/2000) > 2*OUT_BLOCKS*BLOCK_SYMBOLS:
            faults.append('capture cursor differs from midpoint')
        if not (r.get('cap_transport_ok') and r.get('cap_aligned') and r.get('cap_mismatches') == 0 and
                r.get('cap_checked_bus_words') == 2*CAP_WORDS):
            faults.append(r.get('cap_error') or 'complete capture mismatch')
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        faults.append(f'invalid runtime metadata: {type(exc).__name__}')
    return list(dict.fromkeys(faults))


def capture_lines(bus):
    words = np.asarray(bus, dtype='<u2').view('<u4')
    lines = ['@cap '+str(k)+' '+' '.join(f'{int(w):08x}' for w in words[k:k+16]) for k in range(0, len(words), 16)]
    return dict(cap_words=len(words), cap_crc=zlib.crc32(words.tobytes()), cap_stride=1), lines


def self_test():
    cases = 0
    period = len(reference_period())
    for cursor in (9*BLOCK_SYMBOLS, RAW_WORDS*2+BLOCK_SYMBOLS, 100*RAW_WORDS*2+BLOCK_SYMBOLS):
        lower, upper, ref = reference_window(cursor)
        for phase in range(4):
            offset = 3000+phase
            r, lines = capture_lines(ref[offset:offset+8192])
            r.update(cap_symbol_cursor=cursor, cap_search_symbols=SEARCH_SYMBOLS)
            c = validate_capture(r, lines)
            assert c['cap_aligned'] and c['cap_mismatches'] == 0 and c['cap_global_bus_offset'] == lower+offset
            assert c['cap_checked_bus_words'] == 8192
            for malformed in (lines[1:], [lines[0], *lines], lines[:-1]):
                assert not validate_capture(r, malformed)['cap_transport_ok']
            assert not validate_capture(dict(r, cap_crc=r['cap_crc'] ^ 1), lines)['cap_transport_ok']
            bad = ref[offset:offset+8192].copy(); bad[5000] ^= 1
            br, bl = capture_lines(bad); br.update(cap_symbol_cursor=cursor, cap_search_symbols=SEARCH_SYMBOLS)
            bc = validate_capture(br, bl); assert bc['cap_mismatches'] == 1 and bc['cap_first_bad'] == 5000
            outside = reference_period()[(np.arange(8192)+lower-100) % period]
            br, bl = capture_lines(outside); br.update(cap_symbol_cursor=cursor, cap_search_symbols=SEARCH_SYMBOLS)
            assert not validate_capture(br, bl)['cap_aligned']
            cases += 1
    # The source wrap must contain the actual final seven symbols, rather than
    # reset-to-pilot history. Existing scalar/ISA tests independently validate the FIR.
    words = source_words(); floating,q30=levels(RATE)
    scaled=(q30[:,None]*COEFFICIENTS+(1<<29))>>30
    cyclic,_,_=expected(words[:256],words[-4:],scaled,floating)
    reset,_,_=expected(words[:256],None,scaled,floating)
    assert np.any(cyclic[:32] != reset[:32])
    source_keys()
    metadata_cases = 0
    for clock in (128000, 150000):
        for cpw in (6, 8):
            for kernel in range(len(KERNEL_NAMES)):
                hz = clock*1000; ms = 3000; rs = hz/(4*cpw)
                done = int(rs*ms/1000)//BLOCK_SYMBOLS
                blocks = done-1; cursor = (int(rs*ms/2000)//BLOCK_SYMBOLS+3)*BLOCK_SYMBOLS
                _, _, ref = reference_window(cursor)
                r, lines = capture_lines(ref[100:100+8192])
                shape = blocks*BLOCK_SYMBOLS*21
                r.update(cmd='bench32out', git='self-test', clk_hz=hz, ms=ms, cpw=cpw, kernel=kernel,
                         kernel_name=KERNEL_NAMES[kernel], code_placement='flash_cached',
                         L=8, sps=2, rate_index=RATE, gamma1_100=264, gamma2_100=464, outer_radius=1, row_profile=PROFILE,
                         pulse_profile=PULSE, coefficient_sha256=COEFFICIENT_SHA, quantizer='native_3_3_3',
                         table_bytes=117912, reverse_bytes=0, seed=SEED, in_words=IN_WORDS,
                         input_crc=2091885109, input_after_crc=2091885109, coordinate_words=RAW_WORDS,
                    coordinate_crc=2979756864, input_bytes_per_symbol=4, input_format='rolling_radix17_triples',
                    runtime_encode_included=False, history='cyclic_source_tail',
                         core1_idle=True, idle_wait='spin', interrupts_enabled=True, prefill_blocks=OUT_BLOCKS,
                         block_symbols=BLOCK_SYMBOLS, out_blocks=OUT_BLOCKS, out_words=2048,
                         cap_search_symbols=SEARCH_SYMBOLS, cap_symbol_cursor=cursor,
                         cap_done_blocks=cursor//BLOCK_SYMBOLS-3, elapsed_us=ms*1000,
                         generated_blocks=blocks+OUT_BLOCKS, blocks=blocks, shape_calls=blocks, done_blocks=done,
                         setup_encode_cycles=8192*7,setup_encode_cyc_per_sym=7,
                         shape_cycles=shape, gen_busy=shape/(ms*hz/1000), gen_cyc_per_sym=21,
                         producer_work_cycles=shape+blocks*20, producer_work_busy=(shape+blocks*20)/(ms*hz/1000),
                         sym_rate=rs, errors=0, underruns=0, own_errors=0, idle_sent=0, txstalls=0,
                         **dict(zip(('old_crc', 'middle_crc', 'future_crc'), TABLE_CRCS)))
                r.update(validate_capture(r, lines))
                assert not stream_faults(r, clock, ms, cpw, kernel, 'self-test'), stream_faults(r, clock, ms, cpw, kernel, 'self-test')
                for key in ('input_after_crc', 'future_crc', 'blocks', 'shape_calls', 'setup_encode_cycles', 'gen_busy', 'cap_symbol_cursor',
                            'clk_hz', 'cpw', 'kernel', 'errors', 'cap_mismatches', 'producer_work_cycles'):
                    bad = copy.deepcopy(r); bad[key] += hz if key == 'producer_work_cycles' else 1
                    assert stream_faults(bad, clock, ms, cpw, kernel, 'self-test'), key
                missing = dict(r); del missing['errors']
                assert stream_faults(missing, clock, ms, cpw, kernel, 'self-test')
                metadata_cases += 1
    print(json.dumps(dict(self_test='passed', capture_cases=cases, checked_halfwords=8192,
                          bounded_search_symbols=SEARCH_SYMBOLS, source_words=IN_WORDS,
                          source_crc=2091885109, periodic_bus_crc=zlib.crc32(reference_period().tobytes()),
                          metadata_profiles=metadata_cases,
                          negative_controls=['missing_line', 'duplicate_line', 'truncation', 'serial_crc',
                                             'sample_corruption', 'outside_bounded_window', 'history_reset',
                                             'wrong_input_or_table', 'wrong_clock_mode', 'work_accounting',
                                             'missing_fault_counter', 'capture_time'])))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-test', action='store_true')
    p.add_argument('--out', type=Path)
    p.add_argument('--ms', type=int, choices=(100, 3000, 60000), default=3000)
    p.add_argument('--clocks', type=int, nargs='+', choices=(128000, 150000), default=[128000, 150000])
    p.add_argument('--cpw', type=int, nargs='+', choices=(4, 6, 8), default=[6, 8])
    p.add_argument('--kernels', type=int, nargs='+', choices=range(len(KERNEL_NAMES)), default=[1])
    p.add_argument('--expect-deadline-fault', action='store_true', help='negative control; require an output deadline fault')
    p.add_argument('--port'); p.add_argument('--expected-git')
    p.add_argument('--build', type=Path, default=iq.ROOT/'firmware/build-limits')
    p.add_argument('--source', type=Path, default=iq.ROOT/'firmware')
    a = p.parse_args()
    if a.self_test: self_test(); return
    if a.out is None: p.error('--out is required')
    a.out.mkdir(parents=True, exist_ok=False)
    files = list((a.source/'src').glob('*'))+[a.source/'CMakeLists.txt']
    hashes = {'firmware/'+str(f.relative_to(a.source)): hashlib.sha256(f.read_bytes()).hexdigest()
              for f in files if f.is_file()}
    for f in (Path(__file__), iq.ROOT/'host/iqbench.py', iq.ROOT/'host/dual_trial.py',
              iq.ROOT/'host/test_iq32native8.py', iq.ROOT/'host/test_iq32native8_asm.py',
              iq.ROOT/'host/test_iq32keys.py', iq.ROOT/'reference/iqlut.py',
              iq.ROOT/'reference/gen_coeffs.py'):
        hashes[str(f.relative_to(iq.ROOT))] = hashlib.sha256(f.read_bytes()).hexdigest()
    for name in ('iqbench.uf2', 'iqbench.elf'):
        f = a.build/name; shutil.copy2(f, a.out/name); hashes[name] = hashlib.sha256(f.read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=a.ms, clocks_khz=a.clocks,
                    cpw=a.cpw, kernels=a.kernels, expect_deadline_fault=a.expect_deadline_fault,
                    expected_firmware_git=a.expected_git, head=iq.git_rev(), sha256=hashes,
                    source_firmware_directory=str(a.source.resolve()), build_directory=str(a.build.resolve()),
                    setup='USB only; disconnected external GPIOs; stock voltage; cyclic geometric32APSK fixture; rollingtriple keys4B/symbol',
                    scope='L8/N2 native333 prekeyed shaper, interrupt-enabled producer, DMA/PIO and GPIO capture; core1 idle; no modem/AFE/RF; setup-only key conversion excluded from runtime',
                    provenance='UF2 archived but flashing is external; verify source-to-flash identity separately',
                    cpu='Shaper and producer work are instrumented intervals; the outer wait spins and is not included in work fractions',
                    capture='One bounded source-cursor interval, one offset for all8192 contiguous halfwords; cyclic tail FIR history')
    (a.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    reference_period()
    iq.LOGGING = False
    board = None; failures = []; fatal = None
    def record(label, result):
        row = dict(label=label, recorded_at=dt.datetime.now().isoformat(), **result)
        with (a.out/'runs.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
        print(json.dumps(row), flush=True)
    try:
        board = iq.Board(a.port)
        info, _ = board.cmd('info'); record('info', info)
        for clock in a.clocks:
            changed, _ = board.cmd(f'clock {clock}'); record(f'clock-{clock}', changed)
            if changed.get('clk_hz') != clock*1000: raise RuntimeError('clock configuration failed')
            for kernel in a.kernels:
                for cpw in a.cpw:
                    result, lines = board.cmd(f'bench32out {cpw} {a.ms} {kernel}', timeout=a.ms/1000+60)
                    name = f'capture-{clock}-k{kernel}-cpw{cpw}.txt'
                    (a.out/name).write_text('\n'.join(lines)+'\n')
                    if not result.get('error') and result.get('cap_words'):
                        result.update(validate_capture(result, lines))
                    faults = stream_faults(result, clock, a.ms, cpw, kernel, a.expected_git)
                    if a.expect_deadline_fault:
                        passed = not result.get('error') and bool(result.get('errors', 0) & 7) and bool(result.get('underruns') or result.get('own_errors') or result.get('txstalls'))
                        if a.expected_git is not None: passed &= result.get('git') == a.expected_git
                        passed &= result.get('clk_hz') == clock*1000 and result.get('cpw') == cpw and result.get('kernel') == kernel
                        classification = 'expected-deadline-fault' if passed else 'negative-control-failed'
                    else:
                        passed = not faults; classification = 'clean' if passed else 'failed'
                    result.update(faults=faults, classification=classification, capture_file=name, passed=bool(passed))
                    record(f'32apsk-output-{clock}-k{kernel}-cpw{cpw}', result)
                    if not passed: failures.append(dict(clock_khz=clock, kernel=kernel, cpw=cpw, faults=faults))
    except Exception as exc:
        fatal = dict(exception=type(exc).__name__, detail=str(exc)); record('host-exception', fatal)
    finally:
        if board is not None:
            try: board.cmd('clock 128000')
            except Exception as exc: failures.append(dict(cleanup='clock reset failed', detail=str(exc)))
            board.s.close()
        summary = dict(passed=not failures and fatal is None, failures=failures, fatal=fatal)
        (a.out/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    if not summary['passed']: raise SystemExit(1)


if __name__ == '__main__': main()
