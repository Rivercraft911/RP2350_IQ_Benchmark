"""Planar16APSK shaper -> immutable row pointers -> dual DMA/PIO output trial.

Build/flash externally. Exact scalar Q8 reference checks all8192 sparse bus values
at one offset bounded by source-read snapshots around capture arming. No modem/RF.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
from pathlib import Path
import shutil
import zlib

import numpy as np

import iq16out_trial as base
from dual_trial import parse_capture


def validate_capture(r, lines):
    try:
        got = parse_capture(lines)
        if r.get('cap_error') or len(got) != 4096 or r.get('cap_words') != 4096:
            raise ValueError('capture incomplete or missing terminator')
        if zlib.crc32(got.tobytes()) != r.get('cap_crc') or r.get('cap_stride') != 17:
            raise ValueError('capture transport CRC/stride mismatch')
        lower, upper = r['cap_lower_symbol_cursor']*4, r['cap_symbol_cursor']*4
        if not 0 <= lower < upper or upper-lower > 8192:
            raise ValueError('capture source-read window invalid')
    except (KeyError, ValueError, TypeError) as exc:
        return dict(cap_transport_ok=False, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error=str(exc))
    bus = got.view('<u2'); period = base.reference_period()
    offsets = np.arange(lower, upper, dtype=np.int64)
    steps = np.arange(len(bus), dtype=np.int64)*17
    candidates = offsets[period[offsets % len(period)] == bus[0]]
    for j in range(1, 32):
        candidates = candidates[period[(candidates+steps[j]) % len(period)] == bus[j]]
        if not len(candidates): break
    if not len(candidates):
        return dict(cap_transport_ok=True, cap_aligned=False, cap_mismatches=None,
                    cap_checked_bus_words=0, cap_error='no bounded source-read offset matches first32 observations')
    offset, bad = min(((int(o), np.flatnonzero(period[(o+steps) % len(period)] != bus)) for o in candidates),
                      key=lambda item: len(item[1]))
    axis = ((bus >> 14) & 1).astype(np.uint8)
    physical = np.array_equal(axis, int(axis[0]) ^ (np.arange(len(bus)) & 1)) and not np.any(bus & 0x8000)
    return dict(cap_transport_ok=True, cap_aligned=bool(physical), cap_physical_iq_ok=bool(physical),
                cap_mismatches=int(len(bad)), cap_first_bad=int(bad[0]) if len(bad) else None,
                cap_checked_bus_words=len(bus), cap_global_bus_offset=offset,
                cap_period_bus_offset=offset % len(period), cap_start_search_bus_words=upper-lower)


def stream_faults(r, clock, ms, cpw, expected_git=None):
    if r.get('error'): return [f"firmware error: {r['error']}"]
    expected = dict(cmd='bench16planar', clk_hz=clock*1000, ms=ms, cpw=cpw,
                    kernel_name='planar_burst', code_placement='flash_cached', L=8, sps=2,
                    gamma100=270, row_profile=base.PROFILE, quantizer='native_3_4_4', table_bytes=139264,
                    seed=base.SEED, in_words=8192, input_crc=787286031, input_after_crc=787286031,
                    history='cyclic_source_tail', core1_idle=True, interrupts_enabled=True, idle_wait='spin_8us',
                    writers=2, row_words=4, descriptor_rows=1024, descriptor_bytes=8192, row_layout='I4_Q4',
                    runtime_sample_copies=0, runtime_descriptor_writes=0, selectors_restored=True,
                    prefill_blocks=4, block_symbols=1024, reuse_guard_rows=8,
                    cap_words=4096, cap_stride=17, cap_lookbehind_rows=32,
                    **dict(zip(('old_crc', 'middle_crc', 'future_crc'), base.TABLE_CRCS)))
    if expected_git is not None: expected['git'] = expected_git
    faults = [f'{key}={r.get(key)!r}, expected {want!r}' for key, want in expected.items() if r.get(key) != want]
    faults += [key for key in ('errors', 'output_errors', 'txstalls_i', 'txstalls_q')
               if type(r.get(key)) is not int or r[key] != 0]
    try:
        hz = clock*1000; rs = hz/(4*cpw)
        elapsed, blocks = r['elapsed_us'], r['blocks']
        if not ms*1000 <= elapsed <= ms*1000+1000 or blocks <= 0: faults.append('duration/block count invalid')
        if not math.isclose(r['sym_rate'], rs, abs_tol=.11): faults.append('symbol rate mismatch')
        if r['generated_blocks'] != blocks+4: faults.append('produced block accounting mismatch')
        low, high = min(r['read_i'], r['read_q']), max(r['issued_i'], r['issued_q'])
        if not 8 < r['generated_blocks']*256-high <= 1024: faults.append('invalid final produced/issued lead')
        if not 0 <= high-low <= 64: faults.append('axis cursor gap out of bounds')
        if abs(high*4-rs*elapsed/1e6) > 128: faults.append('issued rows differ from elapsed output rate')
        if not r['min_lead_rows'] > 8 or r['max_axis_gap_rows'] > 64: faults.append('runway/axis diagnostic invalid')
        if r['desc_i_base'] % 4096 or r['desc_q_base'] != r['desc_i_base']+4096: faults.append('descriptor alignment/layout invalid')
        for cycles_key, busy_key in (('shape_cycles', 'gen_busy'), ('producer_work_cycles', 'producer_work_busy')):
            want = r[cycles_key]/(elapsed*hz/1e6)
            if not 0 < r[busy_key] <= 1.001 or not math.isclose(r[busy_key], want, abs_tol=2e-6):
                faults.append(f'{busy_key} accounting mismatch')
        if not math.isclose(r['shape_cycles']/(blocks*1024), r['gen_cyc_per_sym'], abs_tol=.00011): faults.append('cycles/symbol mismatch')
        if r['producer_work_cycles'] < r['shape_cycles']: faults.append('producer work less than shaper work')
        if abs(r['cap_symbol_cursor']-rs*ms/2000) > 2048: faults.append('capture cursor differs from midpoint')
        if not (r.get('cap_transport_ok') and r.get('cap_aligned') and r.get('cap_mismatches') == 0 and r.get('cap_checked_bus_words') == 8192):
            faults.append(r.get('cap_error') or 'complete sparse capture mismatch')
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        faults.append(f'invalid runtime metadata: {type(exc).__name__}')
    return list(dict.fromkeys(faults))


def self_test():
    period = base.reference_period(); steps = np.arange(8192)*17
    cases = 0
    for cursor in (32768, 32768*99, 10**8):
        lower = cursor-128
        for phase in range(4):
            offset = lower*4+64+phase
            bus = period[(offset+steps) % len(period)]
            r, lines = base.capture_lines(bus)
            r.update(cap_stride=17, cap_symbol_cursor=cursor, cap_lower_symbol_cursor=lower)
            check = validate_capture(r, lines)
            assert check['cap_aligned'] and check['cap_mismatches'] == 0 and check['cap_global_bus_offset'] == offset
            bad = bus.copy(); bad[5000] ^= 1
            br, bl = base.capture_lines(bad); br.update(cap_stride=17, cap_symbol_cursor=cursor, cap_lower_symbol_cursor=lower)
            checked = validate_capture(br, bl); assert checked['cap_mismatches'] == 1 and checked['cap_first_bad'] == 5000
            assert not validate_capture(dict(r, cap_crc=r['cap_crc'] ^ 1), lines)['cap_transport_ok']
            assert not validate_capture(r, lines[1:])['cap_transport_ok']
            assert not validate_capture(dict(r, cap_lower_symbol_cursor=cursor+1), lines)['cap_transport_ok']
            wrong, wl = base.capture_lines(period[(offset+1000+steps) % len(period)])
            wrong.update(cap_stride=17, cap_symbol_cursor=cursor, cap_lower_symbol_cursor=lower)
            assert not validate_capture(wrong, wl)['cap_aligned']
            cases += 1
    print(json.dumps(dict(self_test='passed', capture_cases=cases, checked_halfwords=8192, stride=17,
                          one_offset=True, physical_period_wrap=True,
                          negative_controls=['corruption', 'transport_crc', 'missing_line', 'invalid_bounds', 'outside_bounded_window'])))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--self-test', action='store_true'); p.add_argument('--out', type=Path)
    p.add_argument('--ms', type=int, choices=(100, 3000, 60000), default=3000)
    p.add_argument('--clocks', type=int, nargs='+', choices=(128000, 150000), default=[128000, 150000])
    p.add_argument('--cpw', type=int, nargs='+', choices=(4, 6, 8), default=[6, 8])
    p.add_argument('--port'); p.add_argument('--expected-git')
    p.add_argument('--build', type=Path, default=base.iq.ROOT/'firmware/build-limits')
    p.add_argument('--source', type=Path, default=base.iq.ROOT/'firmware')
    a = p.parse_args()
    if a.self_test: self_test(); return
    if a.out is None: p.error('--out required')
    a.out.mkdir(parents=True, exist_ok=False)
    files = [f for f in (a.source/'src').rglob('*') if f.is_file()]+[a.source/'CMakeLists.txt']
    hashes = {'firmware/'+str(f.relative_to(a.source)): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
    models = ('iq16planar_trial.py', 'iq16out_trial.py', 'iqbench.py', 'dual_trial.py', 'test_iq16chunk.py', 'test_iq16native8.py')
    hashes.update({'host/'+name: hashlib.sha256((base.iq.ROOT/'host'/name).read_bytes()).hexdigest() for name in models})
    for name in ('iqbench.uf2', 'iqbench.elf'):
        f = a.build/name; shutil.copy2(f, a.out/name); hashes[name] = hashlib.sha256(f.read_bytes()).hexdigest()
    manifest = dict(started=dt.datetime.now().isoformat(), ms=a.ms, clocks_khz=a.clocks, cpw=a.cpw,
                    expected_firmware_git=a.expected_git, head=base.iq.git_rev(), sha256=hashes,
                    setup='USB only; disconnected external GPIOs; stock voltage',
                    scope='Native344 planar16APSK shaper, immutable direct row pointers, two-writer DMA/PIO, sparse GPIO capture; core1 idle; no modem/AFE/RF',
                    cpu='IRQ-enabled instrumented shaper and producer work; excludes8us spin waits and is not total CPU active fraction',
                    provenance='UF2 archived; flash/source identity requires separate verification',
                    capture='All8192 stride17 bus values share one finite offset bounded by pre/post capture-arm read snapshots')
    (a.out/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    base.reference_period(); board = None; failures = []; fatal = None
    def record(label, value):
        row = dict(label=label, recorded_at=dt.datetime.now().isoformat(), **value)
        with (a.out/'runs.jsonl').open('a') as f: f.write(json.dumps(row)+'\n')
        print(json.dumps(row), flush=True)
    try:
        board = base.iq.Board(a.port)
        info, _ = board.cmd('info'); record('info', info)
        for clock in a.clocks:
            changed, _ = board.cmd(f'clock {clock}'); record(f'clock-{clock}', changed)
            if changed.get('clk_hz') != clock*1000: raise RuntimeError('clock configuration failed')
            for cpw in a.cpw:
                r, lines = board.cmd(f'bench16planar {cpw} {a.ms}', timeout=a.ms/1000+60)
                name = f'capture-{clock}-cpw{cpw}.txt'; (a.out/name).write_text('\n'.join(lines)+'\n')
                if not r.get('error') and r.get('cap_words'): r.update(validate_capture(r, lines))
                faults = stream_faults(r, clock, a.ms, cpw, a.expected_git)
                r.update(faults=faults, classification='failed' if faults else 'clean', capture_file=name, passed=not faults)
                record(f'16apsk-planar-{clock}-cpw{cpw}', r)
                if faults: failures.append(dict(clock_khz=clock, cpw=cpw, faults=faults))
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
